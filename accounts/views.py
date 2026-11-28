from datetime import timedelta
import logging
from smtplib import SMTPException

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from portal.models import CompanyProfile, INSTITUTION_NAME, InternProfile

from .forms import (
    CompanyRegistrationForm,
    InternRegistrationForm,
    LoginForm,
    PasswordResetRequestForm,
    PasswordResetSetPasswordForm,
    PasswordResetVerifyForm,
)
from .models import PasswordResetCode, User

LOGGER = logging.getLogger(__name__)
PASSWORD_RESET_SESSION_KEYS = (
    "password_reset_email",
    "password_reset_code_id",
    "password_reset_user_id",
)


class AccountLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

def company_login_view(request):
    return redirect("accounts:login")


def _save_registration(form):
    user = form.save()
    if user.role == User.Role.INTERN:
        profile, _ = InternProfile.objects.get_or_create(user=user)
        for field_name in (
            "student_id",
            "course",
            "year_level",
            "placement_type",
        ):
            setattr(profile, field_name, form.cleaned_data[field_name])
        profile.university = INSTITUTION_NAME
        profile.save()
    elif user.role == User.Role.COMPANY:
        profile, _ = CompanyProfile.objects.get_or_create(user=user)
        for field_name in (
            "company_name",
            "company_address",
            "barangay",
            "city_municipality",
            "province",
            "postal_code",
            "industry_type",
            "contact_person_name",
            "contact_person_position",
            "official_email",
            "contact_number",
            "company_website",
            "moa_reference_number",
        ):
            setattr(profile, field_name, form.cleaned_data[field_name])
        profile.save()
    return user


