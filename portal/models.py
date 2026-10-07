from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .validators import FileSizeAndTypeValidator


class InternProfile(models.Model):
    class PlacementType(models.TextChoices):
        PLATFORM = "platform", "Company using Fieldwork"
        EXTERNAL = "external", "External company (not using Fieldwork)"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="intern_profile")
    student_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    university = models.CharField(max_length=180, blank=True)
    course = models.CharField(max_length=180, blank=True)
    year_level = models.PositiveSmallIntegerField(null=True, blank=True)
    placement_type = models.CharField(
        max_length=12,
        choices=PlacementType.choices,
        default=PlacementType.PLATFORM,
    )
    external_host = models.CharField(max_length=180, blank=True)
    bio = models.TextField(blank=True)
    resume = models.FileField(upload_to="resumes/%Y/%m/", blank=True, validators=[FileSizeAndTypeValidator()])
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.student_id == "":
            self.student_id = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Intern profile: {self.user}"


class OJTRequirement(models.Model):
    class Category(models.TextChoices):
        SCHOOL = "school", "School and academic"
        LEGAL = "legal", "Legal and health"
        APPLICATION = "application", "Application"
        ROLE_SPECIFIC = "role_specific", "Company or role-specific"

    class Status(models.TextChoices):
        NOT_SUBMITTED = "not_submitted", "Not submitted"
        SUBMITTED = "submitted", "Awaiting review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Changes requested"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="ojt_requirements")
    category = models.CharField(max_length=20, choices=Category.choices)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    is_required = models.BooleanField(default=False)
    document = models.FileField(
        upload_to="ojt_requirements/%Y/%m/",
        blank=True,
        validators=[FileSizeAndTypeValidator()],
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_SUBMITTED)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ojt_requirement_reviews",
    )
    review_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("category", "title")
        constraints = [
            models.UniqueConstraint(fields=("intern", "title"), name="uniq_ojt_requirement_intern_title")
        ]

    def __str__(self):
        return f"{self.title} — {self.intern}"


OJT_REQUIREMENT_DEFAULTS = (
    (
        OJTRequirement.Category.SCHOOL,
        "Endorsement / recommendation letter",
        "A letter from your OJT coordinator or department head endorsing you for training.",
        True,
    ),
    (
        OJTRequirement.Category.SCHOOL,
        "School–HTE Memorandum of Agreement (MOA)",
        "The agreement between your school and host training establishment.",
        True,
    ),
    (
        OJTRequirement.Category.SCHOOL,
        "Certificate of Enrollment / Registration (COR)",
        "Proof of enrollment in the designated OJT or practicum subject.",
        True,
    ),
    (
        OJTRequirement.Category.SCHOOL,
        "Transcript of Records / evaluation of grades",
        "Your school record showing that you meet the program prerequisites.",
        True,
    ),
    (
        OJTRequirement.Category.SCHOOL,
        "Student accident insurance",
        "Proof of active student accident insurance for your training period.",
        True,
    ),
    (
        OJTRequirement.Category.LEGAL,
        "Notarized parent / guardian consent and waiver",
        "The notarized consent and waiver required by your school or host.",
        True,
    ),
    (
        OJTRequirement.Category.LEGAL,
        "Medical certificate",
        "A certificate from a licensed clinic or physician confirming fitness for training.",
        True,
    ),
    (
        OJTRequirement.Category.LEGAL,
        "Barangay, police, or NBI clearance",
        "Submit a clearance only if requested by your school or host.",
        False,
    ),
    (
        OJTRequirement.Category.LEGAL,
        "Government-issued ID / tax or social-security document",
        "Submit only the documents requested by your host; do not upload unrequested numbers.",
        False,
    ),
    (
        OJTRequirement.Category.APPLICATION,
        "Resume / CV",
        "Your current resume or curriculum vitae.",
        False,
    ),
    (
        OJTRequirement.Category.APPLICATION,
        "Cover letter / letter of intent",
        "A letter describing your interest in the placement.",
        False,
    ),
    (
        OJTRequirement.Category.APPLICATION,
        "Portfolio",
        "A portfolio of relevant work, if requested for your field.",
        False,
    ),
    (
        OJTRequirement.Category.ROLE_SPECIFIC,
        "Drug test / laboratory exams",
        "Required only for applicable roles or when requested by your host.",
        False,
    ),
)


