import logging
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import TypedDict

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image


MODEL_PATH = Path(__file__).resolve().parent / "models" / "best.onnx"
IMAGE_SIZE = 416
CONFIDENCE_THRESHOLD = 0.35
FACE_CLASS_ID = 0
MIN_FACE_SIZE_PIXELS = 48
MIN_FACE_SIZE_RATIO = 0.08
MIN_VISIBLE_FRACTION = 0.9
NMS_IOU_THRESHOLD = 0.45
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
def load_face_model() -> ort.InferenceSession:
    if not MODEL_PATH.is_file():
        raise FileNotFoundError(
            f"ONNX face-detection model not found: {MODEL_PATH}. "
            "Run ml_engine/export_face_detector.py and include best.onnx in the deployment."
        )

    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False

    try:
        return ort.InferenceSession(
            str(MODEL_PATH),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        LOGGER.exception("Could not load ONNX face-detection model.")
        raise RuntimeError("Could not load the ONNX face-detection model.") from exc


def _read_image(image: ImageSource) -> Image.Image:
    if isinstance(image, (str, Path)):
        image_path = Path(image)
        if not image_path.is_file():
            raise FileNotFoundError(f"Input image not found: {image_path}")
        with Image.open(image_path) as opened_image:
            return opened_image.convert("RGB")
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    raise TypeError("image must be a filesystem path or a PIL image")


def _prepare_input(image: Image.Image) -> tuple[np.ndarray, float, int, int]:
    width, height = image.size
    scale = min(IMAGE_SIZE / width, IMAGE_SIZE / height)
    resized_width = round(width * scale)
    resized_height = round(height * scale)

    rgb_image = np.asarray(image, dtype=np.uint8)
    bgr_image = cv2.cvtColor(rgb_image, cv2.COLOR_RGB2BGR)
    resized = cv2.resize(
        bgr_image,
        (resized_width, resized_height),
        interpolation=cv2.INTER_LINEAR,
    )

    horizontal_padding = IMAGE_SIZE - resized_width
    vertical_padding = IMAGE_SIZE - resized_height
    left = round(horizontal_padding / 2 - 0.1)
    right = round(horizontal_padding / 2 + 0.1)
    top = round(vertical_padding / 2 - 0.1)
    bottom = round(vertical_padding / 2 + 0.1)
    padded = cv2.copyMakeBorder(
        resized,
        top,
        bottom,
        left,
        right,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    model_input = np.ascontiguousarray(
        padded.transpose(2, 0, 1)[None],
        dtype=np.float32,
    )
    model_input /= 255.0
    return model_input, scale, left, top


def _decode_detections(
    output: np.ndarray,
    *,
    scale: float,
    pad_x: int,
    pad_y: int,
    original_width: int,
    original_height: int,
) -> list[FaceDetection]:
    detections_output = np.asarray(output)
    if detections_output.ndim == 3:
        if detections_output.shape[0] != 1:
            raise RuntimeError("ONNX face detector returned an unexpected batch size.")
        detections_output = detections_output[0]
    if detections_output.ndim != 2 or detections_output.shape[1] < 6:
        raise RuntimeError(
            "ONNX face detector output must contain [x1, y1, x2, y2, confidence, class] rows."
        )

    detections: list[FaceDetection] = []
    for x1, y1, x2, y2, confidence, class_id, *_ in detections_output:
        values = np.asarray([x1, y1, x2, y2, confidence, class_id], dtype=np.float64)
        if not np.isfinite(values).all():
            continue
        if int(round(float(class_id))) != FACE_CLASS_ID or confidence < CONFIDENCE_THRESHOLD:
            continue

        raw_left = (float(x1) - pad_x) / scale
        raw_top = (float(y1) - pad_y) / scale
        raw_right = (float(x2) - pad_x) / scale
        raw_bottom = (float(y2) - pad_y) / scale
        raw_width = raw_right - raw_left
        raw_height = raw_bottom - raw_top
        if raw_width <= 0 or raw_height <= 0:
            continue

        left = max(0, min(original_width, round(raw_left)))
        top = max(0, min(original_height, round(raw_top)))
        right = max(0, min(original_width, round(raw_right)))
        bottom = max(0, min(original_height, round(raw_bottom)))
        if right <= left or bottom <= top:
            continue
        visible_fraction = ((right - left) * (bottom - top)) / (raw_width * raw_height)
        minimum_size = max(
            MIN_FACE_SIZE_PIXELS,
            round(min(original_width, original_height) * MIN_FACE_SIZE_RATIO),
        )
        aspect_ratio = (right - left) / (bottom - top)
        if (
            visible_fraction < MIN_VISIBLE_FRACTION
            or min(right - left, bottom - top) < minimum_size
            or not 0.55 <= aspect_ratio <= 1.65
        ):
            continue

        detections.append({
            "confidence": round(float(confidence), 4),
            "bbox": [left, top, right, bottom],
        })

    ordered = sorted(detections, key=lambda detection: detection["confidence"], reverse=True)
    filtered: list[FaceDetection] = []
    for candidate in ordered:
        x1, y1, x2, y2 = candidate["bbox"]
        candidate_area = (x2 - x1) * (y2 - y1)
        overlaps_existing = False
        for existing in filtered:
            ex1, ey1, ex2, ey2 = existing["bbox"]
            intersection_width = max(0, min(x2, ex2) - max(x1, ex1))
            intersection_height = max(0, min(y2, ey2) - max(y1, ey1))
            intersection = intersection_width * intersection_height
            existing_area = (ex2 - ex1) * (ey2 - ey1)
            union = candidate_area + existing_area - intersection
            if union and intersection / union >= NMS_IOU_THRESHOLD:
                overlaps_existing = True
                break
        if not overlaps_existing:
            filtered.append(candidate)
    return filtered


def detect_faces(image: ImageSource) -> FaceDetectionResponse:
    pil_image = _read_image(image)
    model_input, scale, pad_x, pad_y = _prepare_input(pil_image)
    session = load_face_model()
    input_name = session.get_inputs()[0].name

    with _PREDICTION_LOCK:
        try:
            outputs = session.run(None, {input_name: model_input})
        except Exception as exc:
            LOGGER.exception("ONNX face-detection inference failed.")
            raise RuntimeError("ONNX face-detection inference failed.") from exc

    if not outputs:
        raise RuntimeError("ONNX face detector returned no outputs.")
    detections = _decode_detections(
        outputs[0],
        scale=scale,
        pad_x=pad_x,
        pad_y=pad_y,
        original_width=pil_image.width,
        original_height=pil_image.height,
    )
    return {
        "face_detected": bool(detections),
        "face_count": len(detections),
        "detections": detections,
    }
