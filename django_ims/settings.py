import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEBUG = os.getenv("DEBUG", "true").lower() in {"1", "true", "yes"}
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("Set SECRET_KEY when DEBUG is disabled.")
    SECRET_KEY = "django-insecure-local-development-only"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
render_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "fieldwork-internship.onrender.com"
if render_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(render_hostname)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "axes",
    "captcha",
    "accounts.apps.AccountsConfig",
    "portal.apps.PortalConfig",
    "ml_engine.apps.MLEngineConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "axes.middleware.AxesMiddleware",
    "accounts.middleware.RoleBasedRouteGuardMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "django_ims.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "portal.context_processors.portal_context",
            ],
        },
    },
]

WSGI_APPLICATION = "django_ims.wsgi.application"
ASGI_APPLICATION = "django_ims.asgi.application"
database_url = os.getenv("DATABASE_URL")
if not DEBUG and not database_url:
    raise ImproperlyConfigured("Set DATABASE_URL when DEBUG is disabled.")
DATABASES = {
    "default": dj_database_url.config(
        default=database_url or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
        conn_health_checks=True,
    )
}

AUTH_USER_MODEL = "accounts.User"
AXES_ENABLED = False
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_BACKEND = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
if os.getenv("USE_S3", "false").lower() in {"1", "true", "yes"}:
    INSTALLED_APPS.append("storages")
    STORAGES = {
        "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},
        "staticfiles": {"BACKEND": STATICFILES_BACKEND},
    }
    AWS_STORAGE_BUCKET_NAME = os.environ["AWS_STORAGE_BUCKET_NAME"]
    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "us-east-1")
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = True
else:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": STATICFILES_BACKEND},
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "portal:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "internship@example.com")
CAPTCHA_ENABLED = os.getenv("CAPTCHA_ENABLED", "false").lower() in {"1", "true", "yes"}
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG

SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(dsn=SENTRY_DSN, integrations=[DjangoIntegration()], traces_sample_rate=0.1, send_default_pii=False)