from django.core.management.base import BaseCommand

from ml_engine.predictors import Model1CompletionPredictor, Model2RiskPredictor, Model3MatchPredictor, Model4PerformancePredictor


class Command(BaseCommand):
    help = "Load available ML artifacts and exercise deterministic fallback predictors."

    def handle(self, *args, **options):
        checks = (
            ("completion", Model1CompletionPredictor(), lambda model: model.predict(0.9, 0.8, 4.0)),
            ("risk", Model2RiskPredictor(), lambda model: model.predict(1, 0, 0.9)),
            ("matching", Model3MatchPredictor(), lambda model: model.predict(0.8, 0.7, 1.0)),
            ("performance", Model4PerformancePredictor(), lambda model: model.predict(4, 4, 4, 4)),
        )
        for label, model, sample in checks:
            value = sample(model)
            mode = "fallback" if model.using_fallback else "artifact"
            self.stdout.write(f"{label}: {mode} ({value})")