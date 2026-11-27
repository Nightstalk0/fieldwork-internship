import csv
import hashlib
import logging
import mimetypes
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import numpy as np
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature, SignatureExpired, dumps, loads
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.core.paginator import Paginator
from django.views.decorators.http import require_http_methods, require_POST
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from zipfile import BadZipFile, LargeZipFile, ZipFile

from accounts.models import User

from .forms import ApplicationSubmissionForm, CompanyProfileForm, DailyReportForm, DailyReportReviewForm, InternProfileForm, InternWorksiteRequestForm, PostingForm, ScorecardForm, ScorecardSelectionForm, WorksiteLocationForm
from .models import AcademicPeriod, Application, AttendanceFaceCapture, AttendanceLog, AuditLog, CompanyProfile, DailyReport, FACE_CONSENT_VERSION, FaceEnrollment, FaceEnrollmentSubmission, FaceReenrollmentRequest, INSTITUTION_NAME, InternProfile, OJT_RENDERED_HOURS_CAP, Posting, RiskAssessment, Scorecard, WorksiteLocation, pending_attendance_approval_count
from .utils import attendance_pdf, audit, certificate_pdf, haversine_distance_km
from ml_engine.face_capture import FaceCaptureError, decode_camera_image, encode_camera_image
from ml_engine.face_recognition import (
    COSINE_MATCH_THRESHOLD,
    FaceImageError,
    FaceModelError,
    compare_face_embedding,
    cosine_similarity,
    create_face_embedding,
    decrypt_face_capture,
    decrypt_embedding,
    encrypt_face_capture,
    encrypt_embedding,
    inspect_face_candidates,
)

OJT_TARGET_HOURS = 120
LOGGER = logging.getLogger(__name__)


def _approved_hours_by_intern(intern_ids):
    duration = ExpressionWrapper(F("clock_out") - F("clock_in"), output_field=DurationField())
    daily_totals = AttendanceLog.objects.filter(
        intern_id__in=intern_ids,
    ).approved().values(
        "intern_id",
        "work_date",
    ).annotate(
        total_duration=Sum(duration),
    ).order_by("intern_id", "work_date")
    result = {}
    for row in daily_totals:
        total_duration = row["total_duration"]
        daily_hours = round(total_duration.total_seconds() / 3600, 2) if total_duration else 0
        intern_id = row["intern_id"]
        consumed_hours = min(
            daily_hours,
            max(OJT_RENDERED_HOURS_CAP - result.get(intern_id, 0), 0),
        )
        result[intern_id] = round(result.get(intern_id, 0) + consumed_hours, 2)
    return result


