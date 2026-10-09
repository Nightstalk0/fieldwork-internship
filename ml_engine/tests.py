import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
import numpy as np
from PIL import Image
from cryptography.fernet import Fernet
from django.test import override_settings

from .face_capture import FaceCaptureError, decode_camera_image
from .face_detector import detect_faces
from .face_recognition import (
    compare_face_embedding,
    create_face_embedding,
    decrypt_embedding,
    encrypt_embedding,
)
from .predictors import Model1CompletionPredictor, Model2RiskPredictor, Model3MatchPredictor, Model4PerformancePredictor


class FaceDetectorTests(SimpleTestCase):
    @patch("ml_engine.face_detector.load_face_model")
    def test_returns_confidence_and_bounding_box_for_faces(self, load_model):
        model = load_model.return_value
        image = Image.new("RGB", (640, 480))
        model.predict.return_value = [
            SimpleNamespace(boxes=[
                SimpleNamespace(cls=[0], conf=[0.91], xyxy=[[120.2, 80.4, 420.1, 380.3]]),
                SimpleNamespace(cls=[0], conf=[0.84], xyxy=[[12.0, 25.0, 100.0, 130.0]]),
                SimpleNamespace(cls=[1], conf=[0.99], xyxy=[[0.0, 0.0, 10.0, 10.0]]),
            ]),
        ]

        result = detect_faces(image)

        self.assertTrue(result["face_detected"])
        self.assertEqual(result["face_count"], 2)
        self.assertEqual(result["detections"], [
            {"confidence": 0.91, "bbox": [120, 80, 420, 380]},
            {"confidence": 0.84, "bbox": [12, 25, 100, 130]},
        ])
        model.predict.assert_called_once_with(
            source=image,
            imgsz=640,
            device="cpu",
            verbose=False,
        )

    @patch("ml_engine.face_detector.load_face_model")
    def test_returns_empty_detection_list_for_no_faces(self, load_model):
        load_model.return_value.predict.return_value = [SimpleNamespace(boxes=[])]

        result = detect_faces(Image.new("RGB", (640, 480)))

        self.assertEqual(result, {
            "face_detected": False,
            "face_count": 0,
            "detections": [],
        })


class FaceRecognitionTests(SimpleTestCase):
    def _camera_data_url(self):
        image = BytesIO()
        Image.new("RGB", (32, 32), "white").save(image, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode("ascii")

    def test_camera_image_is_decoded_without_saving_it(self):
        image = decode_camera_image(self._camera_data_url())

        self.assertEqual(image.size, (32, 32))
        self.assertEqual(image.mode, "RGB")

    def test_camera_decoder_rejects_non_jpeg_input(self):
        with self.assertRaises(FaceCaptureError):
            decode_camera_image("data:image/png;base64,AAAA")

    @override_settings(FACE_EMBEDDING_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"))
    def test_encrypted_embedding_round_trips(self):
        embedding = b"embedding bytes"

        self.assertNotEqual(encrypt_embedding(embedding), embedding)
        self.assertEqual(decrypt_embedding(encrypt_embedding(embedding)), embedding)

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_embedding_requires_one_detected_face_and_returns_128_values(
        self,
        detect_faces_mock,
        load_models_mock,
        image_to_bgr_mock,
    ):
        detect_faces_mock.return_value = {
            "face_detected": True,
            "face_count": 1,
            "detections": [{"confidence": 0.95, "bbox": [10, 10, 100, 100]}],
        }
        yunet = SimpleNamespace(
            setInputSize=lambda _size: None,
            detect=lambda _image: (None, [[10, 10, 90, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.99]]),
        )
        sface = SimpleNamespace(
            alignCrop=lambda _image, _face: np.zeros((112, 112, 3), dtype=np.uint8),
            feature=lambda _image: [[0.0] * 128],
        )
        load_models_mock.return_value = (yunet, sface)
        image_to_bgr_mock.return_value = np.zeros((120, 120, 3), dtype=np.uint8)

        embedding = create_face_embedding(Image.new("RGB", (120, 120)))

        self.assertEqual(len(embedding), 128 * 4)

    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.decrypt_embedding", return_value=b"\x00" * (128 * 4))
    @patch("ml_engine.face_recognition.create_face_embedding", return_value=b"\x00" * (128 * 4))
    def test_match_returns_similarity_and_pilot_label(self, create_embedding_mock, decrypt_mock, load_models_mock):
        recognizer = SimpleNamespace(match=lambda _current, _reference, _mode: 0.55)
        load_models_mock.return_value = (None, recognizer)

        result = compare_face_embedding(Image.new("RGB", (10, 10)), b"encrypted")

        self.assertEqual(result, {"status": "matched", "similarity": 0.55})
        create_embedding_mock.assert_called_once()
        decrypt_mock.assert_called_once_with(b"encrypted")


class PredictorFallbackTests(SimpleTestCase):
    def test_rule_based_predictions_are_deterministic_and_bounded(self):
        predictors = (
            (Model1CompletionPredictor(), (0.8, 0.9, 4.0)),
            (Model2RiskPredictor(), (2, 1, 0.7)),
            (Model3MatchPredictor(), (0.8, 0.6, 1.0)),
            (Model4PerformancePredictor(), (4, 3, 5, 4)),
        )
        for predictor, values in predictors:
            predictor.model = None
            first = predictor.predict(*values)
            self.assertEqual(first, predictor.predict(*values))
            self.assertGreaterEqual(first, 0)
            self.assertLessEqual(first, 1)