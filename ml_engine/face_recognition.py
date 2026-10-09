import base64
import hashlib
import logging
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Literal, TypedDict

import cv2
import numpy as np
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from PIL import Image

from .face_detector import detect_faces


LOGGER = logging.getLogger(__name__)
MODEL_DIR = Path(__file__).resolve().parent / "models"
SFACE_MODEL_PATH = MODEL_DIR / "face_recognition_sface_2021dec.onnx"
YUNET_MODEL_PATH = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_MODEL_VERSION = "opencv-sface-2021dec-v1"
COSINE_MATCH_THRESHOLD = 0.363
_MODEL_LOCK = Lock()


class FaceRecognitionError(Exception):
    pass


class FaceImageError(FaceRecognitionError):
    pass


class FaceModelError(FaceRecognitionError):
    pass


class FaceMatch(TypedDict):
    status: Literal["matched", "not_matched"]
    similarity: float


@lru_cache(maxsize=1)
def _load_models():
    missing = [path.name for path in (SFACE_MODEL_PATH, YUNET_MODEL_PATH) if not path.is_file()]
    if missing:
        raise FaceModelError(f"Face-recognition model files are missing: {', '.join(missing)}")
    try:
        yunet = cv2.FaceDetectorYN_create(
            str(YUNET_MODEL_PATH),
            "",
            (320, 320),
            0.6,
            0.3,
            5000,
        )
        sface = cv2.FaceRecognizerSF_create(str(SFACE_MODEL_PATH), "")
    except cv2.error as exc:
        LOGGER.exception("Could not load face-recognition ONNX models.")
        raise FaceModelError("Could not load face-recognition models.") from exc
    return yunet, sface


def _encryption_key() -> bytes:
    configured_key = getattr(settings, "FACE_EMBEDDING_ENCRYPTION_KEY", "")
    if configured_key:
        return configured_key.encode("ascii")
    if not settings.DEBUG:
        raise FaceModelError(
            "Set FACE_EMBEDDING_ENCRYPTION_KEY when DEBUG is disabled."
        )
    derived_key = hashlib.sha256(
        b"fieldwork-face-embeddings-v1:" + settings.SECRET_KEY.encode("utf-8")
    ).digest()
    return base64.urlsafe_b64encode(derived_key)


def encrypt_embedding(embedding: bytes) -> bytes:
    try:
        return Fernet(_encryption_key()).encrypt(embedding)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FaceModelError("Face-embedding encryption is not configured correctly.") from exc


def decrypt_embedding(encrypted_embedding: bytes) -> bytes:
    try:
        return Fernet(_encryption_key()).decrypt(bytes(encrypted_embedding))
    except (InvalidToken, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FaceModelError(
            "Could not decrypt the enrolled face embedding; check the encryption key."
        ) from exc


def _image_to_bgr(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)


def _intersection_over_union(box_a, box_b) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bw, bh = box_b[:4]
    bx2, by2 = bx1 + bw, by1 + bh
    intersection_width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    intersection_height = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = intersection_width * intersection_height
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bw) * max(0.0, bh)
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def create_face_embedding(image: Image.Image) -> bytes:
    try:
        detections = detect_faces(image)
    except FileNotFoundError as exc:
        raise FaceModelError("The trained YOLO face detector is missing.") from exc
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        LOGGER.exception("Could not detect a face for recognition.")
        raise FaceModelError("The trained YOLO face detector could not process the image.") from exc
    if detections["face_count"] != 1:
        raise FaceImageError("Capture must contain exactly one clearly visible face.")

    frame = _image_to_bgr(image)
    try:
        with _MODEL_LOCK:
            yunet, sface = _load_models()
            yunet.setInputSize((frame.shape[1], frame.shape[0]))
            _, yunet_faces = yunet.detect(frame)
            if yunet_faces is None or len(yunet_faces) == 0:
                raise FaceImageError("Could not align the detected face.")
            target_box = detections["detections"][0]["bbox"]
            aligned_face = max(
                yunet_faces,
                key=lambda face: _intersection_over_union(target_box, face),
            )
            if _intersection_over_union(target_box, aligned_face) < 0.2:
                raise FaceImageError("The face could not be aligned reliably.")
            aligned_crop = sface.alignCrop(frame, aligned_face)
            feature = sface.feature(aligned_crop)
    except cv2.error as exc:
        LOGGER.exception("Could not align or embed the detected face.")
        raise FaceModelError("The face-recognition model could not process the image.") from exc

    vector = np.asarray(feature, dtype=np.float32).reshape(-1)
    if vector.size != 128 or not np.isfinite(vector).all():
        raise FaceModelError("The face-recognition model returned an invalid embedding.")
    return vector.tobytes()


def compare_face_embedding(image: Image.Image, enrolled_embedding: bytes) -> FaceMatch:
    current_embedding = np.frombuffer(create_face_embedding(image), dtype=np.float32).reshape(1, -1)
    reference_embedding = np.frombuffer(
        decrypt_embedding(enrolled_embedding),
        dtype=np.float32,
    ).reshape(1, -1)
    if current_embedding.shape != reference_embedding.shape:
        raise FaceModelError("The enrolled face embedding has an unexpected size.")

    try:
        with _MODEL_LOCK:
            _, sface = _load_models()
            similarity = float(sface.match(
                current_embedding,
                reference_embedding,
                cv2.FaceRecognizerSF_FR_COSINE,
            ))
    except cv2.error as exc:
        LOGGER.exception("Could not compare face embeddings.")
        raise FaceModelError("Could not compare face embeddings.") from exc

    if not np.isfinite(similarity):
        raise FaceModelError("The face-recognition model returned an invalid score.")
    return {
        "status": "matched" if similarity >= COSINE_MATCH_THRESHOLD else "not_matched",
        "similarity": round(similarity, 4),
    }
