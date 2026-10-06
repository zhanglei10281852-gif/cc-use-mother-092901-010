"""滚动观测窗口与数据缺口测试。

覆盖：
- 窗口边界 [start, end) 的取舍；
- 窗口滑动后旧样本滑出、缺口重新出现；
- 不同指标按各自周期（window_days）取数；
- 聚合口径（窗口内均值）与证据字段；
- 数据补报消除缺口；
- 基础设施快照取评估时刻前最新一条。
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import T0, coverage_metric, incident_metric, make_service, takeover_metric
from readiness_assessment.domain import (
    InfrastructureCondition,
    ItemStatus,
    Observation,
)


class RollingWindowTests(unittest.TestCase):
    def _obs(self, metric, day_offset, value, region="Z-1", hour=0):
        return Observation(metric, region, T0 - timedelta(days=day_offset, hours=-hour), value)

    def test_window_boundaries_half_open(self):
        """窗口为 [as_of - N天, as_of)：恰在起点计入，恰在终点剔除。"""
        svc = make_service()
        start = T0 - timedelta(days=30)
        svc.add_observation(Observation("incident-rate", "Z-1", start, 0.01))       # 计入
        svc.add_observation(Observation("incident-rate", "Z-1", T0, 0.99))          # 剔除
        # 其余指标保持缺口，不影响本断言
        finding = svc.latest_candidate("fleet-a", "Z-1").finding("incident-rate")
        self.assertIs(finding.status, ItemStatus.PASS)
        self.assertEqual(finding.observed_value, 0.01)
        evidence = next(
            e for e in svc.latest_candidate("fleet-a", "Z-1").evidence
            if e.metric_code == "incident-rate"
        )
        self.assertEqual(evidence.sample_count, 1)
        self.assertEqual(evidence.window_starts_at, start)
        self.assertEqual(evidence.window_ends_at, T0)

    def test_samples_slide_out_and_gap_reappears(self):
        """30 天窗口滑动：31 天前的样本掉出窗口后，数据缺口重新暴露。"""
        svc = make_service()
        old = T0 - timedelta(days=31)
        svc.add_observation(Observation("takeover-rate", "Z-1", old, 0.001))
        finding = svc.latest_candidate("fleet-a", "Z-1").finding("takeover-rate")
        self.assertIs(finding.status, ItemStatus.DATA_GAP)
        self.assertIsNone(finding.observed_value)
        self.assertTrue(
            any(b.code == "takeover-rate" for b in svc.latest_candidate("fleet-a", "Z-1").blockers)
        )

        # 补一条窗口内样本 -> 缺口消除
        svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=2), 0.005))
        finding = svc.latest_candidate("fleet-a", "Z-1").finding("takeover-rate")
        self.assertIs(finding.status, ItemStatus.PASS)
        self.assertEqual(finding.observed_value, 0.005)

    def test_each_metric_uses_its_own_window(self):
        """事故率 30 天周期与接管率 7 天周期分别取数。"""
        metrics = frozenset({
            incident_metric(window_days=30),
            takeover_metric(window_days=7),
            coverage_metric(),
        })
        from factories import make_scheme
        svc = make_service(scheme=make_scheme(metrics=metrics))

        # 10 天前的样本：在 30 天窗口内，但在 7 天窗口外
        svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=10), 0.02))
        svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=10), 0.001))
        svc.report_infrastructure(
            InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0 - timedelta(days=1))
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertIs(candidate.finding("incident-rate").status, ItemStatus.PASS)
        self.assertIs(candidate.finding("takeover-rate").status, ItemStatus.DATA_GAP)
        ev_takeover = next(e for e in candidate.evidence if e.metric_code == "takeover-rate")
        self.assertEqual(ev_takeover.window_days, 7)

    def test_aggregation_is_window_mean(self):
        svc = make_service()
        for i, value in enumerate([0.01, 0.02, 0.03]):
            svc.add_observation(
                Observation("incident-rate", "Z-1", T0 - timedelta(days=i + 1), value)
            )
        finding = svc.latest_candidate("fleet-a", "Z-1").finding("incident-rate")
        self.assertAlmostEqual(finding.observed_value, 0.02)

    def test_infrastructure_uses_latest_snapshot_before_as_of(self):
        svc = make_service()
        svc.report_infrastructure(
            InfrastructureCondition("rsu-coverage", "Z-1", 0.70, 0.9, T0 - timedelta(days=10))
        )
        self.assertIs(
            svc.latest_candidate("fleet-a", "Z-1").finding("rsu-coverage").status,
            ItemStatus.FAIL,
        )
        # 新快照达标
        svc.report_infrastructure(
            InfrastructureCondition("rsu-coverage", "Z-1", 0.97, 0.9, T0 - timedelta(days=1))
        )
        finding = svc.latest_candidate("fleet-a", "Z-1").finding("rsu-coverage")
        self.assertIs(finding.status, ItemStatus.PASS)
        self.assertAlmostEqual(finding.observed_value, 0.97)

    def test_infrastructure_without_snapshot_is_data_gap(self):
        svc = make_service()
        self.assertIs(
            svc.latest_candidate("fleet-a", "Z-1").finding("rsu-coverage").status,
            ItemStatus.DATA_GAP,
        )

    def test_every_data_update_triggers_recompute_history(self):
        svc = make_service()
        svc.add_observation(Observation("incident-rate", "Z-1", T0 - timedelta(days=1), 0.01))
        history = svc.candidate_history("fleet-a", "Z-1")
        reasons = [record.reason for record in history]
        self.assertEqual(reasons[0], "application-registered")
        self.assertIn("observation-updated", reasons)


if __name__ == "__main__":
    unittest.main()
