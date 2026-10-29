from pathlib import Path

from ultralytics import YOLO


ML_ENGINE_DIR = Path(__file__).resolve().parent
MODEL_DIR = ML_ENGINE_DIR / "models"
CHECKPOINT_PATH = MODEL_DIR / "best.pt"


def main() -> None:
    if not CHECKPOINT_PATH.is_file():
        raise FileNotFoundError(f"Face-detection checkpoint not found: {CHECKPOINT_PATH}")

    model = YOLO(str(CHECKPOINT_PATH))
    exported_path = Path(model.export(
        format="onnx",
        imgsz=416,
        batch=1,
        device="cpu",
        dynamic=False,
        simplify=False,
        nms=True,
        max_det=10,
        conf=0.25,
    ))
    destination = MODEL_DIR / "best.onnx"
    if exported_path.resolve() != destination.resolve():
        exported_path.replace(destination)
    print(f"Exported face detector: {destination}")


if __name__ == "__main__":
    main()
