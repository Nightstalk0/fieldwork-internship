from django.contrib import admin
from django.urls import include, path

from portal import views as portal_views

handler403 = "django_ims.views.permission_denied"
handler404 = "django_ims.views.page_not_found"
handler500 = "django_ims.views.server_error"

urlpatterns = [
    path("admin/dashboard/", portal_views.coordinator_dashboard),
    path("admin/attendance/export.csv", portal_views.coordinator_attendance_export),
    path("admin/approvals/", portal_views.admin_approvals),
    path("admin/daily-reports/", portal_views.admin_daily_reports),
    path("admin/", admin.site.urls),
    path("captcha/", include("captcha.urls")),
    path("accounts/", include("accounts.urls")),
    path("", include("portal.urls")),
]