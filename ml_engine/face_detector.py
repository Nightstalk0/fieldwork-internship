import logging
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import TypedDict

from PIL import Image


MODEL_PATH = Path(__file__).resolve().parent / "models" / "best.pt"
ImageSource = str | Path | Image.Image
_PREDICTION_LOCK = Lock()
LOGGER = logging.getLogger(__name__)


class FaceDetection(TypedDict):
    confidence: float
    bbox: list[int]


class FaceDetectionResponse(TypedDict):
    face_detected: bool
    face_count: int
    detections: list[FaceDetection]


@lru_cache(maxsize=1)
def load_face_model():
    if not MODEL_PATH.is_file():
        raise FileNotFoundError(f"Face-detection model not found: {MODEL_PATH}")

    import torch

    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        LOGGER.warning("PyTorch inter-op thread limit was already initialized.")

    from ultralytics import YOLO

    return YOLO(str(MODEL_PATH))


def detect_faces(image: ImageSource) -> FaceDetectionResponse:
    if isinstance(image, (str, Path)):
        image_path = Path(image)
        if not image_path.is_file():
            raise FileNotFoundError(f"Input image not found: {image_path}")
        source: str | Image.Image = str(image_path)
    elif isinstance(image, Image.Image):
        source = image
    else:
        raise TypeError("image must be a filesystem path or a PIL image")

    detections: list[FaceDetection] = []
    with _PREDICTION_LOCK:
        results = load_face_model().predict(
            source=source,
            imgsz=416,
            device="cpu",
            max_det=10,
            verbose=False,
        )
    try:
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                if int(box.cls[0]) != 0:
                    continue
                detections.append({
                    "confidence": round(float(box.conf[0]), 4),
                    "bbox": [int(round(float(value))) for value in box.xyxy[0]],
                })
    finally:
        del results

    return {
        "face_detected": bool(detections),
        "face_count": len(detections),
        "detections": detections,
    }
