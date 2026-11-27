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
    FaceImageError,
    compare_face_embedding,
    cosine_similarity,
    create_face_embedding,
    decrypt_embedding,
    encrypt_embedding,
    inspect_face_candidates,
)
from .predictors import Model1CompletionPredictor, Model2RiskPredictor, Model3MatchPredictor, Model4PerformancePredictor


class FaceDetectorTests(SimpleTestCase):
    @patch("ml_engine.face_detector.load_face_model")
    def test_returns_confidence_and_bounding_box_for_faces(self, load_model):
        session = load_model.return_value
        session.get_inputs.return_value = [SimpleNamespace(name="images")]
        session.run.return_value = [np.array([[
            [78, 104, 273, 299, 0.91, 0],
            [80, 105, 271, 296, 0.88, 0],
            [8, 68, 65, 137, 0.84, 0],
            [30, 30, 45, 45, 0.96, 0],
            [-80, 100, 100, 300, 0.97, 0],
            [0, 0, 416, 416, 0.99, 1],
            [0, 0, 100, 100, 0.10, 0],
            [0, 0, 0, 0, 0, 0],
        ]], dtype=np.float32)]
        image = Image.new("RGB", (640, 480))

        result = detect_faces(image)

        self.assertTrue(result["face_detected"])
        self.assertEqual(result["face_count"], 2)
        self.assertEqual(result["detections"], [
            {"confidence": 0.91, "bbox": [120, 80, 420, 380]},
            {"confidence": 0.84, "bbox": [12, 25, 100, 131]},
        ])
        session.run.assert_called_once()
        model_input = session.run.call_args.args[1]["images"]
        self.assertEqual(model_input.shape, (1, 3, 416, 416))
        self.assertEqual(model_input.dtype, np.float32)
        self.assertGreaterEqual(float(model_input.min()), 0)
        self.assertLessEqual(float(model_input.max()), 1)

    @patch("ml_engine.face_detector.load_face_model")
    def test_returns_empty_detection_list_for_no_faces(self, load_model):
        session = load_model.return_value
        session.get_inputs.return_value = [SimpleNamespace(name="images")]
        session.run.return_value = [np.zeros((1, 10, 6), dtype=np.float32)]

        result = detect_faces(Image.new("RGB", (640, 480)))

        self.assertEqual(result, {
            "face_detected": False,
            "face_count": 0,
            "detections": [],
        })

    @patch("ml_engine.face_detector.load_face_model")
    def test_converts_alpha_images_to_three_channel_onnx_input(self, load_model):
        session = load_model.return_value
        session.get_inputs.return_value = [SimpleNamespace(name="images")]
        session.run.return_value = [np.zeros((1, 10, 6), dtype=np.float32)]

        result = detect_faces(Image.new("RGBA", (32, 24), (255, 0, 0, 128)))

        self.assertFalse(result["face_detected"])
        model_input = session.run.call_args.args[1]["images"]
        self.assertEqual(model_input.shape, (1, 3, 416, 416))