@transaction.atomic
def register(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    form = InternRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_registration(form)
        messages.success(
            request,
            f"Account created for {INSTITUTION_NAME}. Sign in to continue.",
        )
        return redirect("accounts:login")
    return render(request, "accounts/register.html", {"form": form})


@transaction.atomic
def company_register_view(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    form = CompanyRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_registration(form)
        messages.success(request, "Company account created. Sign in to continue.")
        return redirect("accounts:login")
    return render(request, "accounts/company_register.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")


def _clear_password_reset_session(request):
    for key in PASSWORD_RESET_SESSION_KEYS:
        request.session.pop(key, None)


def _find_reset_user(email):
    if not email:
        return None
    users = list(
        User.objects.filter(email__iexact=email, is_active=True)
        .order_by("pk")[:2]
    )
    if len(users) != 1 or not users[0].has_usable_password():
        return None
    return users[0]


def _send_password_reset_code(user):
    now = timezone.now()
    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        latest = PasswordResetCode.objects.filter(
            user=locked_user,
            used_at__isnull=True,
            expires_at__gt=now,
        ).first()
        if latest and now - latest.created_at < timedelta(seconds=PasswordResetCode.RESEND_COOLDOWN_SECONDS):
            return "cooldown", latest
        reset_code, raw_code = PasswordResetCode.issue_for_user(locked_user)

    display_name = locked_user.get_full_name() or locked_user.username
    subject = "Fieldwork Portal — Password Reset Verification Code"
    message = (
        f"Hello {display_name},\n\n"
        f"Use the verification code below to reset your Fieldwork Portal password:\n\n"
        f"{raw_code}\n\n"
        f"This code expires in {PasswordResetCode.CODE_TTL_MINUTES} minutes.\n"
        "If you did not request a password reset, you can safely ignore this email."
    )
    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            [locked_user.email],
            fail_silently=False,
        )
    except (SMTPException, OSError):
        LOGGER.exception("Failed to send password reset code for user %s.", locked_user.pk)
        reset_code.used_at = timezone.now()
        reset_code.save(update_fields=("used_at",))
        return "error", None
    return "sent", reset_code


def password_reset_request(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    form = PasswordResetRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        _clear_password_reset_session(request)
        request.session["password_reset_email"] = email
        user = _find_reset_user(email)
        if user:
            status, _ = _send_password_reset_code(user)
            if status == "error":
                messages.error(
                    request,
                    "We could not send the verification email right now. Please try again later.",
                )
                return render(request, "accounts/password_reset_request.html", {"form": form})
        messages.info(
            request,
            "If an active account matches that email, a 6-digit verification code has been sent.",
        )
        return redirect("accounts:password_reset_verify")
    return render(request, "accounts/password_reset_request.html", {"form": form})


def password_reset_verify(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    session_email = request.session.get("password_reset_email", "")
    if request.method == "POST" and request.POST.get("action") == "resend":
        resend_email = (request.POST.get("email") or session_email).strip().lower()
        try:
            validate_email(resend_email)
        except ValidationError:
            form = PasswordResetVerifyForm(initial={"email": session_email})
            form.add_error("email", "Enter a valid email address before resending a code.")
            return render(request, "accounts/password_reset_verify.html", {"form": form})
        request.session["password_reset_email"] = resend_email
        user = _find_reset_user(resend_email)
        if user:
            status, _ = _send_password_reset_code(user)
            if status == "cooldown":
                messages.info(
                    request,
                    f"Please wait {PasswordResetCode.RESEND_COOLDOWN_SECONDS} seconds before requesting another code.",
                )
                return redirect("accounts:password_reset_verify")
            if status == "error":
                messages.error(
                    request,
                    "We could not send the verification email right now. Please try again later.",
                )
                return redirect("accounts:password_reset_verify")
        messages.success(
            request,
            "If an active account matches that email, a new 6-digit verification code has been sent.",
        )
        return redirect("accounts:password_reset_verify")

    form = PasswordResetVerifyForm(
        request.POST or None,
        initial={"email": session_email},
    )
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        code = form.cleaned_data["code"]
        request.session["password_reset_email"] = email
        user = _find_reset_user(email)
        if not user:
            form.add_error(None, "That verification code is invalid or has expired. Request a new code.")
            return render(request, "accounts/password_reset_verify.html", {"form": form})

        with transaction.atomic():
            reset_code = PasswordResetCode.objects.select_for_update().filter(
                user=user,
                email=email,
                used_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).first()
            if not reset_code or not reset_code.is_active_for_verification:
                form.add_error(None, "That verification code is invalid or has expired. Request a new code.")
                return render(request, "accounts/password_reset_verify.html", {"form": form})

            if not reset_code.verify_code(code):
                reset_code.failed_attempts += 1
                update_fields = ["failed_attempts"]
                if reset_code.failed_attempts >= PasswordResetCode.MAX_FAILED_ATTEMPTS:
                    reset_code.used_at = timezone.now()
                    update_fields.append("used_at")
                    error_message = "Too many incorrect attempts. Request a new verification code."
                else:
                    error_message = "Incorrect verification code. Check your email and try again."
                reset_code.save(update_fields=update_fields)
                form.add_error("code", error_message)
                return render(request, "accounts/password_reset_verify.html", {"form": form})

            reset_code.verified_at = timezone.now()
            reset_code.save(update_fields=("verified_at",))

        request.session["password_reset_code_id"] = reset_code.pk
        request.session["password_reset_user_id"] = user.pk
        messages.success(request, "Verification code confirmed. Set your new password below.")
        return redirect("accounts:password_reset_confirm")
    return render(request, "accounts/password_reset_verify.html", {"form": form})


def password_reset_confirm(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    code_id = request.session.get("password_reset_code_id")
    user_id = request.session.get("password_reset_user_id")
    reset_code = (
        PasswordResetCode.objects.select_related("user")
        .filter(pk=code_id, user_id=user_id)
        .first()
        if code_id and user_id
        else None
    )
    if not reset_code or not reset_code.is_active_for_reset or not reset_code.user.is_active:
        _clear_password_reset_session(request)
        messages.error(request, "Your verification session has expired. Request a new verification code.")
        return redirect("accounts:password_reset")

    form = PasswordResetSetPasswordForm(reset_code.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            locked_code = (
                PasswordResetCode.objects.select_for_update()
                .select_related("user")
                .filter(pk=reset_code.pk, user_id=user_id)
                .first()
            )
            if not locked_code or not locked_code.is_active_for_reset or not locked_code.user.is_active:
                _clear_password_reset_session(request)
                messages.error(request, "Your verification session has expired. Request a new verification code.")
                return redirect("accounts:password_reset")
            form.save()
            now = timezone.now()
            locked_code.used_at = now
            locked_code.save(update_fields=("used_at",))
            PasswordResetCode.objects.filter(
                user=locked_code.user,
                used_at__isnull=True,
            ).exclude(pk=locked_code.pk).update(used_at=now)
        _clear_password_reset_session(request)
        messages.success(request, "Your password has been reset. Sign in with your new password.")
        return redirect("accounts:login")
    return render(request, "accounts/password_reset_confirm.html", {"form": form})