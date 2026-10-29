from django import forms

from .models import CompanyProfile, DailyReport, InternProfile, Posting, Scorecard, WorksiteLocation


class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ("organization", "website", "address")


class WorksiteLocationForm(forms.ModelForm):
    class Meta:
        model = WorksiteLocation
        fields = ("name", "address", "latitude", "longitude", "radius_meters")
        widgets = {
            "latitude": forms.NumberInput(attrs={"step": "0.000001", "readonly": True}),
            "longitude": forms.NumberInput(attrs={"step": "0.000001", "readonly": True}),
            "radius_meters": forms.NumberInput(attrs={"min": 10, "max": 5000}),
        }
        help_texts = {
            "latitude": "Use the location button to capture the worksite coordinates.",
            "longitude": "Use the location button to capture the worksite coordinates.",
            "radius_meters": "Attendance is allowed within this distance of the saved coordinates.",
        }

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


class ExternalWorksiteLocationForm(WorksiteLocationForm):
    class Meta(WorksiteLocationForm.Meta):
        fields = ("intern", *WorksiteLocationForm.Meta.fields)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["intern"].queryset = InternProfile.objects.filter(
            placement_type=InternProfile.PlacementType.EXTERNAL,
        ).select_related("user").order_by("user__last_name", "user__first_name", "user__username")


class InternProfileForm(forms.ModelForm):
    class Meta:
        model = InternProfile
        fields = (
            "student_id",
            "university",
            "course",
            "year_level",
            "placement_type",
            "external_host",
            "bio",
            "resume",
        )
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}
        help_texts = {
            "year_level": "Enter your current year level as a number.",
            "placement_type": "Choose whether your host company has a Fieldwork account.",
            "external_host": "Add your host's name when known; it can be added or updated later.",
            "bio": "Summarize your skills, interests, and the type of placement you are seeking.",
            "resume": "Upload a PDF or DOCX resume within the listed file-size limit.",
        }

    def clean(self):
        cleaned_data = super().clean()
        placement_type = cleaned_data.get("placement_type")
        external_host = (cleaned_data.get("external_host") or "").strip()
        cleaned_data["external_host"] = (
            external_host if placement_type == InternProfile.PlacementType.EXTERNAL else ""
        )
        return cleaned_data


class PostingForm(forms.ModelForm):
    class Meta:
        model = Posting
        fields = ("title", "description", "location", "remote", "openings", "start_date", "end_date", "status")
        widgets = {"description": forms.Textarea(attrs={"rows": 5}), "openings": forms.NumberInput(attrs={"min": 1}), "start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}
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