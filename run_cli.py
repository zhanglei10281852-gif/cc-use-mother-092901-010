import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from readiness_assessment.contracts import AssessmentMetric, DecisionStage, MetricWindow


window = MetricWindow(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 29, tzinfo=timezone.utc))
metric = AssessmentMetric("takeover-rate", "zone-a", window, 0.003, DecisionStage.REVIEW)
print(json.dumps({"metric": metric.metric_code, "region": metric.region_code, "stage": metric.stage.value}, ensure_ascii=False))
