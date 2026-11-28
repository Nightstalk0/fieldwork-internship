from datetime import timedelta
import hashlib
import hmac
import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .validators import validate_person_name, validate_philippine_phone_number


class User(AbstractUser):
    class Role(models.TextChoices):
        INTERN = "intern", "Intern"
        COMPANY = "company", "Company"
        COORDINATOR = "coordinator", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INTERN, db_index=True)
    first_name = models.CharField(
        _("first name"),
        max_length=150,
        blank=True,
        validators=[validate_person_name],
    )
    last_name = models.CharField(
        _("last name"),
        max_length=150,
        blank=True,
        validators=[validate_person_name],
    )
    phone = models.CharField(
        max_length=32,
        blank=True,
        validators=[validate_philippine_phone_number],
    )

    @property
    def is_admin(self):
        return self.is_staff or self.is_superuser or self.role == self.Role.COORDINATOR

    def __str__(self):
        return self.get_full_name() or self.username


class PasswordResetCode(models.Model):
    """One-time 6-digit email verification code for password resets."""

    CODE_TTL_MINUTES = 10
    RESEND_COOLDOWN_SECONDS = 60
    MAX_FAILED_ATTEMPTS = 5

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="password_reset_codes",
    )
    email = models.EmailField(db_index=True)
    salt = models.CharField(max_length=32)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-created_at", "-pk")

    @staticmethod
    def hash_code(salt, raw_code):
        payload = f"{salt}:{raw_code}".encode("utf-8")
        return hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

    def verify_code(self, raw_code):
        return hmac.compare_digest(self.code_hash, self.hash_code(self.salt, raw_code))

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_active_for_verification(self):
        return (
            self.used_at is None
            and self.failed_attempts < self.MAX_FAILED_ATTEMPTS
            and not self.is_expired
        )

    @property
    def is_active_for_reset(self):
        return (
            self.used_at is None
            and self.verified_at is not None
            and self.failed_attempts < self.MAX_FAILED_ATTEMPTS
            and not self.is_expired
        )

    @classmethod
    def issue_for_user(cls, user):
        now = timezone.now()
        cls.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
        raw_code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        instance = cls.objects.create(
            user=user,
            email=(user.email or "").strip().lower(),
            salt=salt,
            code_hash=cls.hash_code(salt, raw_code),
            expires_at=now + timedelta(minutes=cls.CODE_TTL_MINUTES),
        )
        return instance, raw_code