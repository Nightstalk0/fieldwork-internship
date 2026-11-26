from django.urls import path

from .views import AccountLoginView, company_login_view, company_register_view, logout_view, register

app_name = "accounts"
urlpatterns = [
    path("login/", AccountLoginView.as_view(), name="login"),
    path("company/login/", company_login_view, name="company_login"),
    path("company/register/", company_register_view, name="company_register"),
    path("register/", register, name="register"),
    path("logout/", logout_view, name="logout"),
]