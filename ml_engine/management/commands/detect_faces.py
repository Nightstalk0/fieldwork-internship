import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ml_engine.face_detector import detect_faces


class Command(BaseCommand):
    help = "Run the trained face detector on an image and print JSON detections."

    def add_arguments(self, parser):
        parser.add_argument("image", type=Path, help="Path to an image file.")

    def handle(self, *args, **options):
        try:
            result = detect_faces(options["image"])
        except FileNotFoundError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(result, indent=2))
