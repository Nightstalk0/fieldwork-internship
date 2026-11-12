from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from portal.models import CompanyProfile, InternProfile

from .models import User

if settings.CAPTCHA_ENABLED:
    from captcha.fields import CaptchaField


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autocomplete": "username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    if settings.CAPTCHA_ENABLED:
        captcha = CaptchaField()


class RegistrationForm(UserCreationForm):
    email = forms.EmailField()
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    student_id = forms.CharField(max_length=64)
    university = forms.CharField(max_length=180)
    course = forms.CharField(max_length=180)
    year_level = forms.IntegerField(min_value=1)
    placement_type = forms.ChoiceField(choices=InternProfile.PlacementType.choices)
    bio = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    company_name = forms.CharField(max_length=180)
    company_address = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))
    industry_type = forms.ChoiceField(choices=CompanyProfile.IndustryType.choices)
    contact_person_name = forms.CharField(max_length=180)
    contact_person_position = forms.CharField(max_length=120)
    official_email = forms.EmailField()
    contact_number = forms.CharField(max_length=32)
    company_website = forms.URLField(required=False)
    moa_reference_number = forms.CharField(max_length=120, required=False)
    role = forms.ChoiceField(
        choices=((User.Role.INTERN, "Intern"), (User.Role.COMPANY, "Company")),
        widget=forms.HiddenInput(),
        initial=User.Role.INTERN,
        required=False,
    )
    if settings.CAPTCHA_ENABLED:
        captcha = CaptchaField()

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "student_id",
            "university",
            "course",
            "year_level",
            "placement_type",
            "bio",
            "company_name",
            "company_address",
            "industry_type",
            "contact_person_name",
            "contact_person_position",
            "official_email",
            "contact_number",
            "company_website",
            "moa_reference_number",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        role = (
            self.data.get("role")
            or self.initial.get("role")
            or User.Role.INTERN
        )
        if role == User.Role.COMPANY:
            for field_name in (
                "student_id",
                "university",
                "course",
                "year_level",
                "placement_type",
                "first_name",
                "last_name",
                "email",
                "phone",
            ):
                self.fields[field_name].required = False
        else:
            for field_name in (
                "company_name",
                "company_address",
                "industry_type",
                "contact_person_name",
                "contact_person_position",
                "official_email",
                "contact_number",
            ):
                self.fields[field_name].required = False

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            return email
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def clean_student_id(self):
        student_id = self.cleaned_data.get("student_id", "").strip()
        if not student_id:
            return student_id
        if InternProfile.objects.filter(student_id=student_id).exists():
            raise forms.ValidationError("An intern with this student ID is already registered.")
        return student_id

    def clean_official_email(self):
        email = self.cleaned_data.get("official_email", "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email address already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("role") == User.Role.COMPANY:
            email = cleaned_data.get("official_email", "")
            cleaned_data["email"] = email
            contact_name = cleaned_data.get("contact_person_name", "").strip()
            name_parts = contact_name.split(maxsplit=1)
            cleaned_data["first_name"] = name_parts[0] if name_parts else ""
            cleaned_data["last_name"] = name_parts[1] if len(name_parts) > 1 else ""
            cleaned_data["phone"] = cleaned_data.get("contact_number", "").strip()
        return cleaned_data

    def clean_role(self):
        return self.cleaned_data.get("role") or User.Role.INTERN