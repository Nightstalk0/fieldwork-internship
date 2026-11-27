from django import forms
from django.conf import settings
from django.utils import timezone

from accounts.validators import normalize_philippine_phone_number, validate_person_name

from .models import Application, CompanyProfile, DailyReport, InternProfile, Posting, Scorecard, WorksiteLocation


class CompanyProfileForm(forms.ModelForm):
    company_address = forms.CharField(
        max_length=255,
        label="Street, building, or office address",
        widget=forms.Textarea(attrs={"rows": 2, "autocomplete": "street-address"}),
    )
    contact_person_name = forms.CharField(
        max_length=180,
        validators=[validate_person_name],
    )
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

    class Meta:
        model = CompanyProfile
        fields = (
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
        labels = {
            "city_municipality": "City or municipality",
        }
        widgets = {
            "postal_code": forms.TextInput(attrs={
                "inputmode": "numeric",
                "pattern": "[0-9]{4}",
                "maxlength": 4,
                "title": "Enter a 4-digit Philippine postal code.",
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in (
            "company_name",
            "company_address",
            "barangay",
            "city_municipality",
            "province",
            "industry_type",
            "contact_person_name",
            "contact_person_position",
            "official_email",
        ):
            self.fields[field_name].required = True

    def clean_contact_number(self):
        return normalize_philippine_phone_number(self.cleaned_data["contact_number"])


class WorksiteLocationForm(forms.ModelForm):
    confirm_location = forms.BooleanField(
        required=True,
        label="I confirm the address and map pin mark the official worksite.",
    )
    radius_meters = forms.IntegerField(
        min_value=10,
        max_value=5000,
        widget=forms.NumberInput(attrs={"min": 10, "max": 5000, "step": 1}),
    )

    class Meta:
        model = WorksiteLocation
        fields = (
            "name",
            "address",
            "latitude",
            "longitude",
            "radius_meters",
            "reported_accuracy_meters",
        )
        widgets = {
            "address": forms.TextInput(attrs={
                "autocomplete": "off",
                "placeholder": "Search company/worksite address...",
            }),
            "latitude": forms.NumberInput(attrs={"step": "0.000001", "readonly": True}),
            "longitude": forms.NumberInput(attrs={"step": "0.000001", "readonly": True}),
            "reported_accuracy_meters": forms.HiddenInput(),
        }
        help_texts = {
            "latitude": "Select an address or drag the map pin to set the coordinates.",
            "longitude": "Select an address or drag the map pin to set the coordinates.",
            "radius_meters": "Attendance is allowed within this distance of the saved coordinates.",
        }

    def __init__(self, *args, allow_company_selection=False, **kwargs):
        super().__init__(*args, **kwargs)
        if allow_company_selection:
            self.fields["company"] = forms.ModelChoiceField(
                queryset=CompanyProfile.objects.filter(user__is_active=True).order_by("company_name"),
                label="Company",
            )
            self.order_fields(("company", *(key for key in self.fields if key != "company")))

    def clean_latitude(self):
        latitude = self.cleaned_data["latitude"]
        if not -90 <= latitude <= 90:
            raise forms.ValidationError("Latitude must be between -90 and 90 degrees.")
        return latitude

    def clean_longitude(self):
        longitude = self.cleaned_data["longitude"]
        if not -180 <= longitude <= 180:
            raise forms.ValidationError("Longitude must be between -180 and 180 degrees.")
        return longitude

    def clean_reported_accuracy_meters(self):
        accuracy = self.cleaned_data.get("reported_accuracy_meters")
        if accuracy is not None and accuracy <= 0:
            raise forms.ValidationError("GPS accuracy must be greater than zero. Capture the location again.")
        if accuracy is not None and accuracy > settings.GPS_MAX_ACCURACY_METERS:
            raise forms.ValidationError(
                f"GPS accuracy must be {settings.GPS_MAX_ACCURACY_METERS} meters or better. "
                "Capture the location again."
            )
        return accuracy


class ExternalWorksiteLocationForm(WorksiteLocationForm):
    class Meta(WorksiteLocationForm.Meta):
        fields = ("intern", *WorksiteLocationForm.Meta.fields)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["intern"].queryset = InternProfile.objects.filter(
            placement_type=InternProfile.PlacementType.EXTERNAL,
        ).select_related("user").order_by("user__last_name", "user__first_name", "user__username")


class InternWorksiteRequestForm(forms.ModelForm):
    application = forms.ModelChoiceField(
        queryset=Application.objects.none(),
        required=False,
        label="Accepted internship assignment",
    )
    confirm_location = forms.BooleanField(
        required=True,
        label="I confirm the address and map pin show the correct OJT worksite.",
    )

    class Meta:
        model = WorksiteLocation
        fields = (
            "application",
            "name",
            "address",
            "latitude",
            "longitude",
            "reported_accuracy_meters",
        )
        labels = {
            "name": "Worksite name",
            "address": "Worksite address",
        }
        widgets = {
            "address": forms.TextInput(attrs={
                "autocomplete": "off",
                "placeholder": "Search company, building, street, barangay, city or province in the Philippines",
            }),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "reported_accuracy_meters": forms.HiddenInput(),
        }

    def __init__(self, *args, intern=None, **kwargs):
        super().__init__(*args, **kwargs)
        if intern is None:
            self.fields.pop("application")
            return

        accepted_applications = Application.objects.filter(
            intern=intern,
            status=Application.Status.ACCEPTED,
        ).select_related("posting__company").order_by("-updated_at", "-pk")
        if accepted_applications.exists():
            self.fields["application"].queryset = accepted_applications
            self.fields["application"].required = True
            self.fields["application"].label_from_instance = (
                lambda application: (
                    f"{application.posting.company.company_name} — {application.posting.title}"
                )
            )
        else:
            self.fields.pop("application")

    def clean_latitude(self):
        latitude = self.cleaned_data["latitude"]
        if not -90 <= latitude <= 90:
            raise forms.ValidationError("Latitude must be between -90 and 90 degrees.")
        return latitude

    def clean_longitude(self):
        longitude = self.cleaned_data["longitude"]
        if not -180 <= longitude <= 180:
            raise forms.ValidationError("Longitude must be between -180 and 180 degrees.")
        return longitude

    def clean_reported_accuracy_meters(self):
        accuracy = self.cleaned_data["reported_accuracy_meters"]
        if accuracy is not None and accuracy < 1:
            raise forms.ValidationError("Location accuracy must be greater than zero.")
        if accuracy is not None and accuracy > settings.GPS_MAX_ACCURACY_METERS:
            raise forms.ValidationError(
                f"GPS accuracy must be {settings.GPS_MAX_ACCURACY_METERS} meters or better. "
                "Capture the location again."
            )
        return accuracy


class InternProfileForm(forms.ModelForm):
    year_level = forms.TypedChoiceField(
        required=False,
        choices=InternProfile.YearLevel.choices,
        coerce=int,
        empty_value=None,
    )

    class Meta:
        model = InternProfile
        fields = (
            "student_id",
            "course",
            "year_level",
            "placement_type",
            "external_host",
            "resume",
        )
        help_texts = {
            "year_level": "Choose your current year level.",
            "placement_type": "Choose whether your host company has a Fieldwork account.",
            "external_host": "Add your host's name when known; it can be added or updated later.",
            "resume": "Upload a PDF, DOCX, PNG, JPG, or WEBP resume (maximum 5 MB).",
        }
        widgets = {
            "resume": forms.FileInput(attrs={"accept": ".pdf,.docx,.png,.jpg,.jpeg,.webp"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and Application.objects.filter(
            intern=self.instance,
            status=Application.Status.ACCEPTED,
        ).exists():
            self.fields["placement_type"].disabled = True

    def clean(self):
        cleaned_data = super().clean()
        placement_type = cleaned_data.get("placement_type")
        external_host = (cleaned_data.get("external_host") or "").strip()
        cleaned_data["external_host"] = (
            external_host if placement_type == InternProfile.PlacementType.EXTERNAL else ""
        )
        return cleaned_data


class PostingForm(forms.ModelForm):
    openings = forms.IntegerField(
        min_value=1,
        max_value=32767,
        widget=forms.NumberInput(attrs={"min": 1, "max": 32767, "step": 1}),
    )

    class Meta:
        model = Posting
        fields = ("title", "description", "location", "remote", "openings", "start_date", "end_date", "status")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "description": "Describe the responsibilities, required skills, and placement expectations.",
            "openings": "Enter the number of interns this placement can accept.",
            "start_date": "The expected placement start date.",
            "end_date": "The expected placement end date, if known.",
        }


class DailyReportForm(forms.ModelForm):
    class Meta:
        model = DailyReport
        fields = ("accomplishments", "challenges")
        widgets = {field: forms.Textarea(attrs={"rows": 4}) for field in fields}
        help_texts = {
            "accomplishments": "Summarize the tasks completed and skills practiced today.",
            "challenges": "Note any blockers or support needed; leave blank when there were none.",
        }


class DailyReportReviewForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(("review", "Mark reviewed"), ("request_changes", "Request changes")),
        widget=forms.HiddenInput(),
    )
    supervisor_feedback = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 1000}),
    )

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("decision") == "request_changes"
            and not cleaned_data.get("supervisor_feedback", "").strip()
        ):
            self.add_error("supervisor_feedback", "Add feedback describing the requested changes.")
        return cleaned_data


class ApplicationSubmissionForm(forms.ModelForm):
    cover_letter = forms.CharField(
        required=False,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": 3000}),
    )

    class Meta:
        model = Application
        fields = ("cover_letter", "resume")
        widgets = {
            "resume": forms.FileInput(attrs={
                "accept": ".pdf,.docx,.png,.jpg,.jpeg,.webp",
            }),
        }


class ScorecardSelectionForm(forms.Form):
    intern_id = forms.ModelChoiceField(
        queryset=InternProfile.objects.none(),
        empty_label="Choose an intern",
    )
    week_start = forms.DateField(
        label="Week begins",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    def __init__(self, *args, interns, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["intern_id"].queryset = interns

    def clean_week_start(self):
        week_start = self.cleaned_data["week_start"]
        if week_start > timezone.localdate():
            raise forms.ValidationError("The week start date cannot be in the future.")
        return week_start


class ScorecardForm(forms.ModelForm):
    class Meta:
        model = Scorecard
        fields = ("technical", "communication", "initiative", "reliability", "feedback")
        widgets = {
            "technical": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "communication": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "initiative": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "reliability": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "feedback": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "technical": "Rate from 1 (needs support) to 5 (exceptional).",
            "communication": "Rate from 1 (needs support) to 5 (exceptional).",
            "initiative": "Rate from 1 (needs support) to 5 (exceptional).",
            "reliability": "Rate from 1 (needs support) to 5 (exceptional).",
        }