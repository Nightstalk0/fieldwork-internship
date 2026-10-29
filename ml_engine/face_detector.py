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
CONFIDENCE_THRESHOLD = 0.25
FACE_CLASS_ID = 0
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
        if int(round(float(class_id))) != FACE_CLASS_ID or confidence < CONFIDENCE_THRESHOLD:
            continue

        left = max(0, min(original_width, round((float(x1) - pad_x) / scale)))
        top = max(0, min(original_height, round((float(y1) - pad_y) / scale)))
        right = max(0, min(original_width, round((float(x2) - pad_x) / scale)))
        bottom = max(0, min(original_height, round((float(y2) - pad_y) / scale)))
        if right <= left or bottom <= top:
            continue

        detections.append({
            "confidence": round(float(confidence), 4),
            "bbox": [left, top, right, bottom],
        })
    return detections


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
