from django.db.models import Q


def portal_context(request):
    if not request.user.is_authenticated:
        return {
            "pending_dtr_count": 0,
            "pending_daily_report_count": 0,
            "pending_requirement_count": 0,
        }
    from .models import AttendanceLog, DailyReport, OJTRequirement

    if request.user.is_staff or request.user.role == "coordinator":
        return {
            "pending_dtr_count": AttendanceLog.objects.filter(
                Q(clock_in__isnull=False, time_in_approved=False)
                | Q(clock_out__isnull=False, time_out_approved=False)
            ).count(),
            "pending_daily_report_count": DailyReport.objects.filter(status=DailyReport.Status.SUBMITTED).count(),
            "pending_requirement_count": OJTRequirement.objects.filter(status=OJTRequirement.Status.SUBMITTED).count(),
        }
    return {
        "pending_dtr_count": 0,
        "pending_daily_report_count": 0,
        "pending_requirement_count": 0,
    }