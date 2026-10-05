from django.apps import apps
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import User


@receiver(post_save, sender=User)
def ensure_role_profile(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.role == User.Role.INTERN:
        apps.get_model("portal", "InternProfile").objects.get_or_create(user=instance)
    elif instance.role == User.Role.COMPANY:
        apps.get_model("portal", "CompanyProfile").objects.get_or_create(
            user=instance,
            defaults={"organization": instance.get_full_name() or instance.username},
        )