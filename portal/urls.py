from django.urls import path

from . import views

app_name = "portal"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("intern/dashboard/", views.intern_dashboard, name="intern_dashboard"),
    path("intern/profile/", views.profile, name="profile"),
    path("intern/postings/", views.postings, name="postings"),
    path("intern/postings/<int:posting_id>/apply/", views.apply_to_posting, name="apply"),
    path("intern/attendance/", views.attendance, name="attendance"),
    path("intern/attendance/export/", views.attendance_export, name="attendance_export"),
    path("intern/reports/", views.reports, name="reports"),
    path("intern/accreditation/", views.accreditation, name="accreditation"),
    path("supervisor/dashboard/", views.company_dashboard, name="company_dashboard"),
    path("supervisor/attendance/export.csv", views.company_attendance_export, name="company_attendance_export"),
    path("company/profile/", views.company_profile, name="company_profile"),
    path("company/postings/new/", views.posting_create, name="posting_create"),
    path("company/dtr/", views.company_dtr_queue, name="company_dtr_queue"),
    path("company/dtr/<int:log_id>/approve/", views.company_approve_dtr, name="company_approve_dtr"),
    path("company/scorecards/", views.company_scorecards, name="company_scorecards"),
    path("company/applications/", views.company_applications, name="company_applications"),
    path("company/applications/<int:application_id>/resume/<str:mode>/", views.company_application_resume, name="company_application_resume"),
    path("company/applicants/", views.applicant_screening, name="applicant_screening"),
    path("company/applications/<int:application_id>/<str:decision>/", views.application_decide, name="application_decide"),
    path("admin/dashboard/", views.coordinator_dashboard, name="coordinator_dashboard"),
    path("admin/attendance/export.csv", views.coordinator_attendance_export, name="coordinator_attendance_export"),
    path("coordinator/audit/", views.audit_logs, name="admin_audit"),
    path("coordinator/companies/verify/", views.company_verification, name="admin_verify"),
    path("coordinator/companies/<int:company_id>/verify/<str:decision>/", views.verify_company, name="admin_verify_company"),
    path("coordinator/dtr/", views.dtr_queue, name="dtr_queue"),
    path("coordinator/dtr/<int:log_id>/approve/", views.approve_dtr, name="approve_dtr"),
    path("coordinator/scorecards/", views.scorecards, name="scorecards"),
    path("coordinator/analytics/", views.analytics, name="analytics"),
]