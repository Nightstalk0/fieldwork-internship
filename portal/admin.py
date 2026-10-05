from django.contrib import admin

from .models import Application, AttendanceLog, AuditLog, CompanyProfile, InternProfile, Posting, RiskAssessment, Scorecard, WeeklyReport


@admin.register(InternProfile)
class InternProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "student_id", "university", "course")
    search_fields = ("user__username", "user__email", "student_id", "university")


@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    list_display = ("organization", "user", "verified")
    list_filter = ("verified",)


@admin.register(Posting)
class PostingAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "status", "created_at")
    list_filter = ("status", "remote")
    search_fields = ("title", "company__organization")


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("intern", "posting", "status", "submitted_at")
    list_filter = ("status",)
    search_fields = ("intern__user__username", "posting__title")


@admin.register(AttendanceLog)
class AttendanceLogAdmin(admin.ModelAdmin):
    list_display = ("intern", "work_date", "clock_in", "clock_out", "time_in_approved", "time_out_approved", "status")
    list_filter = ("time_in_approved", "time_out_approved", "status", "work_date")


@admin.register(WeeklyReport)
class WeeklyReportAdmin(admin.ModelAdmin):
    list_display = ("intern", "week_start", "status", "submitted_at")
    list_filter = ("status", "week_start")


@admin.register(Scorecard)
class ScorecardAdmin(admin.ModelAdmin):
    list_display = ("intern", "reviewer", "week_start", "average")


@admin.register(RiskAssessment)
class RiskAssessmentAdmin(admin.ModelAdmin):
    list_display = ("intern", "assessment_date", "level", "risk_score")
    list_filter = ("level", "assessment_date")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "object_type", "object_id")
    readonly_fields = ("actor", "action", "object_type", "object_id", "details", "created_at")
    date_hierarchy = "created_at"