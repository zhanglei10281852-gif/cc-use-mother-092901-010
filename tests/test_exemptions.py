"""期限性豁免测试。

覆盖：
- 豁免必须限定能力范围与期限（构造期校验）；
- 有效期内指标记为 EXEMPTED 且不构成阻塞，例外中展示到期时间；
- 到期后时钟推进自动暴露受影响能力（重新 FAIL/缺口阻塞）；
- 豁免不可覆盖重大安全事件；
- 未限定能力的"整份豁免"被禁止，防止临时豁免变永久达标。
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import CAP_URBAN, T0, make_service
from readiness_assessment.domain import (
    Exemption,
    InfrastructureCondition,
    ItemStatus,
    Observation,
    SafetyEvent,
    Severity,
    VehicleCapability,
)
from readiness_assessment.errors import AdmissionDomainError


def _ready_data(svc):
    for day in range(10):
        svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=day), 0.02))
        svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=day), 0.005))
    svc.report_infrastructure(
        InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0 - timedelta(days=1))
    )


class ExemptionTests(unittest.TestCase):
    def test_exemption_requires_capability_scope(self):
        with self.assertRaises(AdmissionDomainError):
            Exemption(
                "EX-BAD", "incident-rate", frozenset(), "Z-1",
                T0 - timedelta(days=1), T0 + timedelta(days=7),
            )

    def test_exemption_requires_valid_period(self):
        with self.assertRaises(AdmissionDomainError):
            Exemption(
                "EX-BAD", "incident-rate", CAP_URBAN, "Z-1",
                T0 + timedelta(days=7), T0,
            )

    def test_active_exemption_masks_failure_with_exception_record(self):
        svc = make_service()
        _ready_data(svc)
        # 接管率冲到阈值之上
        for _ in range(5):
            svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(hours=2), 0.08))
        self.assertFalse(svc.latest_candidate("fleet-a", "Z-1").ready)

        svc.grant_exemption(
            Exemption("EX-1", "takeover-rate", CAP_URBAN, "Z-1",
                      T0 - timedelta(days=1), T0 + timedelta(days=7))
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertTrue(candidate.ready)
        self.assertIs(candidate.finding("takeover-rate").status, ItemStatus.EXEMPTED)
        self.assertEqual(candidate.finding("takeover-rate").exemption_id, "EX-1")
        exemption_exc = next(e for e in candidate.exceptions if e.kind == "exemption")
        self.assertTrue(exemption_exc.active)
        self.assertIn("takeover-rate", exemption_exc.detail)
        # 证据仍保留真实观测值，豁免不抹除证据
        evidence = next(e for e in candidate.evidence if e.metric_code == "takeover-rate")
        self.assertIsNotNone(evidence.observed_value)

    def test_expired_exemption_exposes_capability(self):
        svc = make_service()
        _ready_data(svc)
        for _ in range(5):
            svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(hours=2), 0.08))
        svc.grant_exemption(
            Exemption("EX-1", "takeover-rate", CAP_URBAN, "Z-1",
                      T0 - timedelta(days=1), T0 + timedelta(days=7))
        )
        self.assertTrue(svc.latest_candidate("fleet-a", "Z-1").ready)

        # 时钟越过到期时刻
        result = svc.sweep_time_advances(T0 + timedelta(days=8))
        self.assertEqual(result[("fleet-a", "Z-1")], "exemption-expiry")
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertFalse(candidate.ready)
        self.assertIs(candidate.finding("takeover-rate").status, ItemStatus.FAIL)
        record = next(
            e for e in candidate.exceptions
            if e.kind == "exemption" and e.reference == "EX-1"
        )
        self.assertFalse(record.active)
        self.assertTrue(
            any(b.code == "takeover-rate" for b in candidate.blockers)
        )

    def test_exemption_does_not_cover_major_event(self):
        svc = make_service()
        _ready_data(svc)
        svc.grant_exemption(
            Exemption("EX-2", "incident-rate", CAP_URBAN, "Z-1",
                      T0 - timedelta(days=1), T0 + timedelta(days=30))
        )
        svc.add_event(
            SafetyEvent("EV-C", "Z-1", T0 - timedelta(hours=1), Severity.CRITICAL,
                        VehicleCapability.AUTONOMOUS_URBAN, "路口追尾")
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertFalse(candidate.ready)
        major = [e for e in candidate.exceptions if e.kind == "major-event"]
        self.assertEqual(len(major), 1)
        self.assertIn("不可豁免", major[0].detail)
        self.assertTrue(any(b.code == "major-event" for b in candidate.blockers))

    def test_not_yet_valid_exemption_does_not_apply(self):
        svc = make_service()
        _ready_data(svc)
        for _ in range(3):
            svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(hours=2), 0.08))
        svc.grant_exemption(
            Exemption("EX-FUTURE", "takeover-rate", CAP_URBAN, "Z-1",
                      T0 + timedelta(days=1), T0 + timedelta(days=9))
        )
        # 豁免尚未生效，失败仍暴露
        self.assertFalse(svc.latest_candidate("fleet-a", "Z-1").ready)


if __name__ == "__main__":
    unittest.main()
