from django.urls import path

from .views import (
    AccountLoginView,
    company_login_view,
    company_register_view,
    logout_view,
    password_reset_confirm,
    password_reset_request,
    password_reset_verify,
    register,
)

app_name = "accounts"
urlpatterns = [
    path("login/", AccountLoginView.as_view(), name="login"),
    path("forgot-password/", password_reset_request, name="password_reset"),
    path("forgot-password/verify/", password_reset_verify, name="password_reset_verify"),
    path("forgot-password/reset/", password_reset_confirm, name="password_reset_confirm"),
    path("company/login/", company_login_view, name="company_login"),
    path("company/register/", company_register_view, name="company_register"),
    path("register/", register, name="register"),
    path("logout/", logout_view, name="logout"),
]