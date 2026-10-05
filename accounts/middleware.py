from django.http import HttpResponseForbidden


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
            if not (request.user.is_staff or request.user.role == required_role):
                return HttpResponseForbidden("You do not have access to this area.")
        return self.get_response(request)