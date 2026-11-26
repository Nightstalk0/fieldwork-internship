from django.contrib.auth.models import AbstractUser
from django.db import models
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