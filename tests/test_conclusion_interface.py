"""结论接口展示测试：证据、例外、下阶段阻塞原因的结构化视图。"""

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
    Observation,
)


class ConclusionInterfaceTests(unittest.TestCase):
    def test_ready_conclusion_dict_exposes_findings_evidence(self):
        svc = make_service()
        for day in range(1, 11):
            svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=day), 0.02))
            svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=day), 0.005))
        svc.report_infrastructure(
            InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0 - timedelta(days=1))
        )
        payload = svc.latest_candidate("fleet-a", "Z-1").to_dict()

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["region_code"], "Z-1")
        self.assertEqual(payload["scheme_version"], 1)
        codes = {f["metric_code"] for f in payload["findings"]}
        self.assertEqual(codes, {"incident-rate", "takeover-rate", "rsu-coverage"})
        for finding in payload["findings"]:
            self.assertIn(finding["status"], {"pass", "exempted", "fail", "data-gap"})

        evidence = next(e for e in payload["evidence"] if e["metric_code"] == "incident-rate")
        self.assertEqual(evidence["sample_count"], 10)
        self.assertEqual(evidence["window_days"], 30)
        self.assertAlmostEqual(evidence["observed_value"], 0.02)
        self.assertLess(evidence["window_starts_at"], evidence["window_ends_at"])

    def test_blocked_conclusion_dict_exposes_blocker_reasons(self):
        svc = make_service()
        svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=1), 0.40))
        payload = svc.latest_candidate("fleet-a", "Z-1").to_dict()
        self.assertFalse(payload["ready"])
        codes = {b["code"] for b in payload["blockers"]}
        # 事故率超标 + 接管率/设施数据缺口
        self.assertIn("incident-rate", codes)
        self.assertIn("takeover-rate", codes)
        self.assertIn("rsu-coverage", codes)
        blocker = next(b for b in payload["blockers"] if b["code"] == "incident-rate")
        self.assertIn("阈值", blocker["reason"])
        self.assertEqual(blocker["capability"], "autonomous-urban")

    def test_exception_record_is_visible_in_payload(self):
        svc = make_service()
        for day in range(10):
            svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=day), 0.02))
            svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=day), 0.08))
        svc.report_infrastructure(
            InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0 - timedelta(days=1))
        )
        svc.grant_exemption(
            Exemption("EX-7", "takeover-rate", CAP_URBAN, "Z-1",
                      T0 - timedelta(days=1), T0 + timedelta(days=5))
        )
        payload = svc.latest_candidate("fleet-a", "Z-1").to_dict()
        self.assertTrue(payload["ready"])
        exc = next(e for e in payload["exceptions"] if e["reference"] == "EX-7")
        self.assertEqual(exc["kind"], "exemption")
        self.assertTrue(exc["active"])
        self.assertIn("takeover-rate", exc["detail"])
        self.assertEqual(
            next(f for f in payload["findings"] if f["metric_code"] == "takeover-rate")["status"],
            "exempted",
        )


if __name__ == "__main__":
    unittest.main()
