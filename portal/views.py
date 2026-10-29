import csv
import logging
import mimetypes
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path

import numpy as np
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError
from zipfile import BadZipFile, LargeZipFile, ZipFile

from accounts.models import User

from .forms import CompanyProfileForm, DailyReportForm, ExternalWorksiteLocationForm, InternProfileForm, InternWorksiteRequestForm, PostingForm, ScorecardForm, WorksiteLocationForm
from .models import AcademicPeriod, Application, AttendanceFaceCapture, AttendanceLog, AuditLog, CompanyProfile, DailyReport, FACE_CONSENT_VERSION, FaceEnrollment, InternProfile, OJT_RENDERED_HOURS_CAP, Posting, RiskAssessment, Scorecard, WorksiteLocation, pending_attendance_approval_count
from .utils import attendance_pdf, audit, certificate_pdf, haversine_distance_km
from ml_engine.face_capture import FaceCaptureError, decode_camera_image, encode_camera_image
from ml_engine.face_detector import detect_faces
from ml_engine.face_recognition import (
    FaceImageError,
    FaceModelError,
    compare_face_embedding,
    create_face_embedding,
    decrypt_face_capture,
    encrypt_face_capture,
    encrypt_embedding,
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
            if not (request.user.is_staff or request.user.role in roles):
                return render(request, "errors/403.html", status=403)
            return view(request, *args, **kwargs)

        return wrapped

    return decorate


def intern_api_required():
    def decorate(view):
        @wraps(view)
        @require_POST
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return JsonResponse({"error": "Your session expired. Sign in again and retry."}, status=401)
            if request.user.is_staff or request.user.role != User.Role.INTERN:
                return JsonResponse({"error": "Only interns can use face capture."}, status=403)

            if not InternProfile.objects.filter(user=request.user).exists():
                return JsonResponse({"error": "Complete your intern profile before face capture."}, status=404)
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
    enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    return render(request, "portal/intern/profile.html", {
        "form": form,
        "worksite_request_form": InternWorksiteRequestForm(),
        "intern": intern,
        "can_request_intern_worksite": (
            intern.placement_type == InternProfile.PlacementType.EXTERNAL
            or Application.objects.filter(
                intern=intern,
                status=Application.Status.ACCEPTED,
            ).exists()
        ),
        "face_enrollment": enrollment,
        "face_enrollment_current": enrollment is not None and enrollment.consent_text_version == FACE_CONSENT_VERSION,
        "intern_worksite": intern.attendance_worksites.filter(
            Q(approval_status=WorksiteLocation.ApprovalStatus.PENDING)
            | Q(approval_status=WorksiteLocation.ApprovalStatus.APPROVED, is_active=True)
        ).order_by("-created_at").first(),
    })


@role_required(User.Role.INTERN)
@require_POST
def intern_worksite_registration(request):
    form = InternWorksiteRequestForm(request.POST)
    intern = get_object_or_404(InternProfile, user=request.user)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("portal:profile")

    if intern.placement_type == InternProfile.PlacementType.PLATFORM:
        has_accepted_placement = Application.objects.filter(
            intern=intern,
            status=Application.Status.ACCEPTED,
        ).exists()
        if not has_accepted_placement:
            messages.error(request, "An accepted company placement is required before requesting a worksite.")
            return redirect("portal:profile")

    with transaction.atomic():
        locked_intern = InternProfile.objects.select_for_update().get(pk=intern.pk)
        existing_request = WorksiteLocation.objects.filter(intern=locked_intern).filter(
            Q(approval_status=WorksiteLocation.ApprovalStatus.PENDING)
            | Q(approval_status=WorksiteLocation.ApprovalStatus.APPROVED, is_active=True)
        ).exists()
        if existing_request:
            messages.error(
                request,
                "A worksite request is already pending or approved. Ask your supervisor to review or reset it before registering again.",
            )
            return redirect("portal:profile")

        worksite = form.save(commit=False)
        worksite.intern = locked_intern
        worksite.radius_meters = 100
        worksite.approval_status = WorksiteLocation.ApprovalStatus.PENDING
        worksite.is_active = False
        worksite.created_by = request.user
        worksite.save()
        audit(request.user, "worksite.registration_requested", worksite)

    messages.success(request, "Your worksite location was sent to your supervisor for review.")
    return redirect("portal:profile")


@role_required(User.Role.COMPANY)
def company_worksites(request):
    company = get_object_or_404(CompanyProfile, user=request.user)
    accepted_interns = accepted_company_interns(company)
    if request.method == "POST" and request.POST.get("action") == "deactivate":
        worksite = get_object_or_404(company.worksites, pk=request.POST.get("worksite_id"))
        worksite.is_active = False
        worksite.save(update_fields=("is_active",))
        audit(request.user, "worksite.deactivated", worksite)
        messages.success(request, f"{worksite.name} was deactivated.")
        return redirect("portal:company_worksites")

    if request.method == "POST" and request.POST.get("action") in {
        "review_intern_request",
        "reset_intern_location",
    }:
        worksite = get_object_or_404(
            WorksiteLocation.objects.filter(intern__in=accepted_interns, company__isnull=True),
            pk=request.POST.get("worksite_id"),
        )
        action = request.POST.get("action")
        if action == "review_intern_request":
            decision = request.POST.get("decision")
            if worksite.approval_status != WorksiteLocation.ApprovalStatus.PENDING:
                messages.info(request, "This worksite request has already been reviewed.")
            elif decision not in {"approve", "reject"}:
                messages.error(request, "Choose whether to approve or reject the worksite request.")
            else:
                worksite.approval_status = (
                    WorksiteLocation.ApprovalStatus.APPROVED
                    if decision == "approve"
                    else WorksiteLocation.ApprovalStatus.REJECTED
                )
                worksite.is_active = decision == "approve"
                worksite.reviewed_by = request.user
                worksite.reviewed_at = timezone.now()
                worksite.save(update_fields=(
                    "approval_status",
                    "is_active",
                    "reviewed_by",
                    "reviewed_at",
                ))
                audit(request.user, f"worksite.registration_{decision}", worksite)
                outcome = "approved" if decision == "approve" else "rejected"
                messages.success(request, f"{worksite.name} request {outcome}.")
        elif worksite.is_active and worksite.approval_status == WorksiteLocation.ApprovalStatus.APPROVED:
            worksite.is_active = False
            worksite.reviewed_by = request.user
            worksite.reviewed_at = timezone.now()
            worksite.save(update_fields=("is_active", "reviewed_by", "reviewed_at"))
            audit(request.user, "worksite.registration_reset", worksite)
            messages.success(request, f"{worksite.name} was reset. The intern can request a new location.")
        else:
            messages.error(request, "Only an approved, active intern worksite can be reset.")
        return redirect("portal:company_worksites")

    form = WorksiteLocationForm(request.POST or None)
    if request.method == "POST" and request.POST.get("action") == "create":
        if form.is_valid():
            worksite = form.save(commit=False)
            worksite.company = company
            worksite.created_by = request.user
            worksite.save()
            audit(request.user, "worksite.enrolled", worksite)
            messages.success(request, f"{worksite.name} was enrolled as an attendance location.")
            return redirect("portal:company_worksites")
    elif request.method == "POST":
        messages.error(request, "Choose a valid worksite action.")

    return render(request, "portal/worksite_locations.html", {
        "form": form,
        "worksites": WorksiteLocation.objects.filter(
            Q(company=company)
            | Q(company__isnull=True, intern__in=accepted_interns)
        ).select_related("intern__user", "created_by", "reviewed_by"),
        "pending_worksite_count": WorksiteLocation.objects.filter(
            company__isnull=True,
            intern__in=accepted_interns,
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).count(),
        "is_coordinator": False,
        "page_title": "Worksite locations",
        "page_description": "Enroll approved company worksites and review location requests from your accepted interns. An approved intern-specific location takes precedence for that intern.",
    })


@role_required(User.Role.COORDINATOR)
def coordinator_external_worksites(request):
    external_interns = InternProfile.objects.filter(
        placement_type=InternProfile.PlacementType.EXTERNAL,
    )
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

    if request.method == "POST" and request.POST.get("action") == "review_intern_request":
        worksite = get_object_or_404(
            WorksiteLocation.objects.filter(company__isnull=True, intern__in=external_interns),
            pk=request.POST.get("worksite_id"),
        )
        decision = request.POST.get("decision")
        if worksite.approval_status != WorksiteLocation.ApprovalStatus.PENDING:
            messages.info(request, "This worksite request has already been reviewed.")
        elif decision not in {"approve", "reject"}:
            messages.error(request, "Choose whether to approve or reject the worksite request.")
        else:
            worksite.approval_status = (
                WorksiteLocation.ApprovalStatus.APPROVED
                if decision == "approve"
                else WorksiteLocation.ApprovalStatus.REJECTED
            )
            worksite.is_active = decision == "approve"
            worksite.reviewed_by = request.user
            worksite.reviewed_at = timezone.now()
            worksite.save(update_fields=(
                "approval_status",
                "is_active",
                "reviewed_by",
                "reviewed_at",
            ))
            audit(request.user, f"worksite.registration_{decision}", worksite)
            outcome = "approved" if decision == "approve" else "rejected"
            messages.success(request, f"{worksite.name} request {outcome}.")
        return redirect("portal:coordinator_external_worksites")

    form = ExternalWorksiteLocationForm(request.POST or None)
    if request.method == "POST" and request.POST.get("action") == "create":
        if form.is_valid():
            worksite = form.save(commit=False)
            worksite.created_by = request.user
            worksite.save()
            audit(request.user, "worksite.assigned_to_external_intern", worksite)
            messages.success(
                request,
                f"{worksite.name} was assigned to {worksite.intern.user.get_full_name() or worksite.intern.user.username}.",
            )
            return redirect("portal:coordinator_external_worksites")
    elif request.method == "POST":
        messages.error(request, "Choose a valid worksite action.")

    worksites = WorksiteLocation.objects.filter(
        company__isnull=True,
        intern__placement_type=InternProfile.PlacementType.EXTERNAL,
    ).select_related("intern__user", "created_by")
    return render(request, "portal/worksite_locations.html", {
        "form": form,
        "worksites": worksites,
        "pending_worksite_count": worksites.filter(
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).count(),
        "is_coordinator": True,
        "page_title": "External intern worksites",
        "page_description": "Assign worksite locations to external interns or review their self-registration requests before attendance can use them.",
    })


@role_required(User.Role.INTERN)
@require_POST
def intern_face_enrollment(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    if request.POST.get("consent_confirmed") != "on":
        messages.error(request, "Confirm your informed consent before enrolling your face.")
        return redirect("portal:profile")

    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
        embedding = encrypt_embedding(create_face_embedding(image))
    except (FaceCaptureError, FaceImageError) as exc:
        messages.error(request, str(exc))
        return redirect("portal:profile")
    except FaceModelError:
        LOGGER.exception("Face enrollment failed for intern %s.", intern.pk)
        messages.error(request, "Face enrollment could not be completed because the models are unavailable.")
        return redirect("portal:profile")

    _, created = FaceEnrollment.objects.update_or_create(
        intern=intern,
        defaults={
            "encrypted_embedding": embedding,
            "enrolled_by": request.user,
            "consent_confirmed_at": timezone.now(),
            "consent_text_version": FACE_CONSENT_VERSION,
        },
    )
    audit(request.user, "face_enrollment.created" if created else "face_enrollment.updated", intern)
    messages.success(request, "Your encrypted face enrollment is saved. You can now use face recognition for attendance.")
    return redirect("portal:profile")


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
    if not latitude_value.is_finite() or not longitude_value.is_finite():
        return None, None
    if not Decimal("-90") <= latitude_value <= Decimal("90") or not Decimal("-180") <= longitude_value <= Decimal("180"):
        return None, None
    return latitude_value, longitude_value


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
    intern_location = WorksiteLocation.objects.filter(
        intern=intern,
        is_active=True,
        approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
    )
    if intern_location.exists():
        return intern_location

    locations = WorksiteLocation.objects.filter(
        is_active=True,
        approval_status=WorksiteLocation.ApprovalStatus.APPROVED,
    )
    if intern.placement_type == InternProfile.PlacementType.EXTERNAL:
        return locations.filter(company__isnull=True, intern=intern)

    accepted_companies = CompanyProfile.objects.filter(
        postings__applications__intern=intern,
        postings__applications__status=Application.Status.ACCEPTED,
    )
    return locations.filter(
        company__in=accepted_companies,
        intern__isnull=True,
    ).distinct()


def _attendance_location_status(worksites, latitude, longitude):
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
        if distance_meters <= worksite.radius_meters:
            distances.append((distance_meters, worksite.radius_meters, True))
        else:
            distances.append((distance_meters, worksite.radius_meters, False))

    if not distances:
        return AttendanceFaceCapture.LocationStatus.UNAVAILABLE, None, None

    eligible_sites = [entry for entry in distances if entry[2]]
    distance_meters, radius_meters, within_location = min(
        eligible_sites or distances,
        key=lambda entry: entry[0],
    )
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
    image_data = request.POST.get("face_image", "").strip()
    if not image_data:
        return AttendanceLog.FaceCheckStatus.NOT_CAPTURED, None, None

    try:
        image = decode_camera_image(image_data)
    except FaceCaptureError as exc:
        LOGGER.warning("Could not read face image for intern %s: %s", intern.pk, exc)
        return AttendanceLog.FaceCheckStatus.ERROR, None, None

    _log_face_image_diagnostics(image, intern_id=intern.pk, endpoint="attendance")

    enrollment = FaceEnrollment.objects.filter(intern=intern).first()
    if enrollment is None:
        return AttendanceLog.FaceCheckStatus.NOT_ENROLLED, None, None
    if enrollment.consent_text_version != FACE_CONSENT_VERSION:
        return AttendanceLog.FaceCheckStatus.CONSENT_REQUIRED, None, None

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


def _face_check_feedback(face_status):
    if face_status == AttendanceLog.FaceCheckStatus.MATCHED:
        return {
            "quality": "good",
            "label": "Good — face match candidate",
            "message": "One face was detected and it matches the enrolled reference.",
        }
    if face_status == AttendanceLog.FaceCheckStatus.NOT_MATCHED:
        return {
            "quality": "needs_review",
            "label": "Needs review — face detected",
            "message": "A face was detected, but the system could not confirm a match. Retake the frame or submit it for company/admin review.",
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
            "The face could not be checked. Retake the frame or submit it as an exception for company/admin review.",
        ),
    }


@intern_api_required()
def attendance_face_preview(request):
    action = request.POST.get("action")
    if action not in {"clock_in", "clock_out"}:
        return JsonResponse({"error": "Unknown attendance action."}, status=400)

    intern = InternProfile.objects.get(user=request.user)
    today = timezone.localdate()
    log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
    if not _worksites_for_attendance(intern).exists():
        return JsonResponse(
            {"error": "No active worksite is assigned to your placement. Ask your coordinator or host company to enroll one."},
            status=409,
        )
    latitude, longitude = _coordinates(request, action)
    location_status, distance_meters, allowed_radius_meters = _attendance_location_status(
        _worksites_for_attendance(intern),
        latitude,
        longitude,
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
    return JsonResponse({
        **_face_check_feedback(face_status),
        "similarity": face_score,
        "location_status": location_status,
        "distance_meters": distance_meters,
        "allowed_radius_meters": allowed_radius_meters,
        "location_requires_reason": location_status != AttendanceFaceCapture.LocationStatus.INSIDE,
    })


@intern_api_required()
def face_detection_preview(request):
    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
    except FaceCaptureError as exc:
        return JsonResponse({"face_count": 0, "error": str(exc)}, status=400)
    try:
        detection = detect_faces(image)
    except (FileNotFoundError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        _log_face_image_diagnostics(image, intern_id=request.user.pk, endpoint="face_detection")
        LOGGER.exception("Live face detection failed for intern %s.", request.user.pk)
        return JsonResponse(
            {"face_count": 0, "error": "The face-detection model is unavailable. Try again shortly."},
            status=503,
        )
    _log_face_image_diagnostics(
        image,
        intern_id=request.user.pk,
        endpoint="face_detection",
        face_count=detection["face_count"],
    )
    return JsonResponse({
        "face_count": detection["face_count"],
        "face_detected": detection["face_detected"],
    })


@intern_api_required()
def face_enrollment_preview(request):
    try:
        image = decode_camera_image(request.POST.get("face_image", ""))
        create_face_embedding(image)
    except (FaceCaptureError, FaceImageError) as exc:
        return JsonResponse({
            "quality": "poor",
            "label": "Poor enrollment capture",
            "message": str(exc),
        })
    except FaceModelError:
        LOGGER.exception("Face enrollment preview failed for intern %s.", request.user.pk)
        return JsonResponse({
            "quality": "poor",
            "label": "Face recognition unavailable",
            "message": "The face-recognition models could not process this image. Retake it or try again later.",
        }, status=503)
    return JsonResponse({
        "quality": "good",
        "label": "Face detected and ready",
        "message": "One face was detected and can be used for enrollment. Submit to save the encrypted face template.",
    })


@role_required(User.Role.INTERN)
def attendance(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    worksite_locations = _worksites_for_attendance(intern)
    today = timezone.localdate()
    if request.method == "POST":
        action = request.POST.get("action")
        if action not in {"clock_in", "clock_out"}:
            return HttpResponseBadRequest("Unknown attendance action.")
        if action == "clock_out":
            existing_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
            if not existing_log or not existing_log.clock_in:
                messages.error(request, "Record time in before recording time out.")
                return redirect("portal:attendance")
            if not existing_log.time_in_approved:
                messages.error(request, "Your time-in must be approved by the company/admin before you can time out.")
                return redirect("portal:attendance")
        if not worksite_locations.exists():
            messages.error(
                request,
                "No active worksite is assigned to your placement. Ask your coordinator or host company to enroll one.",
            )
            return redirect("portal:attendance")
        latitude, longitude = _coordinates(request, action)
        location_status, distance_meters, allowed_radius_meters = _attendance_location_status(
            worksite_locations,
            latitude,
            longitude,
        )
        location_reason = request.POST.get(f"{action}_location_reason", "").strip()[:1000]
        requires_location_reason = location_status != AttendanceFaceCapture.LocationStatus.INSIDE
        if requires_location_reason and len(location_reason) < 10:
            messages.error(
                request,
                "Your location is outside the assigned worksite radius or GPS is unavailable. "
                "Provide an explanation of at least 10 characters to submit this attendance for supervisor review.",
            )
            return redirect("portal:attendance")
        event = AttendanceFaceCapture.Event.TIME_IN if action == "clock_in" else AttendanceFaceCapture.Event.TIME_OUT
        existing_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
        if existing_log and existing_log.face_captures.filter(
            event=event,
            review_status=AttendanceFaceCapture.ReviewStatus.PENDING,
        ).exists():
            messages.info(request, "This attendance event is already waiting for company/admin review.")
            return redirect("portal:attendance")
        face_status, face_score, encrypted_image = _attendance_face_check(request, intern)
        captured_at = timezone.now()
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
                    messages.info(request, "This attendance event is already waiting for company/admin review.")
                    return redirect("portal:attendance")
                if getattr(log, fields["timestamp"]):
                    messages.info(request, f"You have already recorded {event.replace('_', ' ')} today.")
                    return redirect("portal:attendance")

                automatically_approved = (
                    face_status == AttendanceLog.FaceCheckStatus.MATCHED
                    and location_status == AttendanceFaceCapture.LocationStatus.INSIDE
                )
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
                    messages.warning(
                        request,
                        f"{event.replace('_', ' ').title()} was submitted with a location exception and is awaiting supervisor review.",
                    )
                else:
                    audit(request.user, "attendance.face_review_requested", log, event=event, capture_id=capture.pk)
                    messages.warning(
                        request,
                        "Face verification did not pass. Attendance is awaiting company/admin review.",
                    )
        except IntegrityError:
            messages.info(request, "Today's attendance record was already created. Refresh and try again.")
        return redirect("portal:attendance")

    today_log = AttendanceLog.objects.filter(intern=intern, work_date=today).first()
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
        "location_enrolled": worksite_locations.exists(),
        "worksite_locations": worksite_locations,
        "pending_worksite_request": intern.attendance_worksites.filter(
            approval_status=WorksiteLocation.ApprovalStatus.PENDING,
        ).first(),
        "pending_face_events": pending_face_events,
        **dtr_context,
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
                    AttendanceFaceCapture.objects.filter(attendance_log__intern=intern).delete()
                    enrollment.delete()
                    AttendanceLog.objects.filter(intern=intern).update(
                        clock_in_face_status=AttendanceLog.FaceCheckStatus.NOT_ATTEMPTED,
                        clock_in_face_score=None,
                        clock_out_face_status=AttendanceLog.FaceCheckStatus.NOT_ATTEMPTED,
                        clock_out_face_score=None,
                    )
                audit(request.user, "face_enrollment.deleted", intern)
                messages.success(request, "Face enrollment and its associated attendance captures were deleted.")
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
                "consent_text_version": FACE_CONSENT_VERSION,
            },
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
















@role_required(User.Role.INTERN)
def attendance_export(request):
    intern = get_object_or_404(InternProfile, user=request.user)
    document = attendance_pdf(intern, intern.attendance_logs.all()[:90])
    return FileResponse(document, as_attachment=True, filename="attendance-record.pdf", content_type="application/pdf")




@role_required(User.Role.INTERN)
def daily_report(request):
    intern = get_object_or_404(InternProfile, user=request.user)
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
            messages.success(request, "Daily report submitted for coordinator review.")
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
    return render(request, "portal/company/dashboard.html", {
        "company": company,
        "pending_dtr_count": pending_attendance_approval_count(company_attendance_logs(company)),
        "active_worksite_count": company.worksites.filter(is_active=True).count(),
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
    if request.user.role == User.Role.COMPANY and not request.user.is_staff:
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
        if request.user.is_staff or request.user.role == User.Role.COORDINATOR
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