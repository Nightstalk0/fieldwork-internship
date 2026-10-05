from django.urls import path

from .views import AccountLoginView, logout_view, register

app_name = "accounts"
urlpatterns = [
    path("login/", AccountLoginView.as_view(), name="login"),
    path("register/", register, name="register"),
    path("logout/", logout_view, name="logout"),
]