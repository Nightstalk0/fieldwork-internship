from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from PIL import Image

from .face_detector import detect_faces
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