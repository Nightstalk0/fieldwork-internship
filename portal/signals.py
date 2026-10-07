from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import InternProfile, ensure_default_ojt_requirements


@receiver(post_save, sender=InternProfile)
def create_default_ojt_requirements_for_intern(sender, instance, created, **kwargs):
    if created:
        ensure_default_ojt_requirements(instance)
