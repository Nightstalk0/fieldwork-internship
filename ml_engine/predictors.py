import os
from pathlib import Path

import joblib


MODEL_DIR = Path(__file__).resolve().parent / "models"


class RuleBasedPredictor:
    model_number = 0

    def __init__(self):
        self.model = None
        model_path = MODEL_DIR / f"model_{self.model_number}.joblib"
        if model_path.exists():
            try:
                self.model = joblib.load(model_path)
            except (OSError, ValueError, ImportError):
                self.model = None

    @property
    def using_fallback(self):
        return self.model is None

    def _model_value(self, features):
        if self.model is None:
            return None
        try:
            if hasattr(self.model, "predict_proba"):
                return float(self.model.predict_proba([features])[0][-1])
            return float(self.model.predict([features])[0])
        except (AttributeError, TypeError, ValueError, IndexError):
            return None


class Model1CompletionPredictor(RuleBasedPredictor):
    model_number = 1

    def predict(self, attendance_rate, report_rate, score_average=3.0):
        features = [float(attendance_rate), float(report_rate), float(score_average)]
        result = self._model_value(features)
        if result is None:
            result = 0.5 * features[0] + 0.3 * features[1] + 0.2 * (features[2] / 5)
        return round(max(0.0, min(1.0, result)), 4)


class Model2RiskPredictor(RuleBasedPredictor):
    model_number = 2

    def predict(self, missed_days, overdue_reports, attendance_rate):
        features = [float(missed_days), float(overdue_reports), float(attendance_rate)]
        result = self._model_value(features)
        if result is None:
            result = min(1.0, max(0.0, features[0] * 0.06 + features[1] * 0.12 + (1 - features[2]) * 0.65))
        return round(result, 4)


class Model3MatchPredictor(RuleBasedPredictor):
    model_number = 3

    def predict(self, skill_overlap, interest_match, schedule_match):
        features = [float(skill_overlap), float(interest_match), float(schedule_match)]
        result = self._model_value(features)
        if result is None:
            result = 0.5 * features[0] + 0.3 * features[1] + 0.2 * features[2]
        return round(max(0.0, min(1.0, result)), 4)


class Model4PerformancePredictor(RuleBasedPredictor):
    model_number = 4

    def predict(self, technical, communication, initiative, reliability):
        features = [float(technical), float(communication), float(initiative), float(reliability)]
        result = self._model_value(features)
        if result is None:
            result = sum(features) / (len(features) * 5)
        return round(max(0.0, min(1.0, result)), 4)


def prediction_summary():
    predictors = (Model1CompletionPredictor(), Model2RiskPredictor(), Model3MatchPredictor(), Model4PerformancePredictor())
    return {f"model_{predictor.model_number}": "fallback" if predictor.using_fallback else "artifact" for predictor in predictors}