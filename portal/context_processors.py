def portal_context(request):
    if not request.user.is_authenticated:
        return {
            "pending_dtr_count": 0,
            "pending_daily_report_count": 0,
            "pending_requirement_count": 0,
        }
    from .models import AttendanceLog, DailyReport, OJTRequirement, pending_attendance_approval_count

    if request.user.is_staff or request.user.role == "coordinator":
        return {
            "pending_dtr_count": pending_attendance_approval_count(AttendanceLog.objects.all()),
            "pending_daily_report_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
            "pending_requirement_count": OJTRequirement.objects.filter(status=OJTRequirement.Status.SUBMITTED).count(),
        }
    if request.user.role == "company":
        from .models import Application

        company_logs = AttendanceLog.objects.filter(
            intern__placement_type="platform",
            intern__applications__posting__company__user=request.user,
            intern__applications__status=Application.Status.ACCEPTED,
        ).distinct()
        return {
            "pending_dtr_count": pending_attendance_approval_count(company_logs),
            "pending_daily_report_count": 0,
            "pending_requirement_count": 0,
        }
    return {
        "pending_dtr_count": 0,
        "pending_daily_report_count": 0,
        "pending_requirement_count": 0,
    }