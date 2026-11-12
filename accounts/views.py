from django.contrib.auth import logout
from django.contrib.auth.views import LoginView
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from portal.models import CompanyProfile, InternProfile

from .forms import LoginForm, RegistrationForm
from .models import User


class AccountLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


@transaction.atomic
def register(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        if user.role == User.Role.INTERN:
            profile, _ = InternProfile.objects.get_or_create(user=user)
            for field_name in (
                "student_id",
                "university",
                "course",
                "year_level",
                "placement_type",
            ):
                setattr(profile, field_name, form.cleaned_data[field_name])
            profile.save()
        elif user.role == User.Role.COMPANY:
            profile, _ = CompanyProfile.objects.get_or_create(user=user)
            for field_name in (
                "company_name",
                "company_address",
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
        messages.success(request, "Account created. Sign in to continue.")
        return redirect("accounts:login")
    return render(request, "accounts/register.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("accounts:login")