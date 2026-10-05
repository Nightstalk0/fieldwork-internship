from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        INTERN = "intern", "Intern"
        COMPANY = "company", "Company"
        COORDINATOR = "coordinator", "Coordinator"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INTERN, db_index=True)
    phone = models.CharField(max_length=32, blank=True)

    def __str__(self):
        return self.get_full_name() or self.username