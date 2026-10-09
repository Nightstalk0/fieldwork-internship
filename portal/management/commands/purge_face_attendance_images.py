from django.core.management.base import BaseCommand
from django.utils import timezone

from portal.models import AttendanceFaceCapture


class Command(BaseCommand):
    help = "Delete encrypted attendance face images that have passed their 30-day retention period."

    def handle(self, *args, **options):
        expired_count = AttendanceFaceCapture.objects.filter(
            encrypted_image__isnull=False,
            image_expires_at__lte=timezone.now(),
        ).update(encrypted_image=None)
        self.stdout.write(f"Deleted {expired_count} expired attendance face image(s).")
