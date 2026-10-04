import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import support
from readiness_assessment.decisions import Committee
from readiness_assessment.plan import (
    Aggregation,
    AssessmentPlan,
    EventSeverity,
    InfrastructureCondition,
    MetricDefinition,
    ObservationWindow,
    OperationRegion,
    RectificationCommitment,
    SeverityPolicy,
    ThresholdDirection,
    VehicleCapability,
)
from readiness_assessment.service import AssessmentService


class ObservationWindowTests(unittest.TestCase):
    def test_invalid_window_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            ObservationWindow("w", timedelta(0), timedelta(days=1))
        with self.assertRaises(ValueError):
            ObservationWindow("w", timedelta(days=7), timedelta(0))
        with self.assertRaises(ValueError):
            ObservationWindow("w", timedelta(days=7), timedelta(days=1), min_coverage=0)
        with self.assertRaises(ValueError):
            ObservationWindow("w", timedelta(days=7), timedelta(days=1), min_coverage=1.5)

    def test_required_updates_uses_coverage_ratio(self):
        window = ObservationWindow("w", timedelta(days=7), timedelta(days=1), min_coverage=0.8)
        self.assertEqual(window.expected_updates(), 7)
        self.assertEqual(window.required_updates(), 6)

    def test_span_is_rolling(self):
        span = support.WINDOW.span(support.BASE)
        self.assertEqual(span.ends_at, support.BASE)
        self.assertEqual(span.starts_at, support.BASE - 7 * support.DAY)


class MetricDefinitionTests(unittest.TestCase):
    def test_upper_bound_is_inclusive(self):
        metric = MetricDefinition("m", "u", ThresholdDirection.UPPER_BOUND, 0.01, "ops-7d")
        self.assertTrue(metric.satisfied_by(0.01))
        self.assertFalse(metric.satisfied_by(0.011))

    def test_lower_bound_is_inclusive(self):
        metric = MetricDefinition("m", "u", ThresholdDirection.LOWER_BOUND, 0.99, "ops-7d", Aggregation.LATEST)
        self.assertTrue(metric.satisfied_by(0.99))
        self.assertFalse(metric.satisfied_by(0.989))


class PlanIntegrityTests(unittest.TestCase):
    def test_region_threshold_override_and_fallback(self):
        regions = support.make_regions()
        downtown, suburbs = regions
        metric = support.make_metrics()[0]
        self.assertEqual(downtown.threshold_for(metric), 0.004)
        self.assertEqual(suburbs.threshold_for(metric), 0.01)

    def test_version_must_be_positive(self):
        with self.assertRaises(ValueError):
            support.make_plan(version=0)

    def test_unknown_window_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            AssessmentPlan(
                plan_id="p",
                version=1,
                effective_from=support.BASE,
                regions=support.make_regions(),
                capabilities=support.make_capability(),
                metrics=(MetricDefinition("m", "u", ThresholdDirection.UPPER_BOUND, 1.0, "missing-window"),),
                windows=(support.WINDOW,),
                severity_policy=SeverityPolicy({EventSeverity.CRITICAL}, lookback=timedelta(days=30)),
            )

    def test_unknown_metric_in_capability_is_rejected(self):
        with self.assertRaises(ValueError):
            AssessmentPlan(
                plan_id="p",
                version=1,
                effective_from=support.BASE,
                regions=support.make_regions(),
                capabilities=(VehicleCapability("cap", "能力", ("ghost-metric",)),),
                metrics=support.make_metrics(),
                windows=(support.WINDOW,),
                severity_policy=SeverityPolicy({EventSeverity.CRITICAL}, lookback=timedelta(days=30)),
            )

    def test_unknown_region_in_rectification_and_infrastructure_is_rejected(self):
        base = dict(
            plan_id="p",
            version=1,
            effective_from=support.BASE,
            regions=support.make_regions(),
            capabilities=support.make_capability(),
            metrics=support.make_metrics(),
            windows=(support.WINDOW,),
            severity_policy=SeverityPolicy({EventSeverity.CRITICAL}, lookback=timedelta(days=30)),
        )
        with self.assertRaises(ValueError):
            AssessmentPlan(
                rectifications=(RectificationCommitment("r1", "nowhere", "robotaxi-l4", "整改", support.BASE),),
                **base,
            )
        with self.assertRaises(ValueError):
            AssessmentPlan(
                infrastructure=(InfrastructureCondition("rsu", "nowhere", 0.9, timedelta(days=3)),),
                **base,
            )

    def test_duplicate_codes_are_rejected(self):
        with self.assertRaises(ValueError):
            AssessmentPlan(
                plan_id="p",
                version=1,
                effective_from=support.BASE,
                regions=(OperationRegion("r", "一"), OperationRegion("r", "二")),
                capabilities=support.make_capability(),
                metrics=support.make_metrics(),
                windows=(support.WINDOW,),
                severity_policy=SeverityPolicy({EventSeverity.CRITICAL}, lookback=timedelta(days=30)),
            )

    def test_override_of_unknown_metric_is_rejected(self):
        with self.assertRaises(ValueError):
            AssessmentPlan(
                plan_id="p",
                version=1,
                effective_from=support.BASE,
                regions=(OperationRegion("r", "区域", {"ghost-metric": 1.0}),),
                capabilities=support.make_capability(),
                metrics=support.make_metrics(),
                windows=(support.WINDOW,),
                severity_policy=SeverityPolicy({EventSeverity.CRITICAL}, lookback=timedelta(days=30)),
            )


class PlanVersionTests(unittest.TestCase):
    def test_versions_must_increase(self):
        service = AssessmentService(Committee(("alice",), 1))
        service.register_plan(support.make_plan(version=1))
        with self.assertRaises(ValueError):
            service.register_plan(support.make_plan(version=1))
        service.register_plan(support.make_plan(version=3, takeover_threshold=0.008))
        self.assertEqual(service.plan("city-av").version, 3)
        self.assertEqual(service.plan("city-av", 1).version, 1)

    def test_capability_requires_metrics(self):
        with self.assertRaises(ValueError):
            VehicleCapability("cap", "能力", ())


if __name__ == "__main__":
    unittest.main()
