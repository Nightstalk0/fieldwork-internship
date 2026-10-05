import mimetypes
from pathlib import Path

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


@deconstructible
class FileSizeAndTypeValidator:
    allowed_types = {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    }
    signatures = (
        (b"%PDF-", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
        (b"PK\x03\x04", "application/zip"),
    )

    def __init__(self, max_bytes=5 * 1024 * 1024):
        self.max_bytes = max_bytes

    def __call__(self, uploaded_file):
        if uploaded_file.size > self.max_bytes:
            raise ValidationError(f"File size must not exceed {self.max_bytes // (1024 * 1024)} MB.")

        extension = Path(uploaded_file.name).suffix.lower()
        guessed_type, _ = mimetypes.guess_type(uploaded_file.name)
        readable = True
        try:
            header = uploaded_file.read(16)
            uploaded_file.seek(0)
        except (AttributeError, OSError):
            header = b""
            readable = False

        detected_type = None
        for signature, content_type in self.signatures:
            if header.startswith(signature):
                if content_type == "image/webp" and header[8:12] != b"WEBP":
                    continue
                detected_type = content_type
                break
        if detected_type == "image/gif":
            raise ValidationError("GIF uploads are not supported.")
        if detected_type and detected_type not in self.allowed_types:
            raise ValidationError("This file type is not supported.")
        if detected_type and guessed_type and detected_type != guessed_type:
            if not (detected_type == "application/zip" and extension == ".docx"):
                raise ValidationError("The file content does not match its extension.")
        if readable and not detected_type and extension in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".zip", ".docx"}:
            raise ValidationError("The file content could not be verified.")
        if not detected_type and guessed_type not in self.allowed_types:
            raise ValidationError("Upload a PDF, image, DOCX, ZIP, or plain text file.")