from django.test import SimpleTestCase

from .predictors import Model1CompletionPredictor, Model2RiskPredictor, Model3MatchPredictor, Model4PerformancePredictor


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