class FaceRecognitionTests(SimpleTestCase):
    def _camera_data_url(self):
        image = BytesIO()
        Image.new("RGB", (32, 32), "white").save(image, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode("ascii")

    def _yunet_detection(self, x=10, y=10, width=90, height=100, score=0.99):
        return [
            x,
            y,
            width,
            height,
            x + width * 0.32,
            y + height * 0.38,
            x + width * 0.68,
            y + height * 0.38,
            x + width * 0.5,
            y + height * 0.56,
            x + width * 0.36,
            y + height * 0.76,
            x + width * 0.64,
            y + height * 0.76,
            score,
        ]

    def _face_models(self, yunet_faces):
        quality_crop = np.random.default_rng(42).integers(50, 200, (112, 112, 3), dtype=np.uint8)
        yunet = SimpleNamespace(
            setInputSize=lambda _size: None,
            detect=lambda _image: (None, yunet_faces),
        )
        sface = SimpleNamespace(
            alignCrop=lambda _image, _face: quality_crop,
            feature=lambda _image: [[1.0] + [0.0] * 127],
        )
        return yunet, sface

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
        load_models_mock.return_value = self._face_models([self._yunet_detection()])
        image_to_bgr_mock.return_value = np.random.default_rng(43).integers(
            50, 200, (120, 120, 3), dtype=np.uint8
        )

        embedding = create_face_embedding(Image.new("RGB", (120, 120)))

        self.assertEqual(len(embedding), 128 * 4)
        self.assertAlmostEqual(float(np.linalg.norm(np.frombuffer(embedding, dtype=np.float32))), 1.0)

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_embedding_rejects_multiple_alignment_faces(
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
        load_models_mock.return_value = self._face_models([
            self._yunet_detection(x=5, y=20, width=50, height=75),
            self._yunet_detection(x=65, y=20, width=50, height=75, score=0.98),
        ])
        image_to_bgr_mock.return_value = np.random.default_rng(44).integers(
            50, 200, (120, 120, 3), dtype=np.uint8
        )

        with self.assertRaises(FaceImageError):
            create_face_embedding(Image.new("RGB", (120, 120)))

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_candidate_inspection_ignores_weak_yunet_and_yolo_background_boxes(
        self,
        detect_faces_mock,
        load_models_mock,
        image_to_bgr_mock,
    ):
        detect_faces_mock.return_value = {
            "face_detected": True,
            "face_count": 2,
            "detections": [
                {"confidence": 0.95, "bbox": [10, 10, 100, 100]},
                {"confidence": 0.52, "bbox": [5, 15, 70, 90]},
            ],
        }
        invalid_landmarks = [20, 10, 80, 90] + [0] * 10 + [0.99]
        load_models_mock.return_value = self._face_models([
            self._yunet_detection(),
            invalid_landmarks,
            self._yunet_detection(x=10, y=10, width=90, height=100, score=0.4),
        ])
        image_to_bgr_mock.return_value = np.random.default_rng(45).integers(
            50, 200, (120, 120, 3), dtype=np.uint8
        )

        candidates = inspect_face_candidates(Image.new("RGB", (120, 120)))

        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["alignment_ready"])
        self.assertIsNone(candidates[0]["quality_issue"])

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_candidate_inspection_flags_poor_face_lighting(
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
        load_models_mock.return_value = self._face_models([self._yunet_detection()])
        image_to_bgr_mock.return_value = np.full((120, 120, 3), 5, dtype=np.uint8)

        candidates = inspect_face_candidates(Image.new("RGB", (120, 120)))

        self.assertEqual(candidates[0]["quality_issue"], "lighting")

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_candidate_inspection_rejects_blurry_face_crops(
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
        load_models_mock.return_value = self._face_models([self._yunet_detection()])
        gradient = np.tile(np.linspace(80, 160, 120, dtype=np.uint8)[None, :, None], (120, 1, 3))
        image_to_bgr_mock.return_value = gradient

        candidates = inspect_face_candidates(Image.new("RGB", (120, 120)))

        self.assertEqual(candidates[0]["quality_issue"], "blur")

    @patch("ml_engine.face_recognition._image_to_bgr")
    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.detect_faces")
    def test_candidate_inspection_identifies_a_face_that_needs_to_move_closer(
        self,
        detect_faces_mock,
        load_models_mock,
        image_to_bgr_mock,
    ):
        detect_faces_mock.return_value = {"face_detected": False, "face_count": 0, "detections": []}
        load_models_mock.return_value = self._face_models([
            self._yunet_detection(x=40, y=40, width=24, height=35),
        ])
        image_to_bgr_mock.return_value = np.random.default_rng(46).integers(
            50, 200, (120, 120, 3), dtype=np.uint8
        )

        candidates = inspect_face_candidates(Image.new("RGB", (120, 120)))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["quality_issue"], "too_far")
        self.assertFalse(candidates[0]["alignment_ready"])

    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.decrypt_embedding", return_value=b"\x00" * (128 * 4))
    @patch("ml_engine.face_recognition.create_face_embedding", return_value=b"\x00" * (128 * 4))
    def test_match_requires_strict_similarity_threshold(self, create_embedding_mock, decrypt_mock, load_models_mock):
        recognizer = SimpleNamespace(match=lambda _current, _reference, _mode: 0.55)
        load_models_mock.return_value = (None, recognizer)

        result = compare_face_embedding(Image.new("RGB", (10, 10)), b"encrypted")

        self.assertEqual(result, {"status": "not_matched", "similarity": 0.55})
        create_embedding_mock.assert_called_once()
        decrypt_mock.assert_called_once_with(b"encrypted")

    @patch("ml_engine.face_recognition._load_models")
    @patch("ml_engine.face_recognition.decrypt_embedding", return_value=b"\x00" * (128 * 4))
    @patch("ml_engine.face_recognition.create_face_embedding", return_value=b"\x00" * (128 * 4))
    def test_match_at_threshold_is_accepted(self, _create_embedding_mock, _decrypt_mock, load_models_mock):
        recognizer = SimpleNamespace(match=lambda _current, _reference, _mode: 0.65)
        load_models_mock.return_value = (None, recognizer)

        result = compare_face_embedding(Image.new("RGB", (10, 10)), b"encrypted")

        self.assertEqual(result, {"status": "matched", "similarity": 0.65})

    def test_cosine_similarity_compares_face_embedding_vectors(self):
        first = np.array([1.0, 0.0], dtype=np.float32).tobytes()
        same = np.array([2.0, 0.0], dtype=np.float32).tobytes()
        different = np.array([0.0, 1.0], dtype=np.float32).tobytes()

        self.assertEqual(cosine_similarity(first, same), 1.0)
        self.assertEqual(cosine_similarity(first, different), 0.0)


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