def _dtr_breakdown_context(intern, selected_period_id="all"):
    periods = list(AcademicPeriod.objects.all())
    selected_period = None
    if selected_period_id not in {"", "all", None}:
        selected_period = get_object_or_404(AcademicPeriod, pk=selected_period_id)

    all_logs = list(intern.attendance_logs.order_by("work_date", "pk"))
    approved_hours_by_day = {
        row["work_date"]: round(row["total_duration"].total_seconds() / 3600, 2)
        for row in AttendanceLog.objects.filter(intern=intern).approved_hours_by_day()
    }
    remaining_hours = float(OJT_RENDERED_HOURS_CAP)
    period_hours = {period.pk: 0.0 for period in periods}
    week_for_log = {}
    monday_dates = []

    for period in periods:
        period.display_label = f"{period.academic_year} · {period.get_period_display()}"

    for log in all_logs:
        approved_hours = approved_hours_by_day.get(log.work_date, 0)
        rendered_hours = min(approved_hours, remaining_hours)
        rendered_hours = round(max(rendered_hours, 0), 2)
        remaining_hours = round(max(remaining_hours - rendered_hours, 0), 2)
        log.rendered_hours = rendered_hours

        log.academic_period = next(
            (
                period
                for period in periods
                if period.starts_on <= log.work_date <= period.ends_on
            ),
            None,
        )
        if log.academic_period:
            period_hours[log.academic_period.pk] += rendered_hours

        week_start = log.work_date - timedelta(days=log.work_date.weekday())
        week_for_log[log.pk] = week_start
        monday_dates.append(week_start)
        log.week_day_number = log.work_date.weekday() + 1

    period_totals = [
        {
            "period": period,
            "hours": round(period_hours[period.pk], 2),
        }
        for period in periods
    ]
    overall_hours = round(OJT_RENDERED_HOURS_CAP - remaining_hours, 2)
    unassigned_period_hours = round(
        max(overall_hours - sum(period_hours.values()), 0),
        2,
    )
    if selected_period:
        display_logs = [
            log
            for log in all_logs
            if selected_period.starts_on <= log.work_date <= selected_period.ends_on
        ]
    else:
        display_logs = all_logs

    first_week = min(monday_dates) if monday_dates else None
    week_map = {}
    for log in display_logs:
        week_start = week_for_log[log.pk]
        week_map.setdefault(week_start, []).append(log)

    weekly_groups = []
    for week_start in sorted(week_map, reverse=True):
        week_logs = week_map[week_start]
        week_number = ((week_start - first_week).days // 7 + 1) if first_week else 1
        weekly_groups.append({
            "number": week_number,
            "start": week_start,
            "end": week_start + timedelta(days=6),
            "logs": list(reversed(week_logs)),
            "hours": round(sum(log.rendered_hours for log in week_logs), 2),
        })

    return {
        "weekly_groups": weekly_groups,
        "academic_periods": periods,
        "selected_academic_period": selected_period,
        "period_totals": period_totals,
        "unassigned_period_hours": unassigned_period_hours,
        "approved_rendered_hours": overall_hours,
        "completion_percent": round(overall_hours * 100 / OJT_RENDERED_HOURS_CAP, 1),
        "rendered_hours_cap": OJT_RENDERED_HOURS_CAP,
    }


def role_required(*roles):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            is_company_admin_route = (
                User.Role.COORDINATOR in roles
                and User.Role.COMPANY not in roles
                and request.user.role == User.Role.COMPANY
            )
            if is_company_admin_route or not (
                request.user.is_staff
                or request.user.is_superuser
                or request.user.role in roles
            ):
                return render(request, "errors/403.html", status=403)
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if request.user.role == User.Role.COMPANY or not request.user.is_admin:
            return render(request, "errors/403.html", status=403)
        return view(request, *args, **kwargs)

    return wrapped


def intern_api_required():
    def decorate(view):
        @wraps(view)
        @require_POST
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Your session expired. Sign in again and retry."}, status=401)
            if request.user.is_admin or request.user.role != User.Role.INTERN:
                return JsonResponse({"error": "Only interns can use face capture."}, status=403)

            if not InternProfile.objects.filter(user=request.user).exists():
                return JsonResponse({"error": "Complete your intern profile before face capture."}, status=404)
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    if request.user.role == User.Role.COMPANY:
        return redirect("portal:company_dashboard")
    if request.user.is_admin:
        return redirect("portal:coordinator_dashboard")
    return redirect("portal:intern_dashboard")


@role_required(User.Role.INTERN)
def intern_dashboard(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    completed_hours = _approved_hours_by_intern([intern.pk]).get(intern.pk, 0)
    context = {
        "intern": intern,
        "recent_attendance": intern.attendance_logs.all()[:7],
        "recent_daily_reports": intern.daily_reports.all()[:4],
        "today": timezone.localdate(),
        "completed_hours": completed_hours,
        "completion_percent": round(completed_hours * 100 / OJT_RENDERED_HOURS_CAP, 1),
        "rendered_hours_cap": OJT_RENDERED_HOURS_CAP,
    }
    return render(request, "portal/intern/dashboard.html", context)


@role_required(User.Role.INTERN)
def profile(request):
    if request.GET.get("section") == "worksite":
        return redirect("portal:intern_worksite_registration")

    intern = get_object_or_404(InternProfile, user=request.user)
    form = InternProfileForm(request.POST or None, request.FILES or None, instance=intern)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                profile_instance = form.save()
                audit(request.user, "profile.updated", profile_instance)
        except Exception:
            LOGGER.exception("Profile update failed for intern user %s.", request.user.pk)
            raise
        messages.success(request, "Profile saved.")
        return redirect("portal:profile")
    return render(request, "portal/intern/profile.html", _intern_profile_context(intern, form=form))


def _intern_profile_context(intern, *, form=None):
    enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    latest_face_submission = intern.face_enrollment_submissions.first()
    face_reenrollment_request = intern.face_reenrollment_requests.filter(
        status__in=(
            FaceReenrollmentRequest.Status.PENDING,
            FaceReenrollmentRequest.Status.APPROVED,
            FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
        ),
    ).first()
    latest_face_reenrollment_request = intern.face_reenrollment_requests.first()
    face_submission_pending = (
        latest_face_submission is not None
        and latest_face_submission.status == FaceEnrollmentSubmission.Status.PENDING
    )
    face_reenrollment_authorized = face_reenrollment_request is not None and (
        face_reenrollment_request.status
        in (
            FaceReenrollmentRequest.Status.APPROVED,
            FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
        )
    )
    face_submission_rejected = latest_face_submission is not None and (
        latest_face_submission.status
        in (
            FaceEnrollmentSubmission.Status.REJECTED,
            FaceEnrollmentSubmission.Status.RETAKE_REQUESTED,
        )
    )
    if face_submission_pending:
        face_status = "Pending Admin Review"
    elif face_reenrollment_request and face_reenrollment_request.status == FaceReenrollmentRequest.Status.PENDING:
        face_status = "Retake request pending Admin review"
    elif face_reenrollment_authorized:
        face_status = "Retake Requested"
    elif face_submission_rejected:
        face_status = (
            "Retake Requested"
            if latest_face_submission.status == FaceEnrollmentSubmission.Status.RETAKE_REQUESTED
            else "Rejected"
        )
    elif enrollment:
        face_status = "Enrolled / Approved"
    else:
        face_status = "Not Enrolled"
    can_submit_face = not face_submission_pending and not (
        face_reenrollment_request
        and face_reenrollment_request.status == FaceReenrollmentRequest.Status.PENDING
    ) and (
        enrollment is None
        or face_reenrollment_authorized
        or face_submission_rejected
    )
    can_request_face_reenrollment = enrollment is not None and not (
        face_submission_pending
        or face_submission_rejected
        or face_reenrollment_request is not None
    )
    return {
        "form": form or InternProfileForm(instance=intern),
        "intern": intern,
        "institution_name": INSTITUTION_NAME,
        "face_enrollment": enrollment,
        "face_enrollment_current": enrollment is not None and enrollment.consent_text_version == FACE_CONSENT_VERSION,
        "face_reenrollment_request": face_reenrollment_request,
        "latest_face_reenrollment_request": latest_face_reenrollment_request,
        "latest_face_submission": latest_face_submission,
        "face_status": face_status,
        "can_submit_face": can_submit_face,
        "can_request_face_reenrollment": can_request_face_reenrollment,
        "face_capture_diagnostics": settings.FACE_CAPTURE_DIAGNOSTICS,
    }


def _intern_worksite_context(intern, *, worksite_form=None):
    accepted_applications = Application.objects.filter(
        intern=intern,
        status=Application.Status.ACCEPTED,
    )
    approved_worksites = list(intern.attendance_worksites.filter(
        is_active=True,
        approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
    ).filter(
        Q(application__in=accepted_applications)
        if accepted_applications.exists()
        else Q(application__isnull=True)
    ).select_related("company", "application__posting__company")
        .order_by("name", "pk")
    )
    pending_intern_worksite = intern.attendance_worksites.filter(
        approval_status=WorksiteLocation.ApprovalStatus.PENDING,
    ).select_related("application__posting__company").order_by("-created_at").first()
    latest_worksite_submission = intern.attendance_worksites.exclude(
        approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
        is_active=False,
    ).order_by("-created_at", "-pk").first()
    has_active_approved_worksite = any(worksite.is_active for worksite in approved_worksites)
    latest_submission_needs_action = latest_worksite_submission is not None and (
        latest_worksite_submission.approval_status
        in (
            WorksiteLocation.ApprovalStatus.REJECTED,
            WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED,
        )
    )
    submission_locked = pending_intern_worksite is not None or (
        has_active_approved_worksite and not latest_submission_needs_action
    )
    can_submit_worksite = not submission_locked
    return {
        "worksite_form": worksite_form or InternWorksiteRequestForm(intern=intern),
        "intern": intern,
        "intern_worksites": approved_worksites,
        "pending_intern_worksite": pending_intern_worksite,
        "latest_worksite_submission": latest_worksite_submission,
        "submission_locked": submission_locked,
        "can_submit_worksite": can_submit_worksite,
        "gps_max_accuracy_meters": settings.GPS_MAX_ACCURACY_METERS,
    }


@role_required(User.Role.INTERN)
@require_http_methods(["GET", "POST"])
def intern_worksite_registration(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    form = InternWorksiteRequestForm(
        request.POST if request.method == "POST" else None,
        intern=intern,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
            pending_submission_exists = WorksiteLocation.objects.filter(
                intern=locked_intern,
                approval_status=WorksiteLocation.ApprovalStatus.PENDING,
            ).exists()
            latest_submission = WorksiteLocation.objects.filter(
                intern=locked_intern,
            ).order_by("-created_at", "-pk").first()
            active_approved_exists = WorksiteLocation.objects.filter(
                intern=locked_intern,
                approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
                is_active=True,
            ).exists()
            latest_needs_action = latest_submission is not None and latest_submission.approval_status in (
                WorksiteLocation.ApprovalStatus.REJECTED,
                WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED,
            )
            if pending_submission_exists or active_approved_exists and not latest_needs_action:
                messages.error(
                    request,
                    "Your worksite submission is locked while it is pending or approved. Contact Admin for a correction or new submission.",
                )
                return redirect("portal:intern_worksite_registration")
            worksite = form.save(commit=False)
            worksite.company = None
            worksite.intern = locked_intern
            worksite.application = form.cleaned_data.get("application")
            worksite.created_by = request.user
            worksite.radius_meters = 100
            worksite.approval_status = WorksiteLocation.ApprovalStatus.PENDING
            worksite.rejection_reason = ""
            worksite.is_active = False
            try:
                with transaction.atomic():
                    worksite.save()
            except IntegrityError:
                messages.error(request, "A worksite submission is already pending Admin review.")
                return redirect("portal:intern_worksite_registration")
        audit(request.user, "worksite.registration_requested", worksite)
        messages.success(request, "Your OJT worksite location was submitted for Admin approval.")
        return redirect("portal:intern_worksite_registration")

    return render(request, "portal/intern/worksite_location.html", {
        **_intern_worksite_context(intern, worksite_form=form),
    })


@login_required
def company_worksites(request):
    is_company = request.user.role == User.Role.COMPANY
    is_admin = request.user.is_admin and not is_company
    if not (is_admin or is_company):
        return render(request, "errors/403.html", status=403)

    if is_company and request.method == "POST":
        return HttpResponseForbidden("Company users cannot create or change official worksite locations.")

    company = None if is_admin else get_object_or_404(CompanyProfile, user=request.user)
    form = WorksiteLocationForm(request.POST or None, allow_company_selection=True) if is_admin else None
    if is_admin and request.method == "GET" and request.GET.get("company"):
        form.fields["company"].initial = request.GET["company"]

    if request.method == "POST" and request.POST.get("action") == "deactivate":
        if not is_admin:
            return HttpResponseForbidden("Company worksite changes must be submitted for Admin approval.")
        eligible_worksites = WorksiteLocation.objects.filter(company__isnull=False, intern__isnull=True)
        worksite = get_object_or_404(eligible_worksites, pk=request.POST.get("worksite_id"))
        worksite.is_active = False
        worksite.save(update_fields=("is_active",))
        audit(request.user, "worksite.deactivated", worksite)
        messages.success(request, f"{worksite.name} was deactivated.")
        return redirect("portal:company_worksites")

    if request.method == "POST" and request.POST.get("action") == "create":
        if form.is_valid():
            worksite = form.save(commit=False)
            worksite.company = form.cleaned_data["company"] if is_admin else company
            worksite.created_by = request.user
            worksite.approval_status = (
                WorksiteLocation.ApprovalStatus.APPROVED
                if is_admin
                else WorksiteLocation.ApprovalStatus.PENDING
            )
            worksite.is_active = is_admin
            worksite.save()
            if is_admin:
                audit(request.user, "worksite.enrolled_by_admin", worksite)
                messages.success(request, f"{worksite.name} was registered as an approved attendance location.")
            else:
                audit(request.user, "worksite.registration_requested", worksite)
                messages.success(request, f"{worksite.name} was submitted for Admin approval.")
            return redirect("portal:company_worksites")
    elif request.method == "POST":
        messages.error(request, "Choose a valid worksite action.")

    worksites = WorksiteLocation.objects.filter(company__isnull=False, intern__isnull=True) if is_admin else (
        WorksiteLocation.objects.filter(
            approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
        )
        .filter(
            Q(company=company, intern__isnull=True)
            | Q(
                company__isnull=True,
                application__posting__company=company,
                application__status=Application.Status.ACCEPTED,
            )
        )
        .distinct()
    )
    pending_worksites = WorksiteLocation.objects.filter(
            company__isnull=False,
            intern__isnull=True,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        )
    if not is_admin:
        pending_worksites = WorksiteLocation.objects.none()
    return render(request, "portal/worksite_locations.html", {
        "form": form,
        "gps_max_accuracy_meters": settings.GPS_MAX_ACCURACY_METERS,
        "worksites": worksites.select_related(
            "company",
            "intern__user",
            "created_by",
            "reviewed_by",
            "application__posting__company",
        ),
        "pending_worksite_count": pending_worksites.count() if is_admin else 0,
        "pending_worksite_label": "company worksite requests" if is_admin else "intern worksite requests",
        "is_admin": is_admin,
        "is_admin_company_manager": is_admin,
        "is_read_only_company": is_company,
        "is_coordinator": False,
        "page_title": "Company worksite locations" if is_admin else "Approved worksite locations",
        "page_description": (
            "Register an approved location for a selected Company."
            if is_admin
            else "Approved locations are shown here for reference. Interns submit worksite locations for Admin approval."
        ),
    })


@admin_required
def coordinator_external_worksites(request):
    if request.method == "POST" and request.POST.get("action") == "deactivate":
        worksite = get_object_or_404(
            WorksiteLocation.objects.filter(
                company__isnull=True,
                intern__placement_type=InternProfile.PlacementType.EXTERNAL,
            ),
            pk=request.POST.get("worksite_id"),
        )
        worksite.is_active = False
        worksite.save(update_fields=("is_active",))
        audit(request.user, "worksite.deactivated", worksite)
        messages.success(request, f"{worksite.name} was deactivated.")
        return redirect("portal:coordinator_external_worksites")
    if request.method == "POST":
        return HttpResponseForbidden("Interns must submit their own OJT worksite for Admin review.")

    worksites = WorksiteLocation.objects.filter(
        company__isnull=True,
        intern__placement_type=InternProfile.PlacementType.EXTERNAL,
    ).select_related("intern__user", "created_by", "reviewed_by")
    return render(request, "portal/worksite_locations.html", {
        "worksites": worksites,
        "pending_worksite_count": worksites.filter(
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).count(),
        "gps_max_accuracy_meters": settings.GPS_MAX_ACCURACY_METERS,
        "is_coordinator": True,
        "page_title": "External intern worksites",
        "page_description": "Interns submit their own external-placement worksite locations for Admin review. Review pending requests in Admin approvals.",
    })


@role_required(User.Role.INTERN)
@require_POST
def intern_face_enrollment(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    if request.POST.get("consent_confirmed") != "on":
        messages.error(request, "Confirm your informed consent before enrolling your face.")
        return redirect("portal:profile")

    def submission_is_locked(locked_intern):
        if FaceEnrollmentSubmission.objects.filter(
            intern=locked_intern,
            status=FaceEnrollmentSubmission.Status.PENDING,
        ).exists():
            return True
        if FaceReenrollmentRequest.objects.filter(
            intern=locked_intern,
            status=FaceReenrollmentRequest.Status.PENDING,
        ).exists():
            return True
        has_enrollment = FaceEnrollment.objects.filter(intern=locked_intern).exists()
        has_permission = FaceReenrollmentRequest.objects.filter(
            intern=locked_intern,
            status__in=(
                FaceReenrollmentRequest.Status.APPROVED,
                FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
            ),
        ).exists()
        latest_submission = FaceEnrollmentSubmission.objects.filter(
            intern=locked_intern,
        ).first()
        was_rejected = latest_submission is not None and latest_submission.status in (
            FaceEnrollmentSubmission.Status.REJECTED,
            FaceEnrollmentSubmission.Status.RETAKE_REQUESTED,
        )
        return has_enrollment and not (has_permission or was_rejected)

    with transaction.atomic():
        locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
        if submission_is_locked(locked_intern):
            messages.error(request, "Your face enrollment is locked pending Admin review or authorization.")
            return redirect("portal:profile")

    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
        face_embedding = create_face_embedding(image)
        encrypted_embedding = encrypt_embedding(face_embedding)
    except (FaceCaptureError, FaceImageError) as exc:
        messages.error(request, str(exc))
        return redirect("portal:profile")
    except FaceModelError:
        LOGGER.exception("Face enrollment failed for intern %s.", intern.pk)
        messages.error(request, "Face enrollment could not be completed because the models are unavailable.")
        return redirect("portal:profile")

    try:
        if _face_embedding_is_duplicate(intern, face_embedding):
            messages.error(request, "This face is already registered under another account.")
            return redirect("portal:profile")
    except FaceModelError:
        LOGGER.exception("Could not verify face uniqueness for intern %s.", intern.pk)
        messages.error(request, "Face uniqueness could not be verified. Please try again later.")
        return redirect("portal:profile")

    with transaction.atomic():
        locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
        if submission_is_locked(locked_intern):
            messages.error(request, "Your face enrollment is locked pending Admin review or authorization.")
            return redirect("portal:profile")
        existing_enrollment = FaceEnrollment.objects.select_for_update().filter(
            intern=locked_intern,
        ).first()
        if existing_enrollment:
            try:
                similarity = cosine_similarity(
                    face_embedding,
                    decrypt_embedding(existing_enrollment.encrypted_embedding),
                )
            except FaceModelError:
                LOGGER.exception("Could not compare face re-enrollment for intern %s.", intern.pk)
                messages.error(request, "Your face could not be compared with the current enrollment. Please try again.")
                return redirect("portal:profile")
            if similarity < COSINE_MATCH_THRESHOLD:
                messages.error(
                    request,
                    "This face does not match your current enrollment. Retake the image and try again.",
                )
                return redirect("portal:profile")
        try:
            with transaction.atomic():
                submission = FaceEnrollmentSubmission.objects.create(
                    intern=locked_intern,
                    encrypted_embedding=encrypted_embedding,
                )
        except IntegrityError:
            messages.error(request, "A face enrollment is already pending Admin review.")
            return redirect("portal:profile")
    audit(request.user, "face_enrollment.submitted", submission)
    messages.success(request, "Your encrypted face enrollment was submitted for Admin review.")
    return redirect("portal:profile")


@role_required(User.Role.INTERN)
@require_POST
def intern_face_reenrollment_request(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    reason = request.POST.get("reason", "").strip()
    if not FaceEnrollment.objects.filter(intern=intern).exists():
        messages.error(request, "Enroll your face before requesting re-registration.")
        return redirect("portal:profile")
    if len(reason) < 10 or len(reason) > 500:
        messages.error(request, "Enter a reason between 10 and 500 characters.")
        return redirect("portal:profile")

    with transaction.atomic():
        locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
        active_request = FaceReenrollmentRequest.objects.filter(
            intern=locked_intern,
            status__in=(
                FaceReenrollmentRequest.Status.PENDING,
                FaceReenrollmentRequest.Status.APPROVED,
                FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
            ),
        ).exists()
        pending_submission = FaceEnrollmentSubmission.objects.filter(
            intern=locked_intern,
            status=FaceEnrollmentSubmission.Status.PENDING,
        ).exists()
        if active_request or pending_submission:
            messages.info(request, "A face re-registration request is already awaiting completion.")
            return redirect("portal:profile")
        reenrollment_request = FaceReenrollmentRequest.objects.create(
            intern=locked_intern,
            reason=reason,
        )
        audit(request.user, "face_reenrollment.requested", reenrollment_request)

    messages.success(request, "Your face re-registration request was sent to an administrator.")
    return redirect("portal:profile")


@admin_required
def admin_approvals(request):
    if request.method == "POST":
        action = request.POST.get("action")
        review_reason = request.POST.get("review_reason", "").strip()
        if action in {
            "reject_face_reset",
            "reject_face_submission",
            "request_face_retake",
            "request_face_submission_retake",
            "reject_worksite",
            "request_worksite_corrections",
        } and not 1 <= len(review_reason) <= 1000:
            return HttpResponseBadRequest("Provide a reason or instructions of no more than 1000 characters.")

        if action == "request_face_retake":
            with transaction.atomic():
                intern = get_object_or_404(
                    InternProfile.objects.select_for_update().select_related("user"),
                    pk=request.POST.get("intern_id"),
                )
                if not FaceEnrollment.objects.filter(intern=intern).exists():
                    return HttpResponseBadRequest("This intern has no approved enrollment to retake.")
                if FaceEnrollmentSubmission.objects.filter(
                    intern=intern,
                    status=FaceEnrollmentSubmission.Status.PENDING,
                ).exists():
                    return HttpResponseBadRequest("Review the pending face submission before requesting a retake.")
                if FaceReenrollmentRequest.objects.filter(
                    intern=intern,
                    status__in=(
                        FaceReenrollmentRequest.Status.PENDING,
                        FaceReenrollmentRequest.Status.APPROVED,
                        FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
                    ),
                ).exists():
                    return HttpResponseBadRequest("A face re-registration request is already active.")
                reenrollment_request = FaceReenrollmentRequest.objects.create(
                    intern=intern,
                    reason="Admin requested a face retake.",
                    status=FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
                    reviewed_by=request.user,
                    reviewed_at=timezone.now(),
                    review_reason=review_reason,
                )
                audit(request.user, "face_reenrollment.requested_by_admin", reenrollment_request)
            messages.success(request, f"A face retake was requested for {intern.user.get_full_name() or intern.user.username}.")
            return redirect("portal:admin_approvals")

        if action in {"approve_face_submission", "reject_face_submission", "request_face_submission_retake"}:
            with transaction.atomic():
                submission = get_object_or_404(
                    FaceEnrollmentSubmission.objects.select_for_update().select_related("intern__user"),
                    pk=request.POST.get("request_id"),
                    status=FaceEnrollmentSubmission.Status.PENDING,
                )
                InternProfile.objects.select_for_update().get(pk=submission.intern_id)
                now = timezone.now()
                if action == "approve_face_submission":
                    FaceEnrollment.objects.update_or_create(
                        intern=submission.intern,
                        defaults={
                            "encrypted_embedding": submission.encrypted_embedding,
                            "enrolled_by": submission.intern.user,
                            "consent_confirmed_at": submission.submitted_at,
                            "consent_text_version": FACE_CONSENT_VERSION,
                        },
                    )
                    submission.status = FaceEnrollmentSubmission.Status.APPROVED
                    FaceReenrollmentRequest.objects.filter(
                        intern=submission.intern,
                        status__in=(
                            FaceReenrollmentRequest.Status.APPROVED,
                            FaceReenrollmentRequest.Status.ADMIN_REQUESTED,
                        ),
                    ).update(
                        status=FaceReenrollmentRequest.Status.COMPLETED,
                        reviewed_by=request.user,
                        reviewed_at=now,
                    )
                elif action == "reject_face_submission":
                    submission.status = FaceEnrollmentSubmission.Status.REJECTED
                else:
                    submission.status = FaceEnrollmentSubmission.Status.RETAKE_REQUESTED
                submission.reviewed_by = request.user
                submission.reviewed_at = now
                submission.review_reason = review_reason
                submission.save(update_fields=(
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "review_reason",
                ))
                audit(request.user, f"face_enrollment_submission.{submission.status}", submission)
            messages.success(request, f"Face enrollment submission {submission.get_status_display().lower()}.")
            return redirect("portal:admin_approvals")

        if action in {"approve_face_reset", "reject_face_reset"}:
            status = (
                FaceReenrollmentRequest.Status.APPROVED
                if action == "approve_face_reset"
                else FaceReenrollmentRequest.Status.REJECTED
            )
            with transaction.atomic():
                approval_request = get_object_or_404(
                    FaceReenrollmentRequest.objects.select_for_update(),
                    pk=request.POST.get("request_id"),
                    status=FaceReenrollmentRequest.Status.PENDING,
                )
                approval_request.status = status
                approval_request.reviewed_by = request.user
                approval_request.reviewed_at = timezone.now()
                approval_request.review_reason = review_reason
                approval_request.save(update_fields=(
                    "status",
                    "reviewed_by",
                    "reviewed_at",
                    "review_reason",
                ))
                audit(request.user, f"face_reenrollment.{status}", approval_request)
            messages.success(request, f"Face re-registration request {status}.")
            return redirect("portal:admin_approvals")

        if action not in {"approve_worksite", "reject_worksite", "request_worksite_corrections"}:
            return HttpResponseBadRequest("Unknown approval action.")

        status = {
            "approve_worksite": WorksiteLocation.ApprovalStatus.APPROVED,
            "reject_worksite": WorksiteLocation.ApprovalStatus.REJECTED,
            "request_worksite_corrections": WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED,
        }[action]
        approved_radius = None
        if status == WorksiteLocation.ApprovalStatus.APPROVED:
            radius_value = request.POST.get("radius_meters", "").strip()
            if radius_value:
                try:
                    approved_radius = int(radius_value)
                except ValueError:
                    return HttpResponseBadRequest("The geofence radius must be a whole number of meters.")
                if not 10 <= approved_radius <= 5000:
                    return HttpResponseBadRequest("The geofence radius must be between 10 and 5000 meters.")

        with transaction.atomic():
            worksite_query = WorksiteLocation.objects.select_for_update().filter(
                Q(intern__isnull=False) | Q(company__isnull=False),
            )
            if action == "request_worksite_corrections":
                worksite = get_object_or_404(
                    worksite_query,
                    pk=request.POST.get("request_id"),
                    approval_status__in=(
                        WorksiteLocation.ApprovalStatus.PENDING,
                        WorksiteLocation.ApprovalStatus.APPROVED,
                    ),
                )
            else:
                worksite = get_object_or_404(
                    worksite_query,
                    pk=request.POST.get("request_id"),
                    approval_status=WorksiteLocation.ApprovalStatus.PENDING,
                )
            if status == WorksiteLocation.ApprovalStatus.APPROVED and worksite.intern_id:
                previous_intern_worksites = WorksiteLocation.objects.select_for_update().filter(
                    intern_id=worksite.intern_id,
                    approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
                    is_active=True,
                ).exclude(pk=worksite.pk)
                for previous_worksite in previous_intern_worksites:
                    previous_worksite.is_active = False
                    previous_worksite.save(update_fields=("is_active",))
                    audit(
                        request.user,
                        "worksite.replaced",
                        previous_worksite,
                        replacement_id=worksite.pk,
                    )

            replacement_id = request.POST.get("replace_worksite_id", "").strip()
            if replacement_id:
                if status != WorksiteLocation.ApprovalStatus.APPROVED:
                    return HttpResponseBadRequest("A worksite can only be replaced when approving the new Company site.")
                if not worksite.company_id:
                    return HttpResponseBadRequest("Only Company worksite requests can replace an existing site.")
                replaced_worksite = get_object_or_404(
                    WorksiteLocation.objects.select_for_update(),
                    pk=replacement_id,
                    company_id=worksite.company_id,
                    approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
                    is_active=True,
                )
                replaced_worksite.is_active = False
                replaced_worksite.save(update_fields=("is_active",))
                audit(
                    request.user,
                    "worksite.replaced",
                    replaced_worksite,
                    replacement_id=worksite.pk,
                )
            worksite.approval_status = status
            worksite.is_active = status == WorksiteLocation.ApprovalStatus.APPROVED
            worksite.reviewed_by = request.user
            worksite.reviewed_at = timezone.now()
            worksite.rejection_reason = review_reason if status != WorksiteLocation.ApprovalStatus.APPROVED else ""
            if approved_radius is not None:
                worksite.radius_meters = approved_radius
            worksite.save(update_fields=(
                "approval_status",
                "is_active",
                "reviewed_by",
                "reviewed_at",
                "rejection_reason",
                "radius_meters",
            ))
            audit_action = (
                "worksite.corrections_requested"
                if action == "request_worksite_corrections"
                else f"worksite.registration_{status}"
            )
            audit(request.user, audit_action, worksite, reason=review_reason or None)
            if action == "request_worksite_corrections":
                messages.success(request, f"Corrections were requested for {worksite.name}.")
            else:
                messages.success(request, f"{worksite.name} was {status}.")
        return redirect("portal:admin_approvals")

    worksite_requests = list(
        WorksiteLocation.objects.filter(
            Q(intern__isnull=False) | Q(company__isnull=False),
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).select_related(
            "company",
            "intern__user",
            "application__posting__company",
        )
    )
    for worksite in worksite_requests:
        worksite.replacement_options = (
            WorksiteLocation.objects.filter(
                company_id=worksite.company_id,
                approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
                is_active=True,
            ).exclude(pk=worksite.pk)
            if worksite.company_id
            else WorksiteLocation.objects.none()
        )

    face_enrollment_records = list(FaceEnrollment.objects.select_related("intern__user").order_by(
        "intern__user__last_name",
        "intern__user__first_name",
    ))
    for enrollment in face_enrollment_records:
        enrollment.has_pending_submission = FaceEnrollmentSubmission.objects.filter(
            intern_id=enrollment.intern_id,
            status=FaceEnrollmentSubmission.Status.PENDING,
        ).exists()

    return render(request, "portal/admin/approvals.html", {
        "face_requests": FaceReenrollmentRequest.objects.filter(
            status=FaceReenrollmentRequest.Status.PENDING,
        ).select_related("intern__user", "reviewed_by"),
        "face_submissions": FaceEnrollmentSubmission.objects.filter(
            status=FaceEnrollmentSubmission.Status.PENDING,
        ).select_related("intern__user"),
        "face_enrollment_records": face_enrollment_records,
        "face_request_history": FaceReenrollmentRequest.objects.select_related(
            "intern__user",
            "reviewed_by",
        ).order_by("-created_at")[:100],
        "face_submission_history": FaceEnrollmentSubmission.objects.select_related(
            "intern__user",
            "reviewed_by",
        ).order_by("-submitted_at")[:100],
        "worksite_requests": worksite_requests,
        "worksite_history": WorksiteLocation.objects.filter(intern__isnull=False).exclude(
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).select_related("company", "intern__user", "reviewed_by").order_by("-created_at")[:100],
    })


def _face_embedding_is_duplicate(intern, new_embedding):
    for enrollment in FaceEnrollment.objects.exclude(intern=intern).iterator():
        existing_embedding = decrypt_embedding(enrollment.encrypted_embedding)
        if cosine_similarity(new_embedding, existing_embedding) >= COSINE_MATCH_THRESHOLD:
            return True
    for submission in FaceEnrollmentSubmission.objects.exclude(intern=intern).filter(
        status__in=(
            FaceEnrollmentSubmission.Status.PENDING,
            FaceEnrollmentSubmission.Status.APPROVED,
        ),
    ).exclude(intern__face_enrollment__isnull=False).iterator():
        existing_embedding = decrypt_embedding(submission.encrypted_embedding)
        if cosine_similarity(new_embedding, existing_embedding) >= COSINE_MATCH_THRESHOLD:
            return True
    return False


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
    available = list(
        Posting.objects.filter(status=Posting.Status.PUBLISHED)
        .exclude(pk__in=applied_ids)
        .select_related("company")
    )
    for posting in available:
        posting.apply_form = ApplicationSubmissionForm()
    return render(request, "portal/intern/postings.html", {
        "postings": available,
        "application_count": len(applied_ids),
        "external_placement": False,
    })


def _render_postings_with_application_form(request, intern, posting, form):
    applied_ids = intern.applications.values_list("posting_id", flat=True)
    postings = list(
        Posting.objects.filter(status=Posting.Status.PUBLISHED)
        .exclude(pk__in=applied_ids)
        .select_related("company")
    )
    if all(available_posting.pk != posting.pk for available_posting in postings):
        postings.insert(0, posting)
    for available_posting in postings:
        available_posting.apply_form = (
            form if available_posting.pk == posting.pk else ApplicationSubmissionForm()
        )
    return render(request, "portal/intern/postings.html", {
        "postings": postings,
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
    form = ApplicationSubmissionForm(request.POST, request.FILES)
    if not form.is_valid():
        return _render_postings_with_application_form(request, intern, posting, form)
    application = form.save(commit=False)
    application.intern = intern
    application.posting = posting
    try:
        application.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as error:
        if hasattr(error, "message_dict"):
            for field_name, errors in error.message_dict.items():
                form.add_error(field_name if field_name in form.fields else None, errors)
        else:
            form.add_error(None, error.messages)
        return _render_postings_with_application_form(request, intern, posting, form)
    try:
        with transaction.atomic():
            application.save()
    except IntegrityError:
        form.add_error(None, "You have already applied for this opportunity.")
        return _render_postings_with_application_form(request, intern, posting, form)
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
    if not latitude_value.is_finite() or not longitude_value.is_finite():
        return None, None
    if not Decimal("-90") <= latitude_value <= Decimal("90") or not Decimal("-180") <= longitude_value <= Decimal("180"):
        return None, None
    return latitude_value, longitude_value


def _reported_accuracy_meters(request, prefix):
    accuracy = request.POST.get(f"{prefix}_accuracy_meters", "").strip()
    if not accuracy:
        return None
    try:
        accuracy_value = Decimal(accuracy)
    except InvalidOperation:
        return None
    if not accuracy_value.is_finite() or accuracy_value <= 0:
        return None
    return accuracy_value


def _log_face_image_diagnostics(image, *, intern_id, endpoint, face_count=None):
    if not getattr(settings, "FACE_CAPTURE_DIAGNOSTICS", False):
        return

    image_array = np.asarray(image)
    LOGGER.warning(
        "Face image diagnostics: endpoint=%s intern_id=%s image_type=%s mode=%s "
        "size=%s array_shape=%s array_dtype=%s array_empty=%s face_count=%s",
        endpoint,
        intern_id,
        type(image).__name__,
        image.mode,
        image.size,
        image_array.shape,
        image_array.dtype,
        image_array.size == 0,
        face_count if face_count is not None else "not_run",
    )


def _worksites_for_attendance(intern):
    locations = WorksiteLocation.objects.filter(
        is_active=True,
        approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
    )
    accepted_applications = Application.objects.filter(
        intern=intern,
        status=Application.Status.ACCEPTED,
    )
    approved_intern_worksites = locations.filter(
        intern=intern,
        application__in=accepted_applications,
    )
    if accepted_applications.exists():
        return approved_intern_worksites
    return locations.filter(
        company__isnull=True,
        intern=intern,
        application__isnull=True,
    )


def _attendance_location_status(worksites, latitude, longitude, accuracy_meters=None):
    if latitude is None or longitude is None:
        return AttendanceFaceCapture.LocationStatus.UNAVAILABLE, None, None

    distances = []
    for worksite in worksites:
        distance_meters = haversine_distance_km(
            latitude,
            longitude,
            worksite.latitude,
            worksite.longitude,
        ) * 1000
        allowed_radius_meters = worksite.radius_meters or 100
        if distance_meters <= allowed_radius_meters:
            distances.append((distance_meters, allowed_radius_meters, True))
        else:
            distances.append((distance_meters, allowed_radius_meters, False))

    if not distances:
        return AttendanceFaceCapture.LocationStatus.UNAVAILABLE, None, None

    eligible_sites = [entry for entry in distances if entry[2]]
    distance_meters, radius_meters, within_location = min(
        eligible_sites or distances,
        key=lambda entry: entry[0],
    )
    if accuracy_meters is None or accuracy_meters > settings.GPS_MAX_ACCURACY_METERS:
        location_status = AttendanceFaceCapture.LocationStatus.UNAVAILABLE
    else:
        location_status = (
            AttendanceFaceCapture.LocationStatus.INSIDE
            if within_location
            else AttendanceFaceCapture.LocationStatus.OUTSIDE
        )
    return location_status, round(distance_meters, 2), radius_meters


def _location_fields(action):
    prefix = "clock_in" if action == "clock_in" else "clock_out"
    return {
        "timestamp": prefix,
        "latitude": f"{prefix}_latitude",
        "longitude": f"{prefix}_longitude",
        "within_location": f"{prefix}_within_location",
        "distance": f"{prefix}_distance_meters",
        "allowed_radius": f"{prefix}_allowed_radius_meters",
        "reason": f"{prefix}_location_reason",
        "face_status": f"{prefix}_face_status",
        "face_score": f"{prefix}_face_score",
        "approval": "time_in_approved" if action == "clock_in" else "time_out_approved",
    }


def _distance_decimal(distance_meters):
    return Decimal(str(distance_meters)).quantize(Decimal("0.01")) if distance_meters is not None else None


def _attendance_face_check(request, intern, *, retain_image=True):
    enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    if enrollment is None:
        return AttendanceLog.FaceCheckStatus.NOT_ENROLLED, None, None
    if enrollment.consent_text_version != FACE_CONSENT_VERSION:
        return AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED, None, None

    image_data = request.POST.get("face_image", "").strip()
    if not image_data:
        return AttendanceLog.FaceCheckStatus.NOT_CAPTURED, None, None

    try:
        image = decode_camera_image(image_data)
    except FaceCaptureError as exc:
        LOGGER.warning("Could not read face image for intern %s: %s", intern.pk, exc)
        return AttendanceLog.FaceCheckStatus.ERROR, None, None

    _log_face_image_diagnostics(image, intern_id=intern.pk, endpoint="attendance")

    encrypted_image = None
    if retain_image:
        try:
            encrypted_image = encrypt_face_capture(encode_camera_image(image))
        except (FaceCaptureError, FaceModelError):
            LOGGER.exception("Could not retain face capture for intern %s.", intern.pk)
            return AttendanceLog.FaceCheckStatus.ERROR, None, None

    try:
        result = compare_face_embedding(image, enrollment.encrypted_embedding)
    except FaceImageError as exc:
        LOGGER.info("Face check unavailable for intern %s: %s", intern.pk, exc)
        return AttendanceLog.FaceCheckStatus.UNAVAILABLE, None, encrypted_image
    except FaceModelError:
        LOGGER.exception("Face check failed for intern %s.", intern.pk)
        return AttendanceLog.FaceCheckStatus.ERROR, None, encrypted_image

    return result["status"], result["similarity"], encrypted_image


def _attendance_face_verification_token(intern, action, image_data, similarity):
    return dumps(
        {
            "intern_id": intern.pk,
            "action": action,
            "image_sha256": hashlib.sha256(image_data.encode("utf-8")).hexdigest(),
            "similarity": float(similarity),
        },
        salt="portal.attendance-face-verification.v1",
    )


def _validated_attendance_face_token(token, intern, action, image_data):
    try:
        payload = loads(
            token,
            salt="portal.attendance-face-verification.v1",
            max_age=120,
        )
    except (BadSignature, SignatureExpired):
        return None

    image_hash = hashlib.sha256(image_data.encode("utf-8")).hexdigest()
    if (
        payload.get("intern_id") != intern.pk
        or payload.get("action") != action
        or payload.get("image_sha256") != image_hash
    ):
        return None
    try:
        similarity = float(payload["similarity"])
    except (KeyError, TypeError, ValueError):
        return None
    if not np.isfinite(similarity) or similarity < COSINE_MATCH_THRESHOLD:
        return None
    return similarity


def _face_check_feedback(face_status):
    if face_status == AttendanceLog.FaceCheckStatus.MATCHED:
        return {
            "quality": "good",
            "label": "Good — face match candidate",
            "message": "One face was detected and it matches the enrolled reference.",
        }
    if face_status == AttendanceLog.FaceCheckStatus.NOT_MATCHED:
        return {
            "quality": "poor",
            "label": "Face verification failed",
            "message": "Face verification failed. The captured face does not match your registered profile.",
        }
    messages_by_status = {
        AttendanceLog.FaceCheckStatus.NOT_CAPTURED: "No camera frame was captured.",
        AttendanceLog.FaceCheckStatus.NOT_ENROLLED: "There is no enrolled face reference for your account.",
        AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED: "Your face enrollment needs to be updated before face matching can run.",
        AttendanceLog.FaceCheckStatus.UNAVAILABLE: "The frame did not contain exactly one alignable face.",
        AttendanceLog.FaceCheckStatus.ERROR: "The camera frame or face-recognition service could not be processed.",
    }
    return {
        "quality": "poor",
        "label": "Poor capture — retake recommended",
        "message": messages_by_status.get(
            face_status,
            "The face could not be checked. Retake the frame before recording attendance.",
        ),
    }


def _attendance_face_rejection_message(face_status):
    messages_by_status = {
        AttendanceLog.FaceCheckStatus.NOT_CAPTURED: "Capture a face frame before recording attendance.",
        AttendanceLog.FaceCheckStatus.NOT_ENROLLED: "Enroll your face before recording attendance.",
        AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED: "Update your face enrollment and consent before recording attendance.",
        AttendanceLog.FaceCheckStatus.NOT_MATCHED: "Face verification failed. Face does not match your registered profile.",
        AttendanceLog.FaceCheckStatus.UNAVAILABLE: "The frame must contain exactly one clearly visible face. Retake it and try again.",
        AttendanceLog.FaceCheckStatus.ERROR: "The face frame could not be verified. Retake it or try again later.",
    }
    return messages_by_status.get(face_status, "Face verification is required before recording attendance.")


@intern_api_required()
def attendance_face_preview(request):
    action = request.POST.get("action")
    if action not in {"clock_in", "clock_out"}:
        return JsonResponse({"error": "Unknown attendance action."}, status=400)

    intern = InternProfile.objects.get(user=request.user)
    today = timezone.localdate()
    log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
    worksites = list(_worksites_for_attendance(intern))
    if not worksites:
        return JsonResponse(
            {"error": "No active worksite is assigned to your placement. Ask your coordinator or host company to enroll one."},
            status=409,
        )
    latitude, longitude = _coordinates(request, action)
    accuracy_meters = _reported_accuracy_meters(request, action)
    location_status, distance_meters, allowed_radius_meters = _attendance_location_status(
        worksites,
        latitude,
        longitude,
        accuracy_meters,
    )
    if action == "clock_out" and (
        not log or not log.clock_in or not log.time_in_approved
    ):
        return JsonResponse({"error": "Time-in must be approved before time-out."}, status=400)

    event = AttendanceFaceCapture.Event.TIME_IN if action == "clock_in" else AttendanceFaceCapture.Event.TIME_OUT
    if log and (
        getattr(log, "clock_in" if action == "clock_in" else "clock_out")
        or log.face_captures.filter(
            event=event,
            review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
        ).exists()
    ):
        return JsonResponse({"error": "This attendance event is already recorded or awaiting review."}, status=409)

    face_status, face_score, _ = _attendance_face_check(request, intern, retain_image=False)
    if face_status != AttendanceLog.FaceCheckStatus.MATCHED:
        status_code = (
            503
            if face_status == AttendanceLog.FaceCheckStatus.ERROR
            else 403
            if face_status in {
                AttendanceLog.FaceCheckStatus.NOT_ENROLLED,
                AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED,
                AttendanceLog.FaceCheckStatus.NOT_MATCHED,
            }
            else 400
        )
        return JsonResponse(
            {"error": _attendance_face_rejection_message(face_status), "face_status": face_status},
            status=status_code,
        )
    verification_token = _attendance_face_verification_token(
        intern,
        action,
        request.POST.get("face_image", "").strip(),
        face_score,
    )
    return JsonResponse({
        **_face_check_feedback(face_status),
        **(
            {"similarity": face_score}
            if settings.FACE_CAPTURE_DIAGNOSTICS
            else {}
        ),
        "verification_token": verification_token,
        "location_status": location_status,
        "distance_meters": distance_meters,
        "allowed_radius_meters": allowed_radius_meters,
        "accuracy_meters": float(accuracy_meters) if accuracy_meters is not None else None,
        "max_accuracy_meters": settings.GPS_MAX_ACCURACY_METERS,
        "location_requires_reason": location_status != AttendanceFaceCapture.LocationStatus.INSIDE,
    })


@intern_api_required()
def face_detection_preview(request):
    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
    except FaceCaptureError as exc:
        return JsonResponse({"face_count": 0, "error": str(exc)}, status=400)
    try:
        candidates = inspect_face_candidates(image)
    except (FileNotFoundError, FaceModelError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        _log_face_image_diagnostics(image, intern_id=request.user.pk, endpoint="face_detection")
        LOGGER.exception("Live face detection failed for intern %s.", request.user.pk)
        return JsonResponse(
            {"face_count": 0, "error": "The face-detection models are unavailable. Try again shortly."},
            status=503,
        )
    _log_face_image_diagnostics(
        image,
        intern_id=request.user.pk,
        endpoint="face_detection",
        face_count=len(candidates),
    )
    return JsonResponse({
        "face_count": len(candidates),
        "face_detected": bool(candidates),
        "detections": candidates,
        "image_width": image.width,
        "image_height": image.height,
    })


@intern_api_required()
def face_enrollment_preview(request):
    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
        face_embedding = create_face_embedding(image)
    except (FaceCaptureError, FaceImageError) as exc:
        return JsonResponse({
            "quality": "poor",
            "identity_status": "not_checked",
            "label": "Position Your Face",
            "message": str(exc),
        })
    except FaceModelError:
        LOGGER.exception("Face enrollment preview failed for intern %s.", request.user.pk)
        return JsonResponse({
            "quality": "poor",
            "identity_status": "not_checked",
            "label": "Face recognition unavailable",
            "message": "The face-recognition models could not process this image. Retake it or try again later.",
        }, status=503)
    enrollment = FaceEnrollment.objects.filter(intern__user=request.user).first()
    if enrollment:
        try:
            similarity = cosine_similarity(
                face_embedding,
                decrypt_embedding(enrollment.encrypted_embedding),
            )
        except FaceModelError:
            LOGGER.exception("Face re-enrollment preview comparison failed for intern %s.", request.user.pk)
            return JsonResponse({
                "quality": "poor",
                "identity_status": "unavailable",
                "label": "Face check unavailable",
                "message": "Your face could not be compared with the current enrollment. Retake the image or try again later.",
            }, status=503)
        if similarity < COSINE_MATCH_THRESHOLD:
            return JsonResponse({
                "quality": "poor",
                "identity_status": "not_matched",
                "label": "Face Not Matched",
                "message": "This face does not match your current enrollment. Try again with your own face.",
                **(
                    {"similarity": round(similarity, 4)}
                    if settings.FACE_CAPTURE_DIAGNOSTICS
                    else {}
                ),
            })
        return JsonResponse({
            "quality": "good",
            "identity_status": "matched",
            "label": "Face Matched",
            "message": "Your face matches the current enrollment. Review the preview and submit it for Admin review.",
            **(
                {"similarity": round(similarity, 4)}
                if settings.FACE_CAPTURE_DIAGNOSTICS
                else {}
            ),
        })
    return JsonResponse({
        "quality": "good",
        "identity_status": "not_required",
        "label": "Captured Successfully",
        "message": "Image quality passed. No previous face reference exists, so identity matching was not performed. Submit this enrollment for Admin review.",
    })


@role_required(User.Role.INTERN)
def attendance(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    worksite_locations = _worksites_for_attendance(intern)
    today = timezone.localdate()
    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def attendance_response(
            message,
            *,
            status_code=400,
            code=None,
            success=False,
            attendance_status=None,
            face_status=None,
            level="error",
        ):
            if is_ajax:
                payload = {"success": success}
                payload["message" if success else "error"] = message
                if code:
                    payload["code"] = code
                if attendance_status:
                    payload["attendance_status"] = attendance_status
                if face_status:
                    payload["face_status"] = face_status
                return JsonResponse(payload, status=status_code)
            getattr(messages, level)(request, message)
            return redirect("portal:attendance")

        action = request.POST.get("action")
        if action not in {"clock_in", "clock_out"}:
            if is_ajax:
                return attendance_response("Unknown attendance action.", code="invalid_action")
            return HttpResponseBadRequest("Unknown attendance action.")
        if action == "clock_out":
            existing_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
            if not existing_log or not existing_log.clock_in:
                return attendance_response(
                    "Record time in before recording time out.",
                    code="time_in_required",
                )
            if not existing_log.time_in_approved:
                return attendance_response(
                    "Your time-in must be approved by the company/admin before you can time out.",
                    code="time_in_pending",
                    status_code=409,
                )
        if not worksite_locations.exists():
            return attendance_response(
                "No active worksite is assigned to your placement. Ask your coordinator or host company to enroll one.",
                code="worksite_required",
                status_code=409,
            )
        latitude, longitude = _coordinates(request, action)
        accuracy_meters = _reported_accuracy_meters(request, action)
        location_status, distance_meters, allowed_radius_meters = _attendance_location_status(
            worksite_locations,
            latitude,
            longitude,
            accuracy_meters,
        )
        location_reason = request.POST.get(f"{action}_location_reason", "").strip()[:1000]
        requires_location_reason = location_status != AttendanceFaceCapture.LocationStatus.INSIDE
        if requires_location_reason and len(location_reason) < 10:
            return attendance_response(
                "Your location is outside the assigned worksite radius or GPS is unavailable. "
                "Provide an explanation of at least 10 characters to submit this attendance for supervisor review.",
                code="location_reason_required",
            )
        event = AttendanceFaceCapture.Event.TIME_IN if action == "clock_in" else AttendanceFaceCapture.Event.TIME_OUT
        existing_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
        if existing_log and existing_log.face_captures.filter(
            event=event,
            review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
        ).exists():
            return attendance_response(
                "This attendance event is already waiting for company/admin review.",
                code="already_pending",
                status_code=409,
                level="info",
            )
        verification_token = request.POST.get("face_verification_token", "").strip()
        if verification_token:
            face_image_data = request.POST.get("face_image", "").strip()
            face_score = _validated_attendance_face_token(
                verification_token,
                intern,
                action,
                face_image_data,
            )
            if face_score is None:
                return attendance_response(
                    "Face verification expired or changed. Please verify your face again.",
                    code="face_verification_expired",
                    status_code=403,
                )
            try:
                verified_image = decode_camera_image(face_image_data)
                encrypted_image = encrypt_face_capture(encode_camera_image(verified_image))
            except (FaceCaptureError, FaceModelError):
                LOGGER.exception("Could not retain verified attendance capture for intern %s.", intern.pk)
                return attendance_response(
                    "The verified camera frame could not be saved. Please try again.",
                    code="face_capture_unavailable",
                    status_code=422,
                )
            face_status = AttendanceLog.FaceCheckStatus.MATCHED
        else:
            face_status, face_score, encrypted_image = _attendance_face_check(request, intern)
        if face_status != AttendanceLog.FaceCheckStatus.MATCHED:
            face_response_code = (
                503
                if face_status == AttendanceLog.FaceCheckStatus.ERROR
                else 403
                if face_status in {
                    AttendanceLog.FaceCheckStatus.NOT_MATCHED,
                    AttendanceLog.FaceCheckStatus.NOT_ENROLLED,
                    AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED,
                }
                else 422
            )
            return attendance_response(
                _attendance_face_rejection_message(face_status),
                code="face_verification_failed",
                status_code=face_response_code,
                face_status=face_status,
            )

        captured_at = timezone.now()
        attendance_status = "pending_review"
        try:
            with transaction.atomic():
                if action == "clock_out":
                    log = AttendanceLog.objects.select_for_update().filter(intern=intern, work_date=today).first()
                    if not log or not log.clock_in:
                        messages.error(request, "Record time in before recording time out.")
                        return redirect("portal:attendance")
                    if not log.time_in_approved:
                        messages.error(request, "Your time-in must be approved by the company/admin before you can time out.")
                        return redirect("portal:attendance")
                else:
                    log, _ = AttendanceLog.objects.select_for_update().get_or_create(intern=intern, work_date=today)
                fields = _location_fields(action)
                if log.face_captures.filter(
                    event=event,
                    review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
                ).exists():
                    return attendance_response(
                        "This attendance event is already waiting for company/admin review.",
                        code="already_pending",
                        status_code=409,
                        level="info",
                    )
                if getattr(log, fields["timestamp"]):
                    return attendance_response(
                        f"You have already recorded {event.replace('_', ' ')} today.",
                        code="already_recorded",
                        status_code=409,
                        level="info",
                    )

                automatically_approved = (
                    face_status == AttendanceLog.FaceCheckStatus.MATCHED
                    and location_status == AttendanceFaceCapture.LocationStatus.INSIDE
                )
                attendance_status = "approved" if automatically_approved else "pending_review"
                review_status = (
                    AttendanceFaceCapture.ReviewStatus.NOT_REQUIRED
                    if automatically_approved
                    else AttendanceFaceCapture.ReviewStatus.PENDING
                )
                capture = AttendanceFaceCapture.objects.create(
                    attendance_log=log,
                    event=event,
                    face_status=face_status,
                    face_score=face_score,
                    encrypted_image=encrypted_image,
                    captured_at=captured_at,
                    latitude=latitude,
                    longitude=longitude,
                    location_status=location_status,
                    distance_meters=_distance_decimal(distance_meters),
                    allowed_radius_meters=allowed_radius_meters,
                    location_reason=location_reason,
                    review_status=review_status,
                )
                setattr(log, fields["face_status"], face_status)
                setattr(log, fields["face_score"], face_score)
                setattr(log, fields["latitude"], latitude)
                setattr(log, fields["longitude"], longitude)
                setattr(
                    log,
                    fields["within_location"],
                    location_status == AttendanceFaceCapture.LocationStatus.INSIDE,
                )
                setattr(log, fields["distance"], _distance_decimal(distance_meters))
                setattr(log, fields["allowed_radius"], allowed_radius_meters)
                setattr(log, fields["reason"], location_reason)
                setattr(log, fields["approval"], automatically_approved)
                log.notes = request.POST.get("notes", "").strip()[:255] or log.notes
                if face_status == AttendanceLog.FaceCheckStatus.MATCHED:
                    setattr(log, fields["timestamp"], captured_at)
                update_fields = [
                    fields["face_status"],
                    fields["face_score"],
                    "notes",
                    fields["latitude"],
                    fields["longitude"],
                    fields["within_location"],
                    fields["distance"],
                    fields["allowed_radius"],
                    fields["reason"],
                    fields["approval"],
                ]
                if face_status == AttendanceLog.FaceCheckStatus.MATCHED:
                    update_fields.append(fields["timestamp"])
                log.save(update_fields=update_fields)
                if automatically_approved:
                    audit(
                        request.user,
                        "attendance.clocked_in" if action == "clock_in" else "attendance.clocked_out",
                        log,
                        capture_id=capture.pk,
                    )
                    if not is_ajax:
                        messages.success(
                            request,
                            f"Face verified and {event.replace('_', ' ')} approved within the assigned worksite.",
                        )
                elif requires_location_reason:
                    audit(
                        request.user,
                        "attendance.location_review_requested",
                        log,
                        event=event,
                        capture_id=capture.pk,
                        location_status=location_status,
                    )
                    if not is_ajax:
                        messages.warning(
                            request,
                            f"{event.replace('_', ' ').title()} was submitted with a location exception and is awaiting supervisor review.",
                        )
        except IntegrityError:
            if is_ajax:
                return attendance_response(
                    "Today's attendance record was already created. Refresh and try again.",
                    code="already_recorded",
                    status_code=409,
                    level="info",
                )
            messages.info(request, "Today's attendance record was already created. Refresh and try again.")
        if is_ajax:
            if attendance_status == "approved":
                message = f"Face verified and {event.replace('_', ' ')} approved within the assigned worksite."
            else:
                message = (
                    f"{event.replace('_', ' ').title()} was submitted with a location exception "
                    "and is awaiting supervisor review."
                )
            return attendance_response(
                message,
                status_code=200,
                success=True,
                attendance_status=attendance_status,
            )
        return redirect("portal:attendance")

    today_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
    worksite_rows = list(worksite_locations)
    face_enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    dtr_context = _dtr_breakdown_context(intern, request.GET.get("period", "all"))
    pending_face_events = set()
    if today_log:
        pending_face_events = set(
            today_log.face_captures.filter(
                review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
            ).values_list("event", flat=True)
        )
    return render(request, "portal/intern/attendance.html", {
        "today_log": today_log,
        "today": today,
        "location_enrolled": bool(worksite_rows),
        "worksite_locations": worksite_rows,
        "gps_max_accuracy_meters": settings.GPS_MAX_ACCURACY_METERS,
        "attendance_worksites_json": [
            {
                "name": worksite.name,
                "latitude": float(worksite.latitude),
                "longitude": float(worksite.longitude),
                "radius_meters": worksite.radius_meters or 100,
            }
            for worksite in worksite_rows
        ],
        "face_ready": (
            face_enrollment is not None
            and face_enrollment.consent_text_version == FACE_CONSENT_VERSION
        ),
        "face_capture_diagnostics": settings.FACE_CAPTURE_DIAGNOSTICS,
        "pending_worksite_request": intern.attendance_worksites.filter(
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).first(),
        "pending_face_events": pending_face_events,
        **dtr_context,
    })


@admin_required
def coordinator_face_enrollment(request):
    if request.method == "POST":
        action = request.POST.get("action")
        intern = get_object_or_404(InternProfile.objects.select_related("user"), pk=request.POST.get("intern_id"))
        if action == "delete":
            _delete_intern_face_enrollment(request, intern)
            return redirect("portal:coordinator_face_enrollment")
        if action != "enroll":
            return HttpResponseBadRequest("Unknown face-enrollment action.")
        if FaceEnrollmentSubmission.objects.filter(
            intern=intern,
            status=FaceEnrollmentSubmission.Status.PENDING,
        ).exists():
            return HttpResponseBadRequest("Review the Intern's pending submission before enrolling a replacement.")
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

        try:
            if _face_embedding_is_duplicate(intern, decrypt_embedding(embedding)):
                messages.error(request, "This face is already registered under another account.")
                return redirect("portal:coordinator_face_enrollment")
        except FaceModelError:
            LOGGER.exception("Could not verify face uniqueness for intern %s.", intern.pk)
            messages.error(request, "Face uniqueness could not be verified. Please try again later.")
            return redirect("portal:coordinator_face_enrollment")

        with transaction.atomic():
            locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
            if FaceEnrollmentSubmission.objects.filter(
                intern=locked_intern,
                status=FaceEnrollmentSubmission.Status.PENDING,
            ).exists():
                return HttpResponseBadRequest("Review the Intern's pending submission before enrolling a replacement.")
            _, created = FaceEnrollment.objects.update_or_create(
                intern=locked_intern,
                defaults={
                    "encrypted_embedding": embedding,
                    "enrolled_by": request.user,
                    "consent_confirmed_at": timezone.now(),
                    "consent_text_version": FACE_CONSENT_VERSION,
                },
            )
            reviewed_at = timezone.now()
            FaceEnrollmentSubmission.objects.create(
                intern=locked_intern,
                encrypted_embedding=embedding,
                status=FaceEnrollmentSubmission.Status.APPROVED,
                reviewed_by=request.user,
                reviewed_at=reviewed_at,
            )
        audit(request.user, "face_enrollment.created" if created else "face_enrollment.updated", intern)
        messages.success(request, "Encrypted face embedding saved with your updated attendance-capture consent.")
        return redirect("portal:coordinator_face_enrollment")

    interns = InternProfile.objects.select_related("user").prefetch_related("face_enrollment").order_by(
        "user__last_name",
        "user__first_name",
        "user__username",
    )
    return render(request, "portal/coordinator/face_enrollment.html", {"interns": interns})


def _delete_intern_face_enrollment(request, intern):
    with transaction.atomic():
        InternProfile.objects.select_for_update().get(pk=intern.pk)
        enrollment = FaceEnrollment.objects.select_for_update().filter(intern=intern).first()
        if enrollment:
            FaceEnrollmentSubmission.objects.create(
                intern=intern,
                encrypted_embedding=enrollment.encrypted_embedding,
                status=FaceEnrollmentSubmission.Status.REJECTED,
                reviewed_by=request.user,
                reviewed_at=timezone.now(),
                review_reason="Enrollment was reset by Admin.",
                submitted_at=enrollment.updated_at,
            )
            enrollment.delete()
    if enrollment:
        audit(request.user, "face_enrollment.reset", intern)
        messages.success(request, "Face enrollment was reset. Its encrypted history and attendance captures were retained.")
        return True
    messages.info(request, "This intern has no face enrollment.")
    return False
















@role_required(User.Role.INTERN)
def attendance_export(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    document = attendance_pdf(intern, intern.attendance_logs.all()[:90])
    return FileResponse(document, as_attachment=True, filename="attendance-record.pdf", content_type="application/pdf")




@role_required(User.Role.INTERN)
def daily_report(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    today = timezone.localdate()
    form = DailyReportForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        report = form.save(commit=False)
        report.intern = intern
        report.work_date = today
        report.status = DailyReport.Status.SUBMITTED
        try:
            with transaction.atomic():
                report.save()
        except IntegrityError:
            messages.error(request, "The report could not be saved. Refresh the page and try again.")
        else:
            audit(request.user, "daily_report.submitted", report)
            messages.success(request, "Daily report saved. Previous submissions remain in your history.")
            return redirect("portal:daily_report")
    page_obj = Paginator(
        intern.daily_reports.select_related("reviewer").order_by("-work_date", "-submitted_at"),
        30,
    ).get_page(request.GET.get("page"))
    page_reports = list(page_obj.object_list)
    _attach_approved_report_hours(page_reports)
    return render(request, "portal/intern/daily_report.html", {
        "form": form,
        "today": today,
        "reports": page_reports,
        "page_obj": page_obj,
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
@require_POST
def review_daily_report(request, report_id):
    report = get_object_or_404(
        DailyReport,
        pk=report_id,
        status__in=(DailyReport.Status.SUBMITTED, DailyReport.Status.CHANGES_REQUESTED),
    )
    form = DailyReportReviewForm(request.POST)
    if not form.is_valid():
        return render(request, "portal/coordinator/daily_reports.html", {
            "reports": DailyReport.objects.select_related(
                "intern__user",
                "intern",
                "reviewer",
            ).filter(
                status__in=(DailyReport.Status.SUBMITTED, DailyReport.Status.CHANGES_REQUESTED)
            ).order_by("work_date", "intern__user__last_name")[:100],
            "pending_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
            "review_form": form,
            "review_report_id": report.pk,
        })
    decision = form.cleaned_data["decision"]
    feedback = form.cleaned_data["supervisor_feedback"]

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


def _attach_approved_report_hours(reports):
    if not reports:
        return
    intern_ids = {report.intern_id for report in reports}
    work_dates = {report.work_date for report in reports}
    approved_logs = AttendanceLog.objects.approved().filter(
        intern_id__in=intern_ids,
        work_date__in=work_dates,
    ).only("intern_id", "work_date", "clock_in", "clock_out")
    hours_by_day = {
        (log.intern_id, log.work_date): log.hours_worked
        for log in approved_logs
    }
    for report in reports:
        report.hours_worked = hours_by_day.get((report.intern_id, report.work_date), 0)


def _daily_report_history_context(request, reports, *, is_admin, company=None):
    view_mode = request.GET.get("view", "daily")
    if view_mode not in {"daily", "weekly"}:
        return HttpResponseBadRequest("Choose a daily or weekly report view.")

    search = request.GET.get("q", "").strip()
    if search:
        reports = reports.filter(
            Q(intern__user__first_name__icontains=search)
            | Q(intern__user__last_name__icontains=search)
            | Q(intern__user__username__icontains=search)
            | Q(intern__student_id__icontains=search)
        )

    date_filter = request.GET.get("date", "").strip()
    week_filter = request.GET.get("week", "").strip()
    if date_filter:
        selected_date = parse_date(date_filter)
        if selected_date is None:
            return HttpResponseBadRequest("Enter a valid report date.")
        reports = reports.filter(work_date=selected_date)
    if week_filter:
        selected_week = parse_date(week_filter)
        if selected_week is None:
            return HttpResponseBadRequest("Enter a valid date for the selected week.")
        week_start = selected_week - timedelta(days=selected_week.weekday())
        reports = reports.filter(
            work_date__range=(week_start, week_start + timedelta(days=6)),
        )
    reports = reports.select_related("intern__user", "intern", "reviewer").order_by(
        "-work_date",
        "-submitted_at",
        "intern__user__last_name",
    )
    report_rows = list(reports)
    _attach_approved_report_hours(report_rows)
    paginator = Paginator(report_rows, 30)

    if view_mode == "daily":
        page_obj = paginator.get_page(request.GET.get("page"))
        rows = page_obj.object_list
        weekly_groups = []
        weekly_summary_count = 0
    else:
        grouped = {}
        for report in report_rows:
            week_start = report.work_date - timedelta(days=report.work_date.weekday())
            group = grouped.setdefault(week_start, {
                "start": week_start,
                "end": week_start + timedelta(days=6),
                "reports": [],
                "hours": 0,
                "hour_keys": set(),
            })
            group["reports"].append(report)
            hour_key = (report.intern_id, report.work_date)
            if hour_key not in group["hour_keys"]:
                group["hours"] += report.hours_worked
                group["hour_keys"].add(hour_key)
        weeks = sorted(grouped.values(), key=lambda week: week["start"], reverse=True)
        for week in weeks:
            week.pop("hour_keys")
        weekly_summary_count = len(weeks)
        page_obj = Paginator(weeks, 10).get_page(request.GET.get("page"))
        rows = []
        weekly_groups = page_obj.object_list

    params = request.GET.copy()
    params.pop("page", None)
    return {
        "view_mode": view_mode,
        "search": search,
        "date_filter": date_filter,
        "week_filter": week_filter,
        "page_obj": page_obj,
        "reports": rows,
        "weekly_groups": weekly_groups,
        "report_count": len(report_rows),
        "weekly_summary_count": weekly_summary_count,
        "is_admin": is_admin,
        "company": company,
        "query_string": params.urlencode(),
    }


@role_required(User.Role.COMPANY)
def company_daily_reports(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    context = _daily_report_history_context(
        request,
        DailyReport.objects.filter(intern__in=accepted_company_interns(company)),
        is_admin=False,
        company=company,
    )
    if isinstance(context, HttpResponse):
        return context
    return render(request, "portal/report_history.html", context)


@admin_required
def admin_daily_reports(request):
    context = _daily_report_history_context(
        request,
        DailyReport.objects.all(),
        is_admin=True,
    )
    if isinstance(context, HttpResponse):
        return context
    return render(request, "portal/report_history.html", context)


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


def _dtr_roster_context(request, interns, *, coordinator=False):
    placement = request.GET.get("placement", "")
    valid_placements = {choice[0] for choice in InternProfile.PlacementType.choices}
    if placement and placement not in valid_placements:
        return None, HttpResponseBadRequest("Invalid placement filter.")
    if placement:
        interns = interns.filter(placement_type=placement)

    search = request.GET.get("q", "").strip()
    if search:
        interns = interns.filter(
            Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(user__username__icontains=search)
            | Q(student_id__icontains=search)
        )

    companies = CompanyProfile.objects.none()
    external_hosts = []
    selected_company = None
    selected_host = ""
    if coordinator:
        companies = CompanyProfile.objects.filter(
            postings__applications__status=Application.Status.ACCEPTED,
        ).distinct().order_by("organization")
        external_hosts = list(
            InternProfile.objects.filter(
                placement_type=InternProfile.PlacementType.EXTERNAL,
            ).exclude(external_host="").order_by().values_list(
                "external_host",
                flat=True,
            ).distinct()
        )
        company_id = request.GET.get("company", "")
        if company_id:
            if not company_id.isdigit():
                return None, HttpResponseBadRequest("Invalid company filter.")
            selected_company = get_object_or_404(companies, pk=company_id)
            interns = interns.filter(
                applications__posting__company=selected_company,
                applications__status=Application.Status.ACCEPTED,
            ).distinct()
        selected_host = request.GET.get("host", "")
        if selected_host:
            if selected_host not in external_hosts:
                return None, HttpResponseBadRequest("Invalid external host filter.")
            interns = interns.filter(
                placement_type=InternProfile.PlacementType.EXTERNAL,
                external_host=selected_host,
            )

    interns = interns.select_related("user").order_by(
        "user__last_name",
        "user__first_name",
        "user__username",
    )
    page = Paginator(interns, 25).get_page(request.GET.get("page"))
    hours_by_intern = _approved_hours_by_intern(
        [intern.pk for intern in page.object_list],
    )
    accepted_company_names = {}
    if coordinator:
        page_intern_ids = [intern.pk for intern in page.object_list]
        applications = Application.objects.filter(
            intern_id__in=page_intern_ids,
            status=Application.Status.ACCEPTED,
        ).select_related("posting__company")
        for application in applications:
            accepted_company_names.setdefault(application.intern_id, set()).add(
                application.posting.company.organization,
            )
    for intern in page.object_list:
        intern.approved_rendered_hours = hours_by_intern.get(intern.pk, 0)
        intern.completion_percent = round(
            intern.approved_rendered_hours * 100 / OJT_RENDERED_HOURS_CAP,
            1,
        )
        intern.accepted_company_names = sorted(accepted_company_names.get(intern.pk, set()))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return {
        "intern_page": page,
        "search": search,
        "selected_placement": placement,
        "companies": companies,
        "selected_company": selected_company,
        "external_hosts": external_hosts,
        "selected_host": selected_host,
        "placement_choices": (
            (InternProfile.PlacementType.PLATFORM, "Internal"),
            (InternProfile.PlacementType.EXTERNAL, "External"),
        ),
        "rendered_hours_cap": OJT_RENDERED_HOURS_CAP,
        "is_coordinator": coordinator,
        "query_string": query_params.urlencode(),
    }, None


@role_required(User.Role.COMPANY)
def company_dtr_summary(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    context, error = _dtr_roster_context(
        request,
        accepted_company_interns(company),
    )
    if error:
        return error
    context.update({
        "company": company,
        "detail_url_name": "portal:company_dtr_detail",
    })
    return render(request, "portal/dtr_summary.html", context)


@role_required(User.Role.COORDINATOR)
def coordinator_dtr_summary(request):
    context, error = _dtr_roster_context(
        request,
        InternProfile.objects.all(),
        coordinator=True,
    )
    if error:
        return error
    context["detail_url_name"] = "portal:coordinator_dtr_detail"
    return render(request, "portal/dtr_summary.html", context)


def _render_dtr_detail(request, intern, *, coordinator):
    context = _dtr_breakdown_context(intern, request.GET.get("period", "all"))
    context.update({
        "intern": intern,
        "today": timezone.localdate(),
        "is_coordinator": coordinator,
        "summary_url_name": (
            "portal:coordinator_dtr_summary"
            if coordinator
            else "portal:company_dtr_summary"
        ),
    })
    return render(request, "portal/dtr_detail.html", context)


@role_required(User.Role.COMPANY)
def company_dtr_detail(request, intern_id):
    company = get_object_or_404(CompanyProfile, user=request.user)
    intern = get_object_or_404(accepted_company_interns(company), pk=intern_id)
    return _render_dtr_detail(request, intern, coordinator=False)


@role_required(User.Role.COORDINATOR)
def coordinator_dtr_detail(request, intern_id):
    intern = get_object_or_404(InternProfile.objects.select_related("user"), pk=intern_id)
    return _render_dtr_detail(request, intern, coordinator=True)


@role_required(User.Role.COORDINATOR)
def coordinator_users(request):
    users = User.objects.filter(
        Q(role__in=(User.Role.INTERN, User.Role.COMPANY, User.Role.COORDINATOR))
        | Q(is_staff=True)
        | Q(is_superuser=True),
    )
    role_filter = request.GET.get("role", "")
    role_values = {
        User.Role.INTERN,
        User.Role.COMPANY,
        "admin",
    }
    if role_filter and role_filter not in role_values:
        return HttpResponseBadRequest("Invalid user role filter.")
    if role_filter == "admin":
        users = users.filter(
            Q(is_staff=True)
            | Q(is_superuser=True)
            | Q(role=User.Role.COORDINATOR)
        )
    elif role_filter:
        users = users.filter(role=role_filter, is_staff=False)

    placement_filter = request.GET.get("placement", "")
    if placement_filter not in {"", *InternProfile.PlacementType.values}:
        return HttpResponseBadRequest("Invalid placement type filter.")
    if placement_filter:
        users = users.filter(
            role=User.Role.INTERN,
            intern_profile__placement_type=placement_filter,
        )

    face_filter = request.GET.get("face", "")
    if face_filter not in {"", "enrolled", "not_enrolled"}:
        return HttpResponseBadRequest("Invalid face-enrollment filter.")
    if face_filter:
        enrollment_is_null = face_filter == "not_enrolled"
        users = users.filter(
            role=User.Role.INTERN,
            intern_profile__face_enrollment__isnull=enrollment_is_null,
        )

    search = request.GET.get("q", "").strip()
    if search:
        users = users.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
            | Q(intern_profile__student_id__icontains=search)
        )

    users = users.order_by("last_name", "first_name", "username").distinct().select_related(
        "intern_profile__face_enrollment",
        "company_profile",
    ).prefetch_related(
        "intern_profile__applications__posting__company",
        "intern_profile__attendance_worksites",
    )
    page = Paginator(users, 15).get_page(request.GET.get("page"))
    page_users = list(page.object_list)
    interns_by_user = {}
    companies_by_user = {}
    for user in page_users:
        try:
            interns_by_user[user.pk] = user.intern_profile
        except InternProfile.DoesNotExist:
            pass
        try:
            companies_by_user[user.pk] = user.company_profile
        except CompanyProfile.DoesNotExist:
            pass
    hours_by_intern = _approved_hours_by_intern(
        [intern.pk for intern in interns_by_user.values()],
    )
    for user in page_users:
        intern = interns_by_user.get(user.pk)
        company = companies_by_user.get(user.pk)
        user.directory_intern = intern
        user.directory_company = company
        user.directory_role = (
            "Admin"
            if user.is_admin
            else "Company"
            if user.role == User.Role.COMPANY
            else "Intern"
        )
        user.directory_placement = (
            (
                "Internal"
                if intern.placement_type == InternProfile.PlacementType.PLATFORM
                else "External"
            )
            if intern
            else "—"
        )
        if company:
            user.directory_company_name = company.organization
        elif intern and intern.placement_type == InternProfile.PlacementType.EXTERNAL:
            user.directory_company_name = intern.external_host or "External placement"
        elif intern:
            user.directory_company_name = ", ".join(sorted({
                application.posting.company.organization
                for application in intern.applications.all()
                if application.status == Application.Status.ACCEPTED
            })) or "—"
        else:
            user.directory_company_name = "—"
        user.directory_face_enrolled = bool(
            intern and hasattr(intern, "face_enrollment")
        )
        user.directory_hours = hours_by_intern.get(intern.pk, 0) if intern else None
        user.directory_resettable_worksite = (
            any(
                worksite.is_active
                or worksite.approval_status == WorksiteLocation.ApprovalStatus.PENDING
                for worksite in intern.attendance_worksites.all()
            )
            if intern
            else False
        )
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(request, "portal/coordinator/users.html", {
        "users_page": page,
        "search": search,
        "selected_role": role_filter,
        "selected_placement": placement_filter,
        "selected_face": face_filter,
        "role_choices": (
            (User.Role.INTERN, "Intern"),
            (User.Role.COMPANY, "Company"),
            ("admin", "Admin"),
        ),
        "placement_choices": (
            (InternProfile.PlacementType.PLATFORM, "Internal"),
            (InternProfile.PlacementType.EXTERNAL, "External"),
        ),
        "face_choices": (
            ("enrolled", "Enrolled"),
            ("not_enrolled", "Not Enrolled"),
        ),
        "query_string": query_params.urlencode(),
        "rendered_hours_cap": OJT_RENDERED_HOURS_CAP,
    })


@admin_required
@require_POST
def coordinator_reset_user_face(request, intern_id):
    intern = get_object_or_404(InternProfile.objects.select_related("user"), pk=intern_id)
    _delete_intern_face_enrollment(request, intern)
    return redirect("portal:coordinator_users")


@admin_required
@require_POST
def coordinator_reset_user_worksite(request, intern_id):
    intern = get_object_or_404(InternProfile.objects.select_related("user"), pk=intern_id)
    review_reason = request.POST.get("review_reason", "").strip()
    if not 1 <= len(review_reason) <= 1000:
        return HttpResponseBadRequest("Provide instructions of no more than 1000 characters for the new worksite submission.")
    with transaction.atomic():
        InternProfile.objects.select_for_update().get(pk=intern.pk)
        worksites = list(
            WorksiteLocation.objects.select_for_update().filter(intern=intern).filter(
                Q(is_active=True)
                | Q(approval_status=WorksiteLocation.ApprovalStatus.PENDING)
            )
        )
        if not worksites:
            messages.info(request, "This intern has no active or pending worksite to reset.")
            return redirect("portal:coordinator_users")
        reviewed_at = timezone.now()
        for worksite in worksites:
            worksite.is_active = False
            worksite.approval_status = WorksiteLocation.ApprovalStatus.RESUBMISSION_REQUESTED
            worksite.rejection_reason = review_reason
            worksite.reviewed_by = request.user
            worksite.reviewed_at = reviewed_at
            worksite.save(update_fields=(
                "is_active",
                "approval_status",
                "rejection_reason",
                "reviewed_by",
                "reviewed_at",
            ))
            audit(request.user, "worksite.reset_by_coordinator", worksite)
    messages.success(request, f"Worksite access for {intern.user.get_full_name() or intern.user.username} was reset.")
    return redirect("portal:coordinator_users")


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
def company_attendance_history(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    logs = company_attendance_logs(company)
    search = request.GET.get("q", "").strip()
    if search:
        logs = logs.filter(
            Q(intern__user__first_name__icontains=search)
            | Q(intern__user__last_name__icontains=search)
            | Q(intern__user__username__icontains=search)
            | Q(intern__student_id__icontains=search)
        )
    date_filter = request.GET.get("date", "").strip()
    if date_filter:
        selected_date = parse_date(date_filter)
        if selected_date is None:
            return HttpResponseBadRequest("Enter a valid attendance date.")
        logs = logs.filter(work_date=selected_date)
    page_obj = Paginator(logs, 50).get_page(request.GET.get("page"))
    return render(request, "portal/company/attendance_history.html", {
        "logs": page_obj.object_list,
        "page_obj": page_obj,
        "search": search,
        "date_filter": date_filter,
    })


@role_required(User.Role.COMPANY)
def company_dashboard(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    accepted_interns = accepted_company_interns(company)
    return render(request, "portal/company/dashboard.html", {
        "company": company,
        "pending_dtr_count": pending_attendance_approval_count(company_attendance_logs(company)),
        "active_worksite_count": company.worksites.filter(is_active=True).count(),
        "accepted_intern_count": accepted_interns.count(),
        "daily_report_count": DailyReport.objects.filter(intern__in=accepted_interns).count(),
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
    logs = company_attendance_logs(company).prefetch_related("face_captures", "face_captures__reviewed_by")
    return render(request, "portal/company/dtr_queue.html", {
        "logs": logs[:100],
        "pending_dtr_count": pending_attendance_approval_count(logs),
        "now": timezone.now(),
    })


@role_required(User.Role.COMPANY)
@require_POST
def company_approve_dtr(request, log_id):
    company = get_object_or_404(CompanyProfile, user=request.user)
    log = get_object_or_404(company_attendance_logs(company), pk=log_id)
    return _approve_attendance_time(request, log, "portal:company_dtr_queue", "attendance.approved_by_company")


def _reviewable_face_captures(request):
    captures = AttendanceFaceCapture.objects.select_related(
        "attendance_log__intern__user",
        "reviewed_by",
    )
    if request.user.role == User.Role.COMPANY and not request.user.is_admin:
        company = get_object_or_404(CompanyProfile, user=request.user)
        captures = captures.filter(attendance_log__in=company_attendance_logs(company))
    return captures


def _review_face_capture(request, capture_id, queue_name, audit_action):
    action = request.POST.get("action")
    if action not in {"approve", "reject"}:
        messages.error(request, "Choose whether to approve or reject this attendance exception.")
        return redirect(queue_name)

    with transaction.atomic():
        capture = get_object_or_404(
            _reviewable_face_captures(request).select_related(None).select_for_update(),
            pk=capture_id,
        )
        log = AttendanceLog.objects.select_for_update().get(pk=capture.attendance_log_id)
        if capture.review_status != AttendanceFaceCapture.ReviewStatus.PENDING:
            messages.info(request, "This attendance exception has already been reviewed.")
            return redirect(queue_name)

        reviewed_at = timezone.now()
        capture.reviewed_by = request.user
        capture.reviewed_at = reviewed_at
        fields = _location_fields(
            "clock_in" if capture.event == AttendanceFaceCapture.Event.TIME_IN else "clock_out"
        )
        if action == "reject":
            if getattr(log, fields["timestamp"]) == capture.captured_at and not getattr(log, fields["approval"]):
                setattr(log, fields["timestamp"], None)
                log.save(update_fields=(fields["timestamp"],))
            capture.review_status = AttendanceFaceCapture.ReviewStatus.REJECTED
            capture.save(update_fields=("review_status", "reviewed_by", "reviewed_at"))
            audit(request.user, f"{audit_action}.face_exception_rejected", log, capture_id=capture.pk)
            messages.success(request, "Attendance exception rejected; no time was recorded.")
            return redirect(queue_name)

        recorded_at = getattr(log, fields["timestamp"])
        if recorded_at and recorded_at != capture.captured_at:
            capture.review_status = AttendanceFaceCapture.ReviewStatus.REJECTED
            capture.save(update_fields=("review_status", "reviewed_by", "reviewed_at"))
            messages.error(request, "This time event has already been recorded; the exception was closed without changing attendance.")
            return redirect(queue_name)

        capture.review_status = AttendanceFaceCapture.ReviewStatus.APPROVED
        capture.save(update_fields=("review_status", "reviewed_by", "reviewed_at"))
        if not recorded_at:
            setattr(log, fields["timestamp"], capture.captured_at)
        if capture.location_status != AttendanceFaceCapture.LocationStatus.UNKNOWN:
            setattr(log, fields["latitude"], capture.latitude)
            setattr(log, fields["longitude"], capture.longitude)
            setattr(
                log,
                fields["within_location"],
                capture.location_status == AttendanceFaceCapture.LocationStatus.INSIDE,
            )
            setattr(log, fields["distance"], capture.distance_meters)
            setattr(log, fields["allowed_radius"], capture.allowed_radius_meters)
            setattr(log, fields["reason"], capture.location_reason)
        setattr(log, fields["approval"], True)
        setattr(log, fields["face_status"], capture.face_status)
        setattr(log, fields["face_score"], capture.face_score)
        update_fields = [
            fields["timestamp"],
            fields["approval"],
            fields["face_status"],
            fields["face_score"],
        ]
        if capture.location_status != AttendanceFaceCapture.LocationStatus.UNKNOWN:
            update_fields.extend((
                fields["latitude"],
                fields["longitude"],
                fields["within_location"],
                fields["distance"],
                fields["allowed_radius"],
                fields["reason"],
            ))
        log.save(update_fields=update_fields)
        audit(request.user, f"{audit_action}.face_exception_approved", log, capture_id=capture.pk)
        messages.success(request, f"{capture.get_event_display()} recorded at the captured time and approved.")
    return redirect(queue_name)


@role_required(User.Role.COORDINATOR, User.Role.COMPANY)
@require_POST
def review_attendance_face_capture(request, capture_id):
    queue_name = (
        "portal:dtr_queue"
        if request.user.is_admin
        else "portal:company_dtr_queue"
    )
    return _review_face_capture(
        request,
        capture_id,
        queue_name,
        "attendance.approved_by_company" if queue_name == "portal:company_dtr_queue" else "attendance.approved",
    )


@role_required(User.Role.COORDINATOR, User.Role.COMPANY)
def attendance_face_capture_image(request, capture_id):
    capture = get_object_or_404(
        _reviewable_face_captures(request),
        pk=capture_id,
    )
    if not capture.encrypted_image or capture.image_expires_at <= timezone.now():
        raise Http404("This attendance image has expired or is unavailable.")
    try:
        image_bytes = decrypt_face_capture(capture.encrypted_image)
    except FaceModelError:
        LOGGER.exception("Could not decrypt attendance face capture %s.", capture.pk)
        return HttpResponse("Attendance capture could not be decrypted.", status=500)
    response = HttpResponse(image_bytes, content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def _approve_attendance_time(request, log, queue_name, audit_action):
    approval = {
        "time_in": ("clock_in", "time_in_approved", "Time in"),
        "time_out": ("clock_out", "time_out_approved", "Time out"),
    }.get(request.POST.get("event"))
    if approval is None:
        messages.error(request, "Choose whether to approve time in or time out.")
        return redirect(queue_name)

    timestamp_field, approval_field, label = approval
    capture_event = (
        AttendanceFaceCapture.Event.TIME_IN
        if request.POST["event"] == "time_in"
        else AttendanceFaceCapture.Event.TIME_OUT
    )
    if log.face_captures.filter(
        event=capture_event,
        review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
    ).exists():
        messages.error(request, f"Review the {label.lower()} face capture before approving this event.")
    elif not getattr(log, timestamp_field):
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
    _raise_for_unavailable_scorecard_intern(interns, request)
    selection_form = ScorecardSelectionForm(
        request.POST if request.method == "POST" else None,
        interns=interns,
        initial={"week_start": request.GET.get("week", "")},
    )
    form = ScorecardForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if selection_form.is_valid():
            selected_intern = selection_form.cleaned_data["intern_id"]
            week_start = selection_form.cleaned_data["week_start"]
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
        "selection_form": selection_form,
        "form": form,
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
    audit(request.user, f"application.{application.status}", application)
    messages.success(request, f"Application {application.get_status_display().lower()}.")
    return redirect("portal:applicant_screening")


@role_required(User.Role.COORDINATOR)
def coordinator_dashboard(request):
    external_interns = InternProfile.objects.filter(
        placement_type=InternProfile.PlacementType.EXTERNAL,
    )
    context = {
        "intern_count": InternProfile.objects.count(),
        "pending_dtr_count": pending_attendance_approval_count(AttendanceLog.objects.all()),
        "pending_daily_report_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
        "external_interns_without_worksite_count": external_interns.exclude(
            attendance_worksites__is_active=True,
        ).distinct().count(),
        "active_worksite_count": WorksiteLocation.objects.filter(is_active=True).count(),
        "pending_worksite_request_count": WorksiteLocation.objects.filter(
            intern__isnull=False,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).count(),
        "pending_face_reenrollment_count": FaceReenrollmentRequest.objects.filter(
            status=FaceReenrollmentRequest.Status.PENDING,
        ).count(),
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
    logs = AttendanceLog.objects.select_related("intern__user").prefetch_related(
        "face_captures",
        "face_captures__reviewed_by",
    ).order_by("-work_date")
    return render(request, "portal/coordinator/dtr_queue.html", {
        "logs": logs[:100],
        "pending_dtr_count": pending_attendance_approval_count(logs),
        "now": timezone.now(),
    })


@role_required(User.Role.COORDINATOR)
@require_POST
def approve_dtr(request, log_id):
    log = get_object_or_404(AttendanceLog, pk=log_id)
    return _approve_attendance_time(request, log, "portal:dtr_queue", "attendance.approved")


@role_required(User.Role.COORDINATOR)
def scorecards(request):
    interns = InternProfile.objects.select_related("user").order_by("user__last_name", "user__first_name")
    _raise_for_unavailable_scorecard_intern(interns, request)
    selection_form = ScorecardSelectionForm(
        request.POST if request.method == "POST" else None,
        interns=interns,
        initial={"week_start": request.GET.get("week", "")},
    )
    form = ScorecardForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if selection_form.is_valid():
            selected_intern = selection_form.cleaned_data["intern_id"]
            week_start = selection_form.cleaned_data["week_start"]
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
    return render(request, "portal/coordinator/scorecards.html", {
        "interns": interns,
        "scorecards": Scorecard.objects.filter(company__isnull=True).select_related("intern__user", "reviewer")[:50],
        "selection_form": selection_form,
        "form": form,
    })


def _raise_for_unavailable_scorecard_intern(interns, request):
    intern_id = request.POST.get("intern_id", "")
    if intern_id.isdecimal() and len(intern_id) <= 19:
        numeric_id = int(intern_id)
        if numeric_id <= 9223372036854775807 and not interns.filter(pk=numeric_id).exists():
            raise Http404


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