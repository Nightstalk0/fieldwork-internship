import base64
import binascii
from io import BytesIO

from PIL import Image, UnidentifiedImageError


MAX_CAPTURE_BYTES = 1_500_000
MAX_CAPTURE_DIMENSION = 1920


class FaceCaptureError(ValueError):
    pass


def decode_camera_image(data_url: str) -> Image.Image:
    try:
        media_type, encoded = data_url.split(",", 1)
        if media_type != "data:image/jpeg;base64":
            raise FaceCaptureError("Capture must be a JPEG image.")
        if len(encoded) > MAX_CAPTURE_BYTES * 4 // 3 + 4:
            raise FaceCaptureError("Camera image is too large.")
        image_bytes = base64.b64decode(encoded, validate=True)
        if not image_bytes or len(image_bytes) > MAX_CAPTURE_BYTES:
            raise FaceCaptureError("Camera image is empty or too large.")

        with Image.open(BytesIO(image_bytes)) as image:
            if image.format != "JPEG":
                raise FaceCaptureError("Capture must be a JPEG image.")
            if image.width > MAX_CAPTURE_DIMENSION or image.height > MAX_CAPTURE_DIMENSION:
                raise FaceCaptureError("Camera image dimensions are too large.")
            image.load()
            return image.convert("RGB")
    except (ValueError, binascii.Error, Image.DecompressionBombError, UnidentifiedImageError) as exc:
        if isinstance(exc, FaceCaptureError):
            raise
        raise FaceCaptureError("Could not read the camera image.") from exc


def encode_camera_image(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
    image_bytes = output.getvalue()
    if not image_bytes or len(image_bytes) > MAX_CAPTURE_BYTES:
        raise FaceCaptureError("Camera image is empty or too large to retain.")
    return image_bytes
