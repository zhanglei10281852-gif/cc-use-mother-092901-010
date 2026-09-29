import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from readiness_assessment.contracts import AssessmentMetric, DecisionStage, MetricWindow


class ReadinessContractTests(unittest.TestCase):
    def test_metric_keeps_region_and_window(self):
        window = MetricWindow(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 20, tzinfo=timezone.utc))
        metric = AssessmentMetric("incident-rate", "Z-1", window, 0.02, DecisionStage.REVIEW)
        self.assertEqual((metric.region_code, metric.stage.value), ("Z-1", "review"))

    def test_invalid_window_is_rejected(self):
        instant = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            MetricWindow(instant, instant)


if __name__ == "__main__":
    unittest.main()
