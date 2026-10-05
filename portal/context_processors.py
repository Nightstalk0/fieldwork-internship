from django.db.models import Q


def portal_context(request):
    if not request.user.is_authenticated:
        return {"pending_review_count": 0}
    from .models import AttendanceLog, WeeklyReport

    if request.user.is_staff or request.user.role == "coordinator":
        return {
            "pending_review_count": AttendanceLog.objects.filter(
                Q(clock_in__isnull=False, time_in_approved=False)
                | Q(clock_out__isnull=False, time_out_approved=False)
            ).count()
            + WeeklyReport.objects.filter(status=WeeklyReport.Status.SUBMITTED).count()
        }
    return {"pending_review_count": 0}