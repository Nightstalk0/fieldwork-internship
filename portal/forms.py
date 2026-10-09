from django import forms

from .models import CompanyProfile, CompanyRequirement, DailyReport, InternProfile, OJTRequirement, Posting, Scorecard, WeeklyReport


class CompanyProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyProfile
        fields = ("organization", "website", "address")


class CompanyRequirementForm(forms.ModelForm):
    class Meta:
        model = CompanyRequirement
        fields = ("title", "description")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "title": "Interns accepted into one of your placements will be asked to submit this document.",
            "description": "Explain what the document should contain.",
        }


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


class OJTRequirementUploadForm(forms.ModelForm):
    class Meta:
        model = OJTRequirement
        fields = ("document",)


class PostingForm(forms.ModelForm):
    class Meta:
        model = Posting
        fields = ("title", "description", "location", "remote", "openings", "start_date", "end_date", "status")
        widgets = {"description": forms.Textarea(attrs={"rows": 5}), "openings": forms.NumberInput(attrs={"min": 1}), "start_date": forms.DateInput(attrs={"type": "date"}), "end_date": forms.DateInput(attrs={"type": "date"})}
        help_texts = {
            "description": "Describe the responsibilities, required skills, and any OJT-hour expectations.",
            "openings": "Enter the number of interns this placement can accept.",
            "start_date": "The expected placement start date.",
            "end_date": "The expected placement end date, if known.",
        }


class WeeklyReportForm(forms.ModelForm):
    class Meta:
        model = WeeklyReport
        fields = ("accomplishments", "challenges", "next_week_plan")
        widgets = {field: forms.Textarea(attrs={"rows": 4}) for field in fields}
        help_texts = {
            "accomplishments": "Summarize work completed and skills practiced during this reporting week.",
            "challenges": "Note blockers or support needed; leave blank when there were none.",
            "next_week_plan": "List your planned tasks and learning goals for next week.",
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