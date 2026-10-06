"""规则换版（方案版本化）测试。

覆盖：
- 已发布方案不可变，重复版本号冲突；
- 阈值变更通过发布新版本生效并触发重算；
- 已经签发的阶段决定保留旧版本快照；
- 新提案自动引用当前生效版本；
- 按时刻解析版本（未来版本不提前生效）与版本间阈值 diff。
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import (
    T0,
    incident_metric,
    make_service,
    make_scheme,
    standard_metrics,
    takeover_metric,
    coverage_metric,
)
from readiness_assessment.domain import (
    InfrastructureCondition,
    ItemStatus,
    Observation,
)
from readiness_assessment.errors import SchemeConflictError, UnknownSchemeError


def _feed_passing_data(svc, region="Z-1"):
    for day in range(10):
        svc.add_observation(Observation("incident-rate", region, T0 - timedelta(days=day), 0.02))
        svc.add_observation(Observation("takeover-rate", region, T0 - timedelta(days=day), 0.005))
    svc.report_infrastructure(
        InfrastructureCondition("rsu-coverage", region, 0.95, 0.9, T0 - timedelta(days=1))
    )


class RuleVersioningTests(unittest.TestCase):
    def test_published_version_is_immutable(self):
        svc = make_service()
        with self.assertRaises(SchemeConflictError):
            svc.publish_scheme(make_scheme(version=1))

    def test_unknown_version_lookup(self):
        svc = make_service()
        with self.assertRaises(UnknownSchemeError):
            svc.registry.get("commercial-admission", 99)

    def test_threshold_tightening_via_new_version_recomputes(self):
        svc = make_service()
        _feed_passing_data(svc)
        self.assertTrue(svc.latest_candidate("fleet-a", "Z-1").ready)

        # v2：事故率默认阈值 0.05 -> 0.02，当前观测均值 0.02 仍达标
        tightened = incident_metric(threshold=0.02)
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 - timedelta(days=2),
                        metrics=frozenset({tightened, takeover_metric(), coverage_metric()}))
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertEqual(candidate.scheme_version, 2)
        self.assertTrue(candidate.ready)

        # v3：继续收紧到 0.01 -> 不达标，产生阻塞
        tightened_more = incident_metric(threshold=0.01)
        svc.publish_scheme(
            make_scheme(version=3, published_at=T0 - timedelta(days=1),
                        metrics=frozenset({tightened_more, takeover_metric(), coverage_metric()}))
        )
        candidate = svc.latest_candidate("fleet-a", "Z-1")
        self.assertEqual(candidate.scheme_version, 3)
        self.assertFalse(candidate.ready)
        self.assertIs(candidate.finding("incident-rate").status, ItemStatus.FAIL)
        self.assertEqual(candidate.finding("incident-rate").threshold, 0.01)

    def test_issued_decision_keeps_old_scheme_snapshot(self):
        svc = make_service()
        _feed_passing_data(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        svc.cosign("D-1", "m1")
        issued = svc.cosign("D-1", "m2")
        self.assertTrue(issued.is_issued)

        # 换版后候选结论翻转
        tightened = incident_metric(threshold=0.001)
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 - timedelta(days=1),
                        metrics=frozenset({tightened, takeover_metric(), coverage_metric()}))
        )
        self.assertFalse(svc.latest_candidate("fleet-a", "Z-1").ready)

        # 已签发决定不动：版本号、ready 快照、证据均停留在签发时刻
        view = svc.get_decision("D-1")
        self.assertEqual(view.conclusion["scheme_version"], 1)
        self.assertTrue(view.conclusion["ready"])
        self.assertEqual(view.stage.value, "approved")

    def test_new_proposal_uses_current_version(self):
        svc = make_service()
        _feed_passing_data(svc)
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 - timedelta(days=1), metrics=standard_metrics())
        )
        view = svc.propose("D-2", "fleet-a", "Z-1", "chair")
        self.assertEqual(view.scheme_version, 2)

    def test_future_version_does_not_apply_yet(self):
        svc = make_service()
        _feed_passing_data(svc)
        tightened = incident_metric(threshold=0.001)
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 + timedelta(days=5),
                        metrics=frozenset({tightened, takeover_metric(), coverage_metric()}))
        )
        # 时钟仍在 T0，v2 尚未生效
        self.assertEqual(svc.latest_candidate("fleet-a", "Z-1").scheme_version, 1)
        # 推进到发布之后，v2 生效并制造阻塞
        svc.sweep_time_advances(T0 + timedelta(days=6))
        self.assertEqual(svc.latest_candidate("fleet-a", "Z-1").scheme_version, 2)
        self.assertFalse(svc.latest_candidate("fleet-a", "Z-1").ready)

    def test_version_diff_reports_changed_thresholds(self):
        svc = make_service()
        svc.publish_scheme(
            make_scheme(version=2, published_at=T0 - timedelta(days=1),
                        metrics=frozenset({incident_metric(0.03), takeover_metric(), coverage_metric()}))
        )
        changes = svc.registry.changed_between("commercial-admission", 1, 2)
        self.assertEqual(changes, {"incident-rate": (0.05, 0.03)})


if __name__ == "__main__":
    unittest.main()
