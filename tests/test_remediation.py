"""整改承诺与重大事件阻塞测试。"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import T0, make_scheme, make_service
from readiness_assessment.domain import (
    InfrastructureCondition,
    Observation,
    RemediationCommitment,
    SafetyEvent,
    Severity,
    VehicleCapability,
)


def _ready_data(svc):
    for day in range(10):
        svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=day), 0.02))
        svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=day), 0.005))
    svc.report_infrastructure(
        InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0 - timedelta(days=1))
    )


class RemediationTests(unittest.TestCase):
    def test_open_commitment_blocks_next_stage(self):
        svc = make_service()
        _ready_data(svc)
        svc.record_commitment(
            RemediationCommitment(
                "C-1", VehicleCapability.AUTONOMOUS_URBAN,
                due_at=T0 + timedelta(days=14),
            ),
            "fleet-a", "Z-1",
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertFalse(candidate.ready)
        blocker = next(b for b in candidate.blockers if b.code == "remediation-open")
        self.assertEqual(blocker.capability, "autonomous-urban")

    def test_closed_commitment_clears_blocker(self):
        svc = make_service()
        _ready_data(svc)
        svc.record_commitment(
            RemediationCommitment(
                "C-1", VehicleCapability.AUTONOMOUS_URBAN,
                due_at=T0 + timedelta(days=14),
            ),
            "fleet-a", "Z-1",
        )
        self.assertFalse(svc.latest_candidate("fleet-a", "Z-1").ready)
        svc.close_commitment("fleet-a", "Z-1", "C-1", closed_at=T0 - timedelta(days=1))
        self.assertTrue(svc.latest_candidate("fleet-a", "Z-1").ready)

    def test_overdue_commitment_blocked_with_grace_period(self):
        # 宽限期 3 天：承诺到期后 3 天内仍计为 open 而非 overdue
        svc = make_service(scheme=make_scheme(grace_days=3))
        _ready_data(svc)
        svc.record_commitment(
            RemediationCommitment(
                "C-1", VehicleCapability.AUTONOMOUS_URBAN,
                due_at=T0 - timedelta(days=2),  # 到期 2 天，在宽限期内
            ),
            "fleet-a", "Z-1",
        )
        codes = {b.code for b in svc.latest_candidate("fleet-a", "Z-1").blockers}
        self.assertIn("remediation-open", codes)
        self.assertNotIn("remediation-overdue", codes)

        svc.record_commitment(
            RemediationCommitment(
                "C-1", VehicleCapability.AUTONOMOUS_URBAN,
                due_at=T0 - timedelta(days=10),  # 超出宽限期
            ),
            "fleet-a", "Z-1",
        )
        codes = {b.code for b in svc.latest_candidate("fleet-a", "Z-1").blockers}
        self.assertIn("remediation-overdue", codes)

    def test_high_event_is_major_medium_is_not(self):
        svc = make_service()
        _ready_data(svc)
        svc.add_event(
            SafetyEvent("EV-M", "Z-1", T0 - timedelta(days=1), Severity.MEDIUM,
                        VehicleCapability.AUTONOMOUS_URBAN)
        )
        self.assertTrue(svc.latest_candidate("fleet-a", "Z-1").ready)
        self.assertFalse(
            any(e.kind == "major-event"
                for e in svc.latest_candidate("fleet-a", "Z-1").exceptions)
        )

        svc.add_event(
            SafetyEvent("EV-H", "Z-1", T0 - timedelta(hours=1), Severity.HIGH,
                        VehicleCapability.AUTONOMOUS_URBAN)
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertFalse(candidate.ready)
        self.assertEqual(
            [e.reference for e in candidate.exceptions if e.kind == "major-event"],
            ["EV-H"],
        )

    def test_major_event_threshold_is_versioned(self):
        """方案 v2 把重大事件门槛提到 CRITICAL 后，HIGH 事件不再阻塞。"""
        svc = make_service()
        _ready_data(svc)
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 - timedelta(days=1),
                        major_severity=Severity.CRITICAL)
        )
        svc.add_event(
            SafetyEvent("EV-H", "Z-1", T0 - timedelta(hours=1), Severity.HIGH,
                        VehicleCapability.AUTONOMOUS_URBAN)
        )
        self.assertTrue(svc.latest_candidate("fleet-a", "Z-1").ready)


if __name__ == "__main__":
    unittest.main()