def ensure_default_ojt_requirements(intern):
    for category, title, description, is_required in OJT_REQUIREMENT_DEFAULTS:
        OJTRequirement.objects.get_or_create(
            intern=intern,
            title=title,
            defaults={
                "category": category,
                "description": description,
                "is_required": is_required,
            },
        )


class CompanyProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="company_profile")
    organization = models.CharField(max_length=180)
    website = models.URLField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    verified = models.BooleanField(default=False)

    def __str__(self):
        return self.organization


class Posting(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        CLOSED = "closed", "Closed"

    company = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="postings")
    title = models.CharField(max_length=180)
    description = models.TextField()
    location = models.CharField(max_length=180)
    remote = models.BooleanField(default=False)
    openings = models.PositiveSmallIntegerField(default=1)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return self.title


class Application(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        REVIEWING = "reviewing", "Reviewing"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="applications")
    posting = models.ForeignKey(Posting, on_delete=models.CASCADE, related_name="applications")
    cover_letter = models.TextField(blank=True)
    resume = models.FileField(upload_to="applications/%Y/%m/", validators=[FileSizeAndTypeValidator()], blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("intern", "posting"), name="uniq_application_intern_posting")]
        ordering = ("-submitted_at",)


class AttendanceLog(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        LATE = "late", "Late"
        EXCUSED = "excused", "Excused"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="attendance_logs")
    work_date = models.DateField(default=timezone.localdate)
    clock_in = models.DateTimeField(null=True, blank=True)
    clock_out = models.DateTimeField(null=True, blank=True)
    clock_in_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_in_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_out_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    clock_out_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PRESENT)
    time_in_approved = models.BooleanField(default=False)
    time_out_approved = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("intern", "work_date"), name="uniq_attendance_intern_day")]
        ordering = ("-work_date",)

    @property
    def hours_worked(self):
        if not self.clock_in or not self.clock_out:
            return 0
        return round(max((self.clock_out - self.clock_in).total_seconds(), 0) / 3600, 2)

    @property
    def approved(self):
        return self.time_in_approved and self.time_out_approved


class DailyReport(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Awaiting admin review"
        REVIEWED = "reviewed", "Reviewed"
        CHANGES_REQUESTED = "changes_requested", "Changes requested"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="daily_reports")
    work_date = models.DateField(default=timezone.localdate)
    accomplishments = models.TextField()
    challenges = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    supervisor_feedback = models.TextField(blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_daily_reports",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("intern", "work_date"), name="uniq_daily_report_intern_day")
        ]
        ordering = ("-work_date",)


class WeeklyReport(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        REVIEWED = "reviewed", "Reviewed"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="weekly_reports")
    week_start = models.DateField()
    accomplishments = models.TextField()
    challenges = models.TextField(blank=True)
    next_week_plan = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)
    submitted_at = models.DateTimeField(default=timezone.now)
    supervisor_feedback = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("intern", "week_start"), name="uniq_report_intern_week")]
        ordering = ("-week_start",)


class Scorecard(models.Model):
    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="scorecards")
    company = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, null=True, blank=True, related_name="scorecards")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_scorecards")
    week_start = models.DateField()
    technical = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    communication = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    initiative = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    reliability = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    feedback = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("intern", "week_start"),
                condition=Q(company__isnull=True),
                name="uniq_scorecard_intern_week",
            ),
            models.UniqueConstraint(
                fields=("company", "intern", "week_start"),
                condition=Q(company__isnull=False),
                name="uniq_company_scorecard_intern_week",
            ),
        ]
        ordering = ("-week_start",)

    @property
    def average(self):
        return round((self.technical + self.communication + self.initiative + self.reliability) / 4, 2)


class RiskAssessment(models.Model):
    class Level(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    intern = models.ForeignKey(InternProfile, on_delete=models.CASCADE, related_name="risk_assessments")
    assessment_date = models.DateField(default=timezone.localdate)
    risk_score = models.PositiveSmallIntegerField(validators=[MaxValueValidator(100)])
    level = models.CharField(max_length=8, choices=Level.choices)
    indicators = models.JSONField(default=list, blank=True)
    model_version = models.CharField(max_length=32, default="rules-v1")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("intern", "assessment_date"), name="uniq_risk_intern_day")]
        ordering = ("-assessment_date",)


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    object_type = models.CharField(max_length=120)
    object_id = models.CharField(max_length=80, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.action}: {self.object_type} {self.object_id}" 