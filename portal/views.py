import csv
import logging
import mimetypes
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from zipfile import BadZipFile, LargeZipFile, ZipFile

from accounts.models import User

from .forms import CompanyProfileForm, CompanyRequirementForm, DailyReportForm, InternProfileForm, OJTRequirementUploadForm, PostingForm, ScorecardForm, WeeklyReportForm
from .models import Application, AttendanceLog, AuditLog, CompanyProfile, CompanyRequirement, DailyReport, FaceEnrollment, InternProfile, OJTRequirement, Posting, RiskAssessment, Scorecard, WeeklyReport, assign_accepted_company_ojt_requirements, assign_company_ojt_requirements, baseline_ojt_requirements_approved
from .utils import attendance_pdf, audit, certificate_pdf
from ml_engine.face_capture import FaceCaptureError, decode_camera_image
from ml_engine.face_recognition import (
    FaceImageError,
    FaceModelError,
    compare_face_embedding,
    create_face_embedding,
    encrypt_embedding,
)

OJT_TARGET_HOURS = 120
LOGGER = logging.getLogger(__name__)


def role_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if not (request.user.is_staff or request.user.role in roles):
                return render(request, "errors/403.html", status=403)
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    if request.user.is_staff or request.user.role == User.Role.COORDINATOR:
        return redirect("portal:coordinator_dashboard")
    if request.user.role == User.Role.COMPANY:
        return redirect("portal:company_dashboard")
    return redirect("portal:intern_dashboard")


@role_required(User.Role.INTERN)
def intern_dashboard(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    required_requirements = intern.ojt_requirements.filter(is_required=True, company__isnull=True)
    ojt_ready = baseline_ojt_requirements_approved(intern)
    completed_hours = round(sum(
        log.hours_worked
        for log in intern.attendance_logs.filter(time_in_approved=True, time_out_approved=True)
    ), 2)
    target_hours = OJT_TARGET_HOURS
    context = {
        "intern": intern,
        "recent_attendance": intern.attendance_logs.all()[:7],
        "recent_reports": intern.weekly_reports.all()[:4],
        "recent_daily_reports": intern.daily_reports.all()[:4],
        "application_count": intern.applications.count(),
        "latest_risk": intern.risk_assessments.first(),
        "today": timezone.localdate(),
        "completed_hours": completed_hours,
        "target_hours": target_hours,
        "progress_percent": min(100, round(completed_hours / target_hours * 100)),
        "ojt_ready": ojt_ready,
        "ojt_requirements_remaining": required_requirements.exclude(
            status=OJTRequirement.Status.APPROVED
        ).count(),
    }
    return render(request, "portal/intern/dashboard.html", context)


@role_required(User.Role.INTERN)
def profile(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    form = InternProfileForm(request.POST or None, request.FILES or None, instance=intern)
    if request.method == "POST" and form.is_valid():
        profile_instance = form.save()
        assign_accepted_company_ojt_requirements(profile_instance)
        audit(request.user, "profile.updated", profile_instance)
        messages.success(request, "Profile saved.")
        return redirect("portal:profile")
    return render(request, "portal/intern/profile.html", {"form": form, "intern": intern})


@role_required(User.Role.INTERN)
def postings(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
        return render(request, "portal/intern/postings.html", {
            "postings": Posting.objects.none(),
            "application_count": intern.applications.count(),
            "external_placement": True,
        })
    applied_ids = intern.applications.values_list("posting_id", flat=True)
    available = Posting.objects.filter(status=Posting.Status.PUBLISHED).exclude(pk__in=applied_ids).select_related("company")
    return render(request, "portal/intern/postings.html", {
        "postings": available,
        "application_count": len(applied_ids),
        "external_placement": False,
    })


@role_required(User.Role.INTERN)
@require_POST
def apply_to_posting(request, posting_id):
    intern = get_object_or_404(InternProfile, user=request.user)
    if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
        messages.error(request, "External placements work directly with the OJT coordinator and cannot apply to Fieldwork company opportunities.")
        return redirect("portal:postings")
    posting = get_object_or_404(Posting, pk=posting_id, status=Posting.Status.PUBLISHED)
    application = Application(
        intern=intern,
        posting=posting,
        cover_letter=request.POST.get("cover_letter", "").strip(),
        resume=request.FILES.get("resume"),
    )
    try:
        application.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("portal:postings")
    try:
        with transaction.atomic():
            application.save()
    except IntegrityError:
        messages.info(request, "You have already applied for this opportunity.")
    else:
        audit(request.user, "application.submitted", application)
        messages.success(request, "Application submitted.")
    return redirect("portal:postings")


def _coordinates(request, prefix):
    latitude = request.POST.get(f"{prefix}_latitude", "").strip()
    longitude = request.POST.get(f"{prefix}_longitude", "").strip()
    if not latitude or not longitude:
        return None, None
    try:
        latitude_value, longitude_value = Decimal(latitude), Decimal(longitude)
    except InvalidOperation:
        return None, None
    if not Decimal("-90") <= latitude_value <= Decimal("90") or not Decimal("-180") <= longitude_value <= Decimal("180"):
        return None, None
    return latitude_value, longitude_value


def _attendance_face_check(request, intern):
    image_data = request.POST.get("face_image", "").strip()
    if not image_data:
        return AttendanceLog.FaceCheckStatus.NOT_CAPTURED, None

    try:
        image = decode_camera_image(image_data)
    except FaceCaptureError as exc:
        LOGGER.warning("Could not read face image for intern %s: %s", intern.pk, exc)
        return AttendanceLog.FaceCheckStatus.ERROR, None

    enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    if enrollment is None:
        return AttendanceLog.FaceCheckStatus.NOT_ENROLLED, None

    try:
        result = compare_face_embedding(image, enrollment.encrypted_embedding)
    except FaceImageError as exc:
        LOGGER.info("Face check unavailable for intern %s: %s", intern.pk, exc)
        return AttendanceLog.FaceCheckStatus.UNAVAILABLE, None
    except FaceModelError:
        LOGGER.exception("Face check failed for intern %s.", intern.pk)
        return AttendanceLog.FaceCheckStatus.ERROR, None

    return result["status"], result["similarity"]


@role_required(User.Role.INTERN)
def attendance(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    ojt_ready = baseline_ojt_requirements_approved(intern)
    today = timezone.localdate()
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"clock_in", "clock_out"}:
            return HttpResponseBadRequest("Unknown attendance action.")
        if action == "clock_in" and not ojt_ready:
            messages.error(request, "Your required OJT documents must be approved before you can start attendance.")
            return redirect("portal:ojt_requirements")
        latitude, longitude = _coordinates(request, action)
        face_status, face_score = _attendance_face_check(request, intern)
        try:
            with transaction.atomic():
                if action == "clock_out":
                    log = AttendanceLog.objects.select_for_update().filter(intern=intern, work_date=today).first()
                    if not log or not log.clock_in:
                        messages.error(request, "Record time in before recording time out.")
                        return redirect("portal:attendance")
                else:
                    log, _ = AttendanceLog.objects.select_for_update().get_or_create(intern=intern, work_date=today)
                if action == "clock_in":
                    if log.clock_in:
                        messages.info(request, "You have already recorded time in today.")
                    else:
                        log.clock_in = timezone.now()
                        log.clock_in_latitude, log.clock_in_longitude = latitude, longitude
                        log.clock_in_face_status = face_status
                        log.clock_in_face_score = face_score
                        log.notes = request.POST.get("notes", "").strip()[:255]
                        log.save(update_fields=(
                            "clock_in",
                            "clock_in_latitude",
                            "clock_in_longitude",
                            "clock_in_face_status",
                            "clock_in_face_score",
                            "notes",
                        ))
                        audit(request.user, "attendance.clocked_in", log)
                        messages.success(
                            request,
                            f"Time in recorded using server time. Face check (pilot): {log.get_clock_in_face_status_display()}.",
                        )
                elif log.clock_out:
                    messages.info(request, "You have already recorded time out today.")
                else:
                    log.clock_out = timezone.now()
                    log.clock_out_latitude, log.clock_out_longitude = latitude, longitude
                    log.clock_out_face_status = face_status
                    log.clock_out_face_score = face_score
                    log.notes = request.POST.get("notes", "").strip()[:255] or log.notes
                    log.save(update_fields=(
                        "clock_out",
                        "clock_out_latitude",
                        "clock_out_longitude",
                        "clock_out_face_status",
                        "clock_out_face_score",
                        "notes",
                    ))
                    audit(request.user, "attendance.clocked_out", log)
                    messages.success(
                        request,
                        f"Time out recorded using server time. Face check (pilot): {log.get_clock_out_face_status_display()}.",
                    )
        except IntegrityError:
            messages.info(request, "Today's attendance record was already created. Refresh and try again.")
        return redirect("portal:attendance")

    today_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
    logs = intern.attendance_logs.all()[:30]
    return render(request, "portal/intern/attendance.html", {
        "today_log": today_log,
        "logs": logs,
        "today": today,
        "ojt_ready": ojt_ready,
    })


@role_required(User.Role.COORDINATOR)
def coordinator_face_enrollment(request):
    if request.method == "POST":
        action = request.POST.get("action")
        intern = get_object_or_404(InternProfile.objects.select_related("user"), pk=request.POST.get("intern_id"))
        if action == "delete":
            enrollment = FaceEnrollment.objects.filter(intern=intern).first()
            if enrollment:
                with transaction.atomic():
                    enrollment.delete()
                    AttendanceLog.objects.filter(intern=intern).update(
                        clock_in_face_status=AttendanceLog.FaceCheckStatus.NOT_ATTEMPTED,
                        clock_in_face_score=None,
                        clock_out_face_status=AttendanceLog.FaceCheckStatus.NOT_ATTEMPTED,
                        clock_out_face_score=None,
                    )
                audit(request.user, "face_enrollment.deleted", intern)
                messages.success(request, "Face enrollment and its recorded pilot scores were deleted.")
            else:
                messages.info(request, "This intern has no face enrollment.")
            return redirect("portal:coordinator_face_enrollment")
        if action != "enroll":
            return HttpResponseBadRequest("Unknown face-enrollment action.")
        if request.POST.get("consent_confirmed") != "on":
            messages.error(request, "Confirm that the intern gave informed consent before enrolling.")
            return redirect("portal:coordinator_face_enrollment")

        try:
            image = decode_camera_image(request.POST.get("face_image", ""))
            embedding = encrypt_embedding(create_face_embedding(image))
        except (FaceCaptureError, FaceImageError) as exc:
            messages.error(request, str(exc))
            return redirect("portal:coordinator_face_enrollment")
        except FaceModelError:
            LOGGER.exception("Face enrollment failed for intern %s.", intern.pk)
            messages.error(request, "Face enrollment could not be completed because the models are unavailable.")
            return redirect("portal:coordinator_face_enrollment")

        enrollment, created = FaceEnrollment.objects.update_or_create(
            intern=intern,
            defaults={
                "encrypted_embedding": embedding,
                "enrolled_by": request.user,
                "consent_confirmed_at": timezone.now(),
                "consent_text_version": "v1",
            },
        )
        audit(request.user, "face_enrollment.created" if created else "face_enrollment.updated", intern)
        messages.success(request, "Encrypted face embedding saved; the camera image was not stored.")
        return redirect("portal:coordinator_face_enrollment")

    interns = InternProfile.objects.select_related("user").prefetch_related("face_enrollment").order_by(
        "user__last_name",
        "user__first_name",
        "user__username",
    )
    return render(request, "portal/coordinator/face_enrollment.html", {"interns": interns})


def _ojt_requirements_context(intern, bound_requirement=None, bound_form=None):
    requirements = intern.ojt_requirements.all()
    rows = []
    for requirement in requirements:
        rows.append({
            "requirement": requirement,
            "form": bound_form if requirement.pk == getattr(bound_requirement, "pk", None) else OJTRequirementUploadForm(),
        })
    required = requirements.filter(is_required=True, company__isnull=True)
    approved_required = required.filter(status=OJTRequirement.Status.APPROVED).count()
    return {
        "requirement_rows": rows,
        "required_count": required.count(),
        "approved_required_count": approved_required,
        "ojt_ready": baseline_ojt_requirements_approved(intern),
    }


@role_required(User.Role.INTERN)
def ojt_requirements(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    return render(
        request,
        "portal/intern/requirements.html",
        _ojt_requirements_context(intern),
    )


@role_required(User.Role.INTERN)
@require_POST
def ojt_requirement_upload(request, requirement_id):
    intern = get_object_or_404(InternProfile, user=request.user)
    requirement = get_object_or_404(intern.ojt_requirements, pk=requirement_id)
    if requirement.status in {OJTRequirement.Status.SUBMITTED, OJTRequirement.Status.APPROVED}:
        messages.info(request, "This document is already awaiting review or has been approved.")
        return redirect("portal:ojt_requirements")

    form = OJTRequirementUploadForm(request.POST, request.FILES, instance=requirement)
    if form.is_valid():
        requirement = form.save(commit=False)
        requirement.status = OJTRequirement.Status.SUBMITTED
        requirement.reviewer = None
        requirement.review_note = ""
        requirement.submitted_at = timezone.now()
        requirement.reviewed_at = None
        requirement.save()
        audit(request.user, "ojt_requirement.submitted", requirement)
        messages.success(request, f"{requirement.title} uploaded for coordinator review.")
        return redirect("portal:ojt_requirements")

    return render(
        request,
        "portal/intern/requirements.html",
        _ojt_requirements_context(intern, requirement, form),
    )


@role_required(User.Role.COORDINATOR)
def coordinator_ojt_requirements(request):
    requirements = OJTRequirement.objects.filter(
        status=OJTRequirement.Status.SUBMITTED
    ).select_related("intern__user").order_by("submitted_at")
    return render(request, "portal/coordinator/requirements.html", {
        "requirements": _mark_requirement_preview_support(requirements),
    })


@role_required(User.Role.COORDINATOR)
@require_POST
def review_ojt_requirement(request, requirement_id):
    requirement = get_object_or_404(
        OJTRequirement.objects.select_related("intern__user"),
        pk=requirement_id,
        status=OJTRequirement.Status.SUBMITTED,
    )
    decision = request.POST.get("decision")
    if decision not in {"approve", "reject"}:
        return HttpResponseBadRequest("Unknown document review decision.")
    review_note = request.POST.get("review_note", "").strip()
    if decision == "reject" and not review_note:
        messages.error(request, "Add a note explaining what the intern needs to correct.")
        return redirect("portal:coordinator_ojt_requirements")

    requirement.status = (
        OJTRequirement.Status.APPROVED if decision == "approve" else OJTRequirement.Status.REJECTED
    )
    requirement.reviewer = request.user
    requirement.review_note = review_note
    requirement.reviewed_at = timezone.now()
    requirement.save(update_fields=("status", "reviewer", "review_note", "reviewed_at"))
    if decision == "approve" and requirement.company_id is None:
        assign_accepted_company_ojt_requirements(requirement.intern)
    outcome = "approved" if decision == "approve" else "rejected"
    audit(request.user, f"ojt_requirement.{outcome}", requirement)
    messages.success(request, f"{requirement.title} {outcome}.")
    return redirect("portal:coordinator_ojt_requirements")


@login_required
def ojt_requirement_document(request, requirement_id, mode="download"):
    if mode not in {"preview", "download"}:
        return HttpResponseBadRequest("Invalid document action.")
    requirement = get_object_or_404(OJTRequirement, pk=requirement_id)
    can_review = request.user.is_staff or request.user.role == User.Role.COORDINATOR
    is_owner = (
        request.user.role == User.Role.INTERN
        and requirement.intern.user_id == request.user.pk
    )
    can_view_as_company = False
    if request.user.role == User.Role.COMPANY and requirement.intern.placement_type == InternProfile.PlacementType.PLATFORM:
        company = get_object_or_404(CompanyProfile, user=request.user)
        can_view_as_company = (
            requirement.intern.applications.filter(
                posting__company=company,
                status=Application.Status.ACCEPTED,
            ).exists()
            and requirement.company_id in (None, company.pk)
        )
    if not (can_review or is_owner or can_view_as_company):
        return render(request, "errors/403.html", status=403)
    if not requirement.document:
        raise Http404("No document has been uploaded.")

    content_type = mimetypes.guess_type(requirement.document.name)[0] or "application/octet-stream"
    is_docx = Path(requirement.document.name).suffix.lower() == ".docx"
    if mode == "preview" and is_docx:
        try:
            with requirement.document.open("rb") as document_file:
                document = Document(document_file)
        except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError, ValueError, OSError):
            return HttpResponseBadRequest("This DOCX document could not be previewed. Download the original file instead.")

        preview_blocks = _docx_preview_blocks(document)
        response = render(request, "portal/company/document_preview.html", {
            "title": requirement.title,
            "intern": requirement.intern,
            "filename": Path(requirement.document.name).name,
            "preview_blocks": preview_blocks,
            "archive_entries": None,
            "download_url": reverse("portal:ojt_requirement_document", args=(requirement.pk,)),
        })
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; sandbox"
        return response

    is_zip = Path(requirement.document.name).suffix.lower() == ".zip"
    if mode == "preview" and is_zip:
        try:
            with requirement.document.open("rb") as document_file:
                with ZipFile(document_file) as archive:
                    entries = archive.infolist()
                    archive_entries = [
                        {"name": entry.filename, "size": entry.file_size}
                        for entry in entries[:200]
                        if not entry.is_dir()
                    ]
        except (BadZipFile, LargeZipFile, OSError):
            return HttpResponseBadRequest("This ZIP document could not be previewed. Download the original file instead.")

        response = render(request, "portal/company/document_preview.html", {
            "title": requirement.title,
            "intern": requirement.intern,
            "filename": Path(requirement.document.name).name,
            "preview_blocks": [],
            "archive_entries": archive_entries,
            "archive_entry_count": len(entries),
            "archive_truncated": len(entries) > 200,
            "download_url": reverse("portal:ojt_requirement_document", args=(requirement.pk,)),
        })
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; sandbox"
        return response

    preview_content_types = {"application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain"}
    if mode == "preview" and content_type not in preview_content_types:
        return HttpResponseBadRequest("This document type cannot be previewed in a browser. Download the original file instead.")

    response = FileResponse(
        requirement.document.open("rb"),
        as_attachment=mode == "download",
        filename=Path(requirement.document.name).name,
        content_type=content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def _docx_preview_blocks(document):
    preview_blocks = []
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            if item.text.strip():
                preview_blocks.append({
                    "kind": "paragraph",
                    "text": item.text,
                    "heading": item.style.name == "Title" or item.style.name.startswith("Heading"),
                })
        elif isinstance(item, Table):
            rows = [[cell.text for cell in row.cells] for row in item.rows]
            if any(text.strip() for row in rows for text in row):
                preview_blocks.append({"kind": "table", "rows": rows})
    return preview_blocks


@role_required(User.Role.INTERN)
def attendance_export(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    document = attendance_pdf(intern, intern.attendance_logs.all()[:90])
    return FileResponse(document, as_attachment=True, filename="attendance-record.pdf", content_type="application/pdf")


@role_required(User.Role.INTERN)
def reports(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
        messages.info(request, "External placements submit a daily report to the OJT coordinator.")
        return redirect("portal:daily_report")
    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    existing = WeeklyReport.objects.filter(intern=intern, week_start=week_start).first()
    form = WeeklyReportForm(request.POST or None, instance=existing)
    if request.method == "POST" and form.is_valid():
        report = form.save(commit=False)
        report.intern = intern
        report.week_start = week_start
        report.status = WeeklyReport.Status.SUBMITTED
        report.submitted_at = timezone.now()
        try:
            with transaction.atomic():
                report.save()
        except IntegrityError:
            messages.error(request, "A report for this week already exists. Refresh and edit the saved report.")
        else:
            audit(request.user, "weekly_report.submitted", report)
            messages.success(request, "Weekly report submitted.")
            return redirect(f"{reverse('portal:reports')}?saved=1")
    return render(request, "portal/intern/reports.html", {"form": form, "reports": intern.weekly_reports.all()[:12], "week_start": week_start})


@role_required(User.Role.INTERN)
def daily_report(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    if intern.placement_type != InternProfile.PlacementType.EXTERNAL:
        messages.info(request, "Daily admin reports are for external placements. Use your weekly reports instead.")
        return redirect("portal:reports")
    today = timezone.localdate()
    existing = DailyReport.objects.filter(intern=intern, work_date=today).first()
    if request.method == "POST" and existing and existing.status == DailyReport.Status.REVIEWED:
        messages.info(request, "Today's report has already been reviewed and cannot be changed.")
        return redirect("portal:daily_report")
    if existing and existing.status == DailyReport.Status.REVIEWED:
        form = DailyReportForm(instance=existing)
        form.fields["accomplishments"].disabled = True
        form.fields["challenges"].disabled = True
    else:
        form = DailyReportForm(request.POST or None, instance=existing)
    if request.method == "POST" and form.is_valid():
        report = form.save(commit=False)
        report.intern = intern
        report.work_date = today
        report.status = DailyReport.Status.SUBMITTED
        report.supervisor_feedback = ""
        report.reviewer = None
        report.submitted_at = timezone.now()
        report.reviewed_at = None
        try:
            with transaction.atomic():
                report.save()
        except IntegrityError:
            messages.error(request, "Today's report was just saved. Refresh and try again.")
        else:
            audit(request.user, "daily_report.submitted", report)
            messages.success(request, "Daily report submitted to your OJT coordinator.")
            return redirect("portal:daily_report")
    return render(request, "portal/intern/daily_report.html", {
        "form": form,
        "report": existing,
        "today": today,
        "reports": intern.daily_reports.select_related("reviewer")[:14],
    })


@role_required(User.Role.COORDINATOR)
def coordinator_daily_reports(request):
    reports = DailyReport.objects.select_related(
        "intern__user",
        "intern",
        "reviewer",
    ).filter(
        status__in=(DailyReport.Status.SUBMITTED, DailyReport.Status.CHANGES_REQUESTED)
    ).order_by("work_date", "intern__user__last_name")
    return render(request, "portal/coordinator/daily_reports.html", {
        "reports": reports[:100],
        "pending_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
    })


@role_required(User.Role.COORDINATOR)
def coordinator_weekly_reports(request):
    reports = WeeklyReport.objects.select_related(
        "intern__user",
        "intern",
    ).filter(
        intern__placement_type=InternProfile.PlacementType.PLATFORM,
    )[:100]
    return render(request, "portal/coordinator/weekly_reports.html", {
        "reports": reports,
    })


@role_required(User.Role.COORDINATOR)
@require_POST
def review_daily_report(request, report_id):
    report = get_object_or_404(
        DailyReport,
        pk=report_id,
        status__in=(DailyReport.Status.SUBMITTED, DailyReport.Status.CHANGES_REQUESTED),
    )
    decision = request.POST.get("decision")
    if decision not in {"review", "request_changes"}:
        return HttpResponseBadRequest("Unknown daily report review decision.")
    feedback = request.POST.get("supervisor_feedback", "").strip()
    if decision == "request_changes" and not feedback:
        messages.error(request, "Add feedback describing the requested changes.")
        return redirect("portal:coordinator_daily_reports")

    report.status = (
        DailyReport.Status.REVIEWED if decision == "review" else DailyReport.Status.CHANGES_REQUESTED
    )
    report.reviewer = request.user
    report.supervisor_feedback = feedback
    report.reviewed_at = timezone.now()
    report.save(update_fields=("status", "reviewer", "supervisor_feedback", "reviewed_at"))
    audit(
        request.user,
        "daily_report.reviewed" if decision == "review" else "daily_report.changes_requested",
        report,
    )
    messages.success(request, "Daily report review saved.")
    return redirect("portal:coordinator_daily_reports")


@role_required(User.Role.INTERN)
def accreditation(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    completed_hours = sum(
        log.hours_worked
        for log in intern.attendance_logs.filter(time_in_approved=True, time_out_approved=True)
    )
    if completed_hours < OJT_TARGET_HOURS:
        messages.info(request, f"Accreditation becomes available after {OJT_TARGET_HOURS} approved hours.")
        return redirect("portal:intern_dashboard")
    document = certificate_pdf(intern)
    return FileResponse(document, as_attachment=True, filename="internship-certificate.pdf", content_type="application/pdf")


def accepted_company_interns(company):
    return InternProfile.objects.filter(
        placement_type=InternProfile.PlacementType.PLATFORM,
        applications__posting__company=company,
        applications__status=Application.Status.ACCEPTED,
    ).select_related("user").distinct()


def company_attendance_logs(company):
    return AttendanceLog.objects.filter(
        intern__in=accepted_company_interns(company),
    ).select_related("intern__user").order_by("-work_date").distinct()


def _attendance_csv_response(logs, filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(("Intern", "Student ID", "Date", "Time in", "Time out", "Hours", "Time in approval", "Time out approval"))
    for log in logs:
        writer.writerow((
            log.intern.user.get_full_name() or log.intern.user.username,
            log.intern.student_id or "",
            log.work_date.isoformat(),
            log.clock_in.isoformat() if log.clock_in else "",
            log.clock_out.isoformat() if log.clock_out else "",
            log.hours_worked,
            "Approved" if log.time_in_approved else "Pending",
            "Approved" if log.time_out_approved else "Pending",
        ))
    return response


@role_required(User.Role.COMPANY)
def company_attendance_export(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    return _attendance_csv_response(
        company_attendance_logs(company),
        "supervisor-attendance-summary.csv",
    )


@role_required(User.Role.COMPANY)
def company_dashboard(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    company_applications = Application.objects.filter(posting__company=company)
    pending_applications = company_applications.filter(
        status__in=(Application.Status.SUBMITTED, Application.Status.REVIEWING)
    )
    return render(request, "portal/company/dashboard.html", {
        "company": company,
        "postings": company.postings.all()[:8],
        "open_posting_count": company.postings.filter(status=Posting.Status.PUBLISHED).count(),
        "application_count": company_applications.count(),
        "pending_application_count": pending_applications.count(),
        "pending_applications": pending_applications.select_related("intern__user", "posting")[:8],
        "pending_dtr_count": company_attendance_logs(company).filter(
            Q(clock_in__isnull=False, time_in_approved=False)
            | Q(clock_out__isnull=False, time_out_approved=False)
        ).count(),
    })


@role_required(User.Role.COMPANY)
def company_profile(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    form = CompanyProfileForm(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        company = form.save()
        audit(request.user, "company_profile.updated", company)
        messages.success(request, "Company profile saved.")
        return redirect("portal:company_profile")
    return render(request, "portal/company/profile.html", {"form": form, "company": company})


@role_required(User.Role.COMPANY)
def company_requirements(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    if request.method == "POST" and "delete_requirement" in request.POST:
        requirement = get_object_or_404(
            CompanyRequirement,
            pk=request.POST["delete_requirement"],
            company=company,
        )
        audit(request.user, "company_requirement.deleted", requirement)
        requirement.delete()
        messages.success(request, "Company requirement removed.")
        return redirect("portal:company_requirements")

    form = CompanyRequirementForm(request.POST or None)
    form.instance.company = company
    if request.method == "POST" and form.is_valid():
        requirement = form.save(commit=False)
        requirement.company = company
        try:
            with transaction.atomic():
                requirement.save()
                accepted_interns = InternProfile.objects.filter(
                    placement_type=InternProfile.PlacementType.PLATFORM,
                    applications__posting__company=company,
                    applications__status=Application.Status.ACCEPTED,
                ).distinct()
                for intern in accepted_interns:
                    assign_company_ojt_requirements(intern, company)
        except IntegrityError:
            messages.error(request, "That requirement already exists. Refresh and try again.")
        else:
            audit(request.user, "company_requirement.created", requirement)
            messages.success(request, "Company requirement saved.")
            return redirect("portal:company_requirements")
    return render(request, "portal/company/requirements.html", {
        "company": company,
        "requirements": company.requirements.all(),
        "form": form,
    })


@role_required(User.Role.COMPANY)
def posting_create(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    form = PostingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        posting = form.save(commit=False)
        posting.company = company
        posting.save()
        audit(request.user, "posting.created", posting)
        messages.success(request, "Opportunity saved.")
        return redirect("portal:company_dashboard")
    return render(request, "portal/company/posting_form.html", {"form": form})


@role_required(User.Role.COMPANY)
def company_applications(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    applications = _mark_resume_preview_support(
        Application.objects.filter(posting__company=company).select_related("intern__user", "posting")
    )
    return render(request, "portal/company/applications.html", {"applications": applications})


def _mark_resume_preview_support(applications):
    preview_content_types = {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    for application in applications:
        content_type = mimetypes.guess_type(application.resume.name)[0] if application.resume else None
        application.resume_previewable = content_type in preview_content_types
    return applications


def _mark_requirement_preview_support(requirements):
    preview_content_types = {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    }
    for requirement in requirements:
        content_type = mimetypes.guess_type(requirement.document.name)[0] if requirement.document else None
        requirement.document_previewable = (
            content_type in preview_content_types
            or Path(requirement.document.name).suffix.lower() in {".docx", ".zip"}
        ) if requirement.document else False
    return requirements


@role_required(User.Role.COMPANY)
def company_intern_documents(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    requirements = OJTRequirement.objects.filter(
        intern__placement_type=InternProfile.PlacementType.PLATFORM,
        intern__applications__posting__company=company,
        intern__applications__status=Application.Status.ACCEPTED,
        document__gt="",
    ).filter(
        Q(company__isnull=True) | Q(company=company)
    ).select_related("intern__user", "company").distinct().order_by(
        "intern__user__last_name",
        "intern__user__first_name",
        "title",
    )
    return render(request, "portal/company/intern_documents.html", {
        "requirements": _mark_requirement_preview_support(requirements[:200]),
    })


@role_required(User.Role.COMPANY)
def company_application_resume(request, application_id, mode):
    if mode not in {"preview", "download"}:
        return HttpResponseBadRequest("Invalid resume action.")
    company = get_object_or_404(CompanyProfile, user=request.user)
    application = get_object_or_404(
        Application.objects.select_related("posting"),
        pk=application_id,
        posting__company=company,
    )
    if not application.resume:
        return HttpResponseBadRequest("This application has no resume.")

    content_type = mimetypes.guess_type(application.resume.name)[0] or "application/octet-stream"
    docx_content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if mode == "preview" and (content_type == docx_content_type or Path(application.resume.name).suffix.lower() == ".docx"):
        try:
            with application.resume.open("rb") as resume_file:
                document = Document(resume_file)
        except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError, ValueError, OSError):
            return HttpResponseBadRequest("This DOCX resume could not be previewed. Download the original file instead.")

        preview_blocks = []
        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                if item.text.strip():
                    preview_blocks.append({
                        "kind": "paragraph",
                        "text": item.text,
                        "heading": item.style.name == "Title" or item.style.name.startswith("Heading"),
                    })
            elif isinstance(item, Table):
                rows = [[cell.text for cell in row.cells] for row in item.rows]
                if any(text.strip() for row in rows for text in row):
                    preview_blocks.append({"kind": "table", "rows": rows})

        response = render(request, "portal/company/resume_preview.html", {
            "application": application,
            "preview_blocks": preview_blocks,
        })
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; sandbox"
        return response

    preview_content_types = {"application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain"}
    if mode == "preview" and content_type not in preview_content_types:
        return HttpResponseBadRequest("This resume file type cannot be previewed in a browser.")

    response = FileResponse(
        application.resume.open("rb"),
        as_attachment=mode == "download",
        filename=Path(application.resume.name).name,
        content_type=content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@role_required(User.Role.COMPANY)
def company_dtr_queue(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    logs = company_attendance_logs(company)
    return render(request, "portal/company/dtr_queue.html", {
        "logs": logs[:100],
        "pending_dtr_count": logs.filter(
            Q(clock_in__isnull=False, time_in_approved=False)
            | Q(clock_out__isnull=False, time_out_approved=False)
        ).count(),
    })


@role_required(User.Role.COMPANY)
@require_POST
def company_approve_dtr(request, log_id):
    company = get_object_or_404(CompanyProfile, user=request.user)
    log = get_object_or_404(company_attendance_logs(company), pk=log_id)
    return _approve_attendance_time(request, log, "portal:company_dtr_queue", "attendance.approved_by_company")


def _approve_attendance_time(request, log, queue_name, audit_action):
    approval = {
        "time_in": ("clock_in", "time_in_approved", "Time in"),
        "time_out": ("clock_out", "time_out_approved", "Time out"),
    }.get(request.POST.get("event"))
    if approval is None:
        messages.error(request, "Choose whether to approve time in or time out.")
        return redirect(queue_name)

    timestamp_field, approval_field, label = approval
    if not getattr(log, timestamp_field):
        messages.error(request, f"{label} has not been recorded yet.")
    elif getattr(log, approval_field):
        messages.info(request, f"{label} is already approved.")
    else:
        setattr(log, approval_field, True)
        log.save(update_fields=(approval_field,))
        audit(request.user, audit_action, log, event=request.POST["event"])
        messages.success(request, f"{label} approved.")
    return redirect(queue_name)


@role_required(User.Role.COMPANY)
def company_scorecards(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    interns = accepted_company_interns(company).order_by("user__last_name", "user__first_name")
    selected_intern = None
    week_start = request.GET.get("week", "")
    form = ScorecardForm()
    if request.method == "POST":
        selected_intern = get_object_or_404(interns, pk=request.POST.get("intern_id"))
        try:
            week_start = date.fromisoformat(request.POST.get("week_start", ""))
        except (TypeError, ValueError):
            messages.error(request, "Choose a valid week start date.")
        else:
            scorecard = Scorecard.objects.filter(
                company=company,
                intern=selected_intern,
                week_start=week_start,
            ).first()
            form = ScorecardForm(request.POST, instance=scorecard)
            if form.is_valid():
                scorecard = form.save(commit=False)
                scorecard.company = company
                scorecard.reviewer = request.user
                scorecard.intern = selected_intern
                scorecard.week_start = week_start
                try:
                    with transaction.atomic():
                        scorecard.save()
                except IntegrityError:
                    messages.error(request, "A scorecard for this intern and week was just saved. Refresh before editing.")
                else:
                    audit(request.user, "scorecard.saved", scorecard, company_id=company.pk)
                    messages.success(request, "Scorecard saved.")
                    return redirect("portal:company_scorecards")
    return render(request, "portal/company/scorecards.html", {
        "company": company,
        "interns": interns,
        "scorecards": Scorecard.objects.filter(company=company).select_related("intern__user", "reviewer")[:50],
        "form": form,
        "selected_intern": selected_intern,
        "week_start": week_start,
    })


@role_required(User.Role.COMPANY)
def applicant_screening(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    pending_statuses = (Application.Status.SUBMITTED, Application.Status.REVIEWING)
    pending = _mark_resume_preview_support(
        Application.objects.filter(posting__company=company, status__in=pending_statuses)
        .select_related("intern__user", "posting")
        .order_by("submitted_at")
        [:100]
    )
    return render(request, "portal/company/screening.html", {
        "pending": pending,
        "pending_count": pending.count(),
    })


@role_required(User.Role.COMPANY)
@require_POST
def application_decide(request, application_id, decision):
    company = get_object_or_404(CompanyProfile, user=request.user)
    application = get_object_or_404(
        Application.objects.select_related("intern__user", "posting"),
        pk=application_id,
        posting__company=company,
    )
    decisions = {
        "accept": Application.Status.ACCEPTED,
        "reject": Application.Status.REJECTED,
    }
    if decision not in decisions:
        return HttpResponseBadRequest("Invalid application decision.")
    if application.status not in (Application.Status.SUBMITTED, Application.Status.REVIEWING):
        messages.warning(request, "This application has already been reviewed.")
        return redirect("portal:applicant_screening")

    application.status = decisions[decision]
    application.save(update_fields=("status", "updated_at"))
    if application.status == Application.Status.ACCEPTED:
        assign_company_ojt_requirements(application.intern, company)
    audit(request.user, f"application.{application.status}", application)
    messages.success(request, f"Application {application.get_status_display().lower()}.")
    return redirect("portal:applicant_screening")


@role_required(User.Role.COORDINATOR)
def coordinator_dashboard(request):
    interns = InternProfile.objects.select_related("user").prefetch_related(
        "applications__posting__company",
        "risk_assessments",
        Prefetch(
            "attendance_logs",
            queryset=AttendanceLog.objects.filter(
                time_in_approved=True,
                time_out_approved=True,
            ).only("intern_id", "clock_in", "clock_out"),
        ),
    ).order_by("user__last_name", "user__first_name")[:100]
    intern_rows = []
    for intern in interns:
        application = next(iter(intern.applications.all()), None)
        risk = next(iter(intern.risk_assessments.all()), None)
        if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
            placement = intern.external_host
        else:
            placement = application.posting.company.organization if application else "No placement selected"
        completed_hours = round(sum(log.hours_worked for log in intern.attendance_logs.all()), 2)
        intern_rows.append({
            "intern": intern,
            "company": placement,
            "company_search": placement.lower(),
            "placement_type": intern.get_placement_type_display(),
            "status": application.status if application else "none",
            "status_display": application.get_status_display() if application else "No application",
            "risk": risk.level if risk else "none",
            "risk_display": risk.get_level_display() if risk else "No assessment",
            "risk_score": risk.risk_score if risk else None,
            "completed_hours": completed_hours,
            "progress_percent": min(100, round(completed_hours / OJT_TARGET_HOURS * 100)),
        })
    context = {
        "intern_count": InternProfile.objects.count(),
        "company_count": CompanyProfile.objects.count(),
        "open_posting_count": Posting.objects.filter(status=Posting.Status.PUBLISHED).count(),
        "pending_dtr_count": AttendanceLog.objects.filter(
            Q(clock_in__isnull=False, time_in_approved=False)
            | Q(clock_out__isnull=False, time_out_approved=False)
        ).count(),
        "pending_report_count": WeeklyReport.objects.filter(status=WeeklyReport.Status.SUBMITTED).count(),
        "pending_daily_report_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
        "pending_ojt_requirement_count": OJTRequirement.objects.filter(
            status=OJTRequirement.Status.SUBMITTED
        ).count(),
        "high_risk_count": RiskAssessment.objects.filter(level=RiskAssessment.Level.HIGH).count(),
        "intern_rows": intern_rows,
    }
    return render(request, "portal/coordinator/dashboard.html", context)


@role_required(User.Role.COORDINATOR)
def coordinator_attendance_export(request):
    logs = AttendanceLog.objects.select_related("intern__user").order_by("-work_date", "intern__user__last_name")
    return _attendance_csv_response(logs, "program-attendance-summary.csv")


@role_required(User.Role.COORDINATOR)
def audit_logs(request):
    logs = AuditLog.objects.select_related("actor").all()[:100]
    return render(request, "portal/admin/audit.html", {"logs": logs})


@role_required(User.Role.COORDINATOR)
def company_verification(request):
    companies = CompanyProfile.objects.select_related("user").order_by("verified", "organization")
    return render(request, "portal/admin/verify.html", {
        "companies": companies[:100],
        "unverified_count": companies.filter(verified=False).count(),
    })


@role_required(User.Role.COORDINATOR)
@require_POST
def verify_company(request, company_id, decision):
    if decision not in ("verify", "unverify"):
        return HttpResponseBadRequest("Invalid verification decision.")
    company = get_object_or_404(CompanyProfile, pk=company_id)
    company.verified = decision == "verify"
    company.save(update_fields=("verified",))
    audit(request.user, "company.verified" if company.verified else "company.unverified", company)
    messages.success(request, f"{company.organization} {decision} status updated.")
    return redirect("portal:admin_verify")


@role_required(User.Role.COORDINATOR)
def dtr_queue(request):
    logs = AttendanceLog.objects.select_related("intern__user").order_by("-work_date")
    return render(request, "portal/coordinator/dtr_queue.html", {"logs": logs[:100]})


@role_required(User.Role.COORDINATOR)
@require_POST
def approve_dtr(request, log_id):
    log = get_object_or_404(AttendanceLog, pk=log_id)
    return _approve_attendance_time(request, log, "portal:dtr_queue", "attendance.approved")


@role_required(User.Role.COORDINATOR)
def scorecards(request):
    interns = InternProfile.objects.select_related("user").order_by("user__last_name", "user__first_name")
    selected_intern = None
    week_start = request.GET.get("week", "")
    form = ScorecardForm()
    if request.method == "POST":
        selected_intern = get_object_or_404(InternProfile, pk=request.POST.get("intern_id"))
        try:
            week_start = date.fromisoformat(request.POST.get("week_start", ""))
        except ValueError:
            messages.error(request, "Choose a valid week start date.")
        else:
            scorecard = Scorecard.objects.filter(
                company__isnull=True,
                intern=selected_intern,
                week_start=week_start,
            ).first()
            form = ScorecardForm(request.POST, instance=scorecard)
            if form.is_valid():
                scorecard = form.save(commit=False)
                scorecard.reviewer = request.user
                scorecard.intern = selected_intern
                scorecard.week_start = week_start
                try:
                    with transaction.atomic():
                        scorecard.save()
                except IntegrityError:
                    messages.error(request, "A scorecard for this intern and week was just saved. Refresh before editing.")
                else:
                    audit(request.user, "scorecard.saved", scorecard)
                    messages.success(request, "Scorecard saved.")
                    return redirect("portal:scorecards")
    return render(request, "portal/coordinator/scorecards.html", {"interns": interns, "scorecards": Scorecard.objects.filter(company__isnull=True).select_related("intern__user", "reviewer")[:50], "form": form, "selected_intern": selected_intern, "week_start": week_start})


@role_required(User.Role.COORDINATOR)
def analytics(request):
    context = {
        "applications_by_status": Application.objects.values("status").annotate(total=Count("id")).order_by("status"),
        "attendance_by_status": AttendanceLog.objects.values("status").annotate(total=Count("id")).order_by("status"),
        "average_score": Scorecard.objects.aggregate(value=Avg("technical")) ["value"],
        "risk_by_level": RiskAssessment.objects.values("level").annotate(total=Count("id")).order_by("level"),
        "risk_assessments": RiskAssessment.objects.select_related("intern__user").order_by("-assessment_date", "-risk_score")[:50],
    }
    return render(request, "portal/coordinator/analytics.html", context)