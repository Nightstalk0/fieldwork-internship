from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.core.validators import RegexValidator

from portal.models import CompanyProfile, InternProfile

from .models import User
from .validators import normalize_philippine_phone_number, validate_person_name

if settings.CAPTCHA_ENABLED:
    from captcha.fields import CaptchaField


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="Email or username",
        widget=forms.TextInput(attrs={"autocomplete": "username"}),
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        "autocomplete": "current-password",
        "aria-describedby": "login-password-feedback",
    }))
    if settings.CAPTCHA_ENABLED:
        captcha = CaptchaField()

    def clean_username(self):
        identifier = self.cleaned_data["username"].strip()
        if "@" not in identifier:
            return identifier

        usernames = list(
            User.objects.filter(email__iexact=identifier)
            .order_by("pk")
            .values_list("username", flat=True)[:2]
        )
        return usernames[0] if len(usernames) == 1 else identifier


class RegistrationForm(UserCreationForm):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    first_name = forms.CharField(max_length=150, validators=[validate_person_name])
    last_name = forms.CharField(max_length=150, validators=[validate_person_name])
    phone = forms.CharField(
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={
            "type": "tel",
            "inputmode": "tel",
            "pattern": "[0-9+(). -]{7,32}",
            "title": "Use a Philippine number with digits and standard separators.",
            "autocomplete": "tel",
        }),
    )
    student_id = forms.CharField(max_length=64)
    university = forms.CharField(max_length=180)
    course = forms.CharField(max_length=180)
    year_level = forms.TypedChoiceField(
        choices=InternProfile.YearLevel.choices,
        coerce=int,
        empty_value=None,
    )
    placement_type = forms.ChoiceField(choices=InternProfile.PlacementType.choices)
    company_name = forms.CharField(max_length=180)
    company_address = forms.CharField(
        max_length=255,
        label="Street, building, or office address",
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "street-address"}),
    )
    barangay = forms.CharField(max_length=100)
    city_municipality = forms.CharField(max_length=120, label="City or municipality")
    province = forms.CharField(max_length=120)
    postal_code = forms.CharField(
        max_length=4,
        required=False,
        widget=forms.TextInput(attrs={
            "inputmode": "numeric",
            "pattern": "[0-9]{4}",
            "maxlength": 4,
            "title": "Enter a 4-digit Philippine postal code.",
        }),
        validators=[RegexValidator(r"^[0-9]{4}$", "Enter a 4-digit Philippine postal code.")],
    )
    industry_type = forms.ChoiceField(choices=CompanyProfile.IndustryType.choices)
    contact_person_name = forms.CharField(max_length=180, validators=[validate_person_name])
    contact_person_position = forms.CharField(max_length=120)
    official_email = forms.EmailField(widget=forms.EmailInput(attrs={"autocomplete": "email"}))
    contact_number = forms.CharField(
        max_length=32,
        widget=forms.TextInput(attrs={
            "type": "tel",
            "inputmode": "tel",
            "pattern": "[0-9+(). -]{7,32}",
            "title": "Use a Philippine number with digits and standard separators.",
            "autocomplete": "tel",
        }),
    )
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
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        role = (
            self.data.get("role")
            or self.initial.get("role")
            or User.Role.INTERN
        )
        self.fields["username"].widget.attrs.setdefault("autocomplete", "username")
        self.fields["password1"].widget.attrs.update({
            "autocomplete": "new-password",
            "minlength": 8,
            "data-registration-password": "",
            "aria-describedby": "password1-feedback",
        })
        self.fields["password2"].widget.attrs.update({
            "autocomplete": "new-password",
            "data-password-confirmation": "",
            "aria-describedby": "password2-feedback",
        })
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
                "barangay",
                "city_municipality",
                "province",
                "postal_code",
                "industry_type",
                "contact_person_name",
                "contact_person_position",
                "official_email",
                "contact_number",
            ):
                self.fields[field_name].required = False

    def clean_phone(self):
        return normalize_philippine_phone_number(self.cleaned_data.get("phone", ""))

    def clean_contact_number(self):
        return normalize_philippine_phone_number(self.cleaned_data.get("contact_number", ""))

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


class CompanyRegistrationForm(RegistrationForm):
    def __init__(self, *args, **kwargs):
        bound_data = args[0] if args else kwargs.get("data")
        if bound_data is not None:
            bound_data = bound_data.copy()
            bound_data["role"] = User.Role.COMPANY
            if args:
                args = (bound_data, *args[1:])
            else:
                kwargs["data"] = bound_data
        initial = dict(kwargs.get("initial", {}))
        initial["role"] = User.Role.COMPANY
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)
        for field_name in (
            "first_name",
            "last_name",
            "email",
            "phone",
            "student_id",
            "university",
            "course",
            "year_level",
            "placement_type",
        ):
            self.fields.pop(field_name)

    def clean_role(self):
        return User.Role.COMPANY


class InternRegistrationForm(RegistrationForm):
    company_fields = (
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
    )

    def __init__(self, *args, **kwargs):
        bound_data = args[0] if args else kwargs.get("data")
        if bound_data is not None:
            bound_data = bound_data.copy()
            bound_data["role"] = User.Role.INTERN
            if args:
                args = (bound_data, *args[1:])
            else:
                kwargs["data"] = bound_data
        super().__init__(*args, **kwargs)
        self.fields["role"].choices = ((User.Role.INTERN, "Intern"),)
        for field_name in self.company_fields:
            self.fields.pop(field_name)

    def clean_role(self):
        return User.Role.INTERN