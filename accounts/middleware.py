from django.http import HttpResponseForbidden, JsonResponse


class RoleBasedRouteGuardMiddleware:
    role_prefixes = {
        "/intern/": "intern",
        "/company/": "company",
        "/coordinator/": "coordinator",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        required_role = next(
            (role for prefix, role in self.role_prefixes.items() if request.path.startswith(prefix)),
            None,
        )
        if required_role and request.user.is_authenticated:
            if request.path_info == "/company/worksites/":
                has_access = request.user.is_admin or request.user.role == "company"
            elif required_role == "coordinator":
                has_access = request.user.is_admin
            else:
                has_access = (
                    request.user.is_staff
                    or request.user.is_superuser
                    or request.user.role == required_role
                )
            if not has_access:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse(
                        {"error": "Your account does not have access to this face-capture action."},
                        status=403,
                    )
                return HttpResponseForbidden("You do not have access to this area.")
        return self.get_response(request)