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
COSINE_MATCH_THRESHOLD = settings.FACE_COSINE_MATCH_THRESHOLD
YUNET_SCORE_THRESHOLD = 0.65
MIN_FACE_SIZE_PIXELS = 48
MIN_FACE_SIZE_RATIO = 0.10
MIN_FACE_ASPECT_RATIO = 0.55
MAX_FACE_ASPECT_RATIO = 1.65
MAX_FACE_ROLL_DEGREES = 20
MIN_YOLO_SECOND_FACE_CONFIDENCE = 0.75
MIN_BLUR_VARIANCE = 15.0
MIN_FACE_BRIGHTNESS = 35.0
MAX_FACE_BRIGHTNESS = 225.0
MIN_FACE_CONTRAST = 12.0
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


class FaceCandidate(TypedDict):
    confidence: float
    bbox: list[int]
    roll_degrees: float | None
    alignment_ready: bool
    quality_issue: str | None


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
            YUNET_SCORE_THRESHOLD,
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


def cosine_similarity(first_embedding: bytes, second_embedding: bytes) -> float:
    try:
        first = np.frombuffer(bytes(first_embedding), dtype=np.float32)
        second = np.frombuffer(bytes(second_embedding), dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise FaceModelError("A face embedding could not be read.") from exc
    if first.size == 0 or first.shape != second.shape:
        raise FaceModelError("Face embeddings have incompatible sizes.")
    if not np.isfinite(first).all() or not np.isfinite(second).all():
        raise FaceModelError("A face embedding contains invalid values.")
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    if denominator == 0:
        raise FaceModelError("A face embedding has zero magnitude.")
    similarity = float(np.dot(first, second) / denominator)
    if not np.isfinite(similarity):
        raise FaceModelError("Could not calculate face similarity.")
    return similarity


def encrypt_face_capture(image_bytes: bytes) -> bytes:
    try:
        return Fernet(_encryption_key()).encrypt(image_bytes)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FaceModelError("Face-capture encryption is not configured correctly.") from exc


def decrypt_face_capture(encrypted_image: bytes) -> bytes:
    try:
        return Fernet(_encryption_key()).decrypt(bytes(encrypted_image))
    except (InvalidToken, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FaceModelError(
            "Could not decrypt the attendance capture; check the encryption key."
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


def _face_quality_issue(frame: np.ndarray, bbox: list[int]) -> str | None:
    left, top, right, bottom = bbox
    face_crop = frame[top:bottom, left:right]
    if face_crop.size == 0:
        return "alignment"
    gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < MIN_FACE_BRIGHTNESS or brightness > MAX_FACE_BRIGHTNESS:
        return "lighting"
    if float(np.std(gray)) < MIN_FACE_CONTRAST:
        return "contrast"
    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if blur_variance < MIN_BLUR_VARIANCE:
        return "blur"
    return None


def _yunet_candidate(face, width: int, height: int):
    row = np.asarray(face, dtype=np.float32).reshape(-1)
    if row.size < 15 or not np.isfinite(row[:15]).all() or row[14] < YUNET_SCORE_THRESHOLD:
        return None

    left, top, box_width, box_height = map(float, row[:4])
    right, bottom = left + box_width, top + box_height
    minimum_size = max(
        MIN_FACE_SIZE_PIXELS,
        round(min(width, height) * MIN_FACE_SIZE_RATIO),
    )
    aspect_ratio = box_width / box_height if box_height > 0 else 0
    if (
        left < 0
        or top < 0
        or right > width
        or bottom > height
        or min(box_width, box_height) < 24
        or not MIN_FACE_ASPECT_RATIO <= aspect_ratio <= MAX_FACE_ASPECT_RATIO
    ):
        return None

    landmarks = row[4:14].reshape(5, 2)
    x1, y1, x2, y2 = left, top, right, bottom
    margin_x, margin_y = box_width * 0.1, box_height * 0.1
    if (
        (landmarks[:, 0] < x1 - margin_x).any()
        or (landmarks[:, 0] > x2 + margin_x).any()
        or (landmarks[:, 1] < y1 - margin_y).any()
        or (landmarks[:, 1] > y2 + margin_y).any()
        or (landmarks[:, 0] < 0).any()
        or (landmarks[:, 0] >= width).any()
        or (landmarks[:, 1] < 0).any()
        or (landmarks[:, 1] >= height).any()
    ):
        return None

    right_eye, left_eye, nose, right_mouth, left_mouth = landmarks
    eye_distance = float(np.linalg.norm(right_eye - left_eye))
    mouth_distance = float(np.linalg.norm(right_mouth - left_mouth))
    if (
        eye_distance < box_width * 0.18
        or eye_distance > box_width * 0.8
        or mouth_distance < box_width * 0.1
        or not (float(np.mean(landmarks[:2, 1])) < nose[1] < np.mean(landmarks[3:, 1]))
    ):
        return None

    roll_degrees = float(np.degrees(np.arctan2(
        float(left_eye[1] - right_eye[1]),
        float(left_eye[0] - right_eye[0]),
    )))
    bbox = [round(x1), round(y1), round(x2), round(y2)]
    return {
        "face": row,
        "candidate": FaceCandidate(
            confidence=round(float(row[14]), 4),
            bbox=bbox,
            roll_degrees=round(roll_degrees, 2),
            alignment_ready=min(box_width, box_height) >= minimum_size,
            quality_issue="too_far" if min(box_width, box_height) < minimum_size else None,
        ),
        "too_far": min(box_width, box_height) < minimum_size,
    }


def _inspect_face_candidates(image: Image.Image):
    try:
        detections = detect_faces(image)
    except FileNotFoundError as exc:
        raise FaceModelError("The trained YOLO face detector is missing.") from exc
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        LOGGER.exception("Could not detect a face for recognition.")
        raise FaceModelError("The trained YOLO face detector could not process the image.") from exc

    frame = _image_to_bgr(image)
    try:
        with _MODEL_LOCK:
            yunet, sface = _load_models()
            yunet.setInputSize((frame.shape[1], frame.shape[0]))
            _, yunet_faces = yunet.detect(frame)
    except cv2.error as exc:
        LOGGER.exception("Could not detect faces for alignment.")
        raise FaceModelError("The face-recognition model could not process the image.") from exc

    valid_candidates = []
    for raw_face in yunet_faces if yunet_faces is not None else ():
        candidate = _yunet_candidate(raw_face, frame.shape[1], frame.shape[0])
        if candidate is None:
            continue
        if any(
            _intersection_over_union(existing["candidate"]["bbox"], candidate["face"]) >= 0.5
            for existing in valid_candidates
        ):
            continue
        candidate["candidate"]["alignment_ready"] = any(
            _intersection_over_union(detection["bbox"], candidate["face"]) >= 0.2
            for detection in detections["detections"]
        ) and not candidate["too_far"]
        if not candidate["too_far"]:
            candidate["candidate"]["quality_issue"] = _face_quality_issue(
                frame,
                candidate["candidate"]["bbox"],
            )
        valid_candidates.append(candidate)

    if any(not candidate["too_far"] for candidate in valid_candidates):
        valid_candidates = [candidate for candidate in valid_candidates if not candidate["too_far"]]

    for detection in detections["detections"]:
        if detection["confidence"] < MIN_YOLO_SECOND_FACE_CONFIDENCE:
            continue
        if min(
            detection["bbox"][2] - detection["bbox"][0],
            detection["bbox"][3] - detection["bbox"][1],
        ) < max(72, round(min(frame.shape[:2]) * 0.15)):
            continue
        if any(
            _intersection_over_union(detection["bbox"], candidate["face"]) >= 0.2
            for candidate in valid_candidates
        ):
            continue
        valid_candidates.append({
            "face": None,
            "candidate": FaceCandidate(
                confidence=detection["confidence"],
                bbox=detection["bbox"],
                roll_degrees=None,
                alignment_ready=False,
                quality_issue="alignment",
            ),
        })

    return frame, sface, valid_candidates


def inspect_face_candidates(image: Image.Image) -> list[FaceCandidate]:
    _, _, candidates = _inspect_face_candidates(image)
    return [candidate["candidate"] for candidate in candidates]


def create_face_embedding(image: Image.Image) -> bytes:
    frame, sface, candidates = _inspect_face_candidates(image)
    if len(candidates) != 1:
        if candidates:
            raise FaceImageError("Only one clearly visible face should be in the camera frame.")
        raise FaceImageError("No complete, clearly visible face could be aligned.")

    selected = candidates[0]
    candidate = selected["candidate"]
    if candidate["quality_issue"] == "too_far":
        raise FaceImageError("Move closer to the camera so your face is large enough to verify.")
    if not candidate["alignment_ready"] or selected["face"] is None:
        raise FaceImageError("Look directly at the camera and keep your full face visible.")
    if candidate["roll_degrees"] is not None and abs(candidate["roll_degrees"]) > MAX_FACE_ROLL_DEGREES:
        raise FaceImageError("Keep your head level and look directly at the camera.")
    if candidate["quality_issue"] == "lighting":
        raise FaceImageError("Please move to a brighter area with even lighting.")
    if candidate["quality_issue"] == "contrast":
        raise FaceImageError("Please move to a brighter area so your face is clearly visible.")
    if candidate["quality_issue"] == "blur":
        raise FaceImageError("The face is blurry. Keep your face steady and try again.")

    try:
        with _MODEL_LOCK:
            aligned_crop = sface.alignCrop(frame, selected["face"])
            if _face_quality_issue(
                aligned_crop,
                [0, 0, aligned_crop.shape[1], aligned_crop.shape[0]],
            ) is not None:
                raise FaceImageError("The face is blurry or poorly lit. Adjust the camera and try again.")
            feature = sface.feature(aligned_crop)
    except cv2.error as exc:
        LOGGER.exception("Could not align or embed the detected face.")
        raise FaceModelError("The face-recognition model could not process the image.") from exc

    vector = np.asarray(feature, dtype=np.float32).reshape(-1)
    if vector.size != 128 or not np.isfinite(vector).all():
        raise FaceModelError("The face-recognition model returned an invalid embedding.")
    magnitude = float(np.linalg.norm(vector))
    if not np.isfinite(magnitude) or magnitude == 0:
        raise FaceModelError("The face-recognition model returned an empty embedding.")
    vector /= magnitude
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
