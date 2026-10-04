import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import support
from readiness_assessment.evaluation import ConclusionOutcome
from readiness_assessment.exemptions import Exemption, ExemptionScopeError
from readiness_assessment.observations import MetricObservation, SafetyEvent
from readiness_assessment.plan import EventSeverity


class RollingWindowTests(unittest.TestCase):
    """滚动窗口：旧观测随窗口前移滑出，聚合结果与结论随之改变。"""

    def test_spike_rolls_out_of_window(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        # 窗口起点处的一次接管率尖峰，其余时间表现良好
        service.record_observation(MetricObservation("takeover-rate", region, capability, support.BASE - 6 * support.DAY, 0.5))
        for offset in range(5, -1, -1):
            at = support.BASE - offset * support.DAY
            service.record_observation(MetricObservation("takeover-rate", region, capability, at, 0.001))
        support.record_daily(service, region, capability, support.BASE + support.DAY, 1, takeover=0.001)
        for offset in range(6, -1, -1):
            at = support.BASE - offset * support.DAY
            service.record_observation(MetricObservation("incident-rate", region, capability, at, 0.001))
            service.record_observation(MetricObservation("system-availability", region, capability, at, 0.999))
        service.record_observation(MetricObservation("incident-rate", region, capability, support.BASE + support.DAY, 0.001))
        service.record_observation(MetricObservation("system-availability", region, capability, support.BASE + support.DAY, 0.999))
        support.record_infra(service, region, 0.85, support.BASE)

        during_spike = service.evaluate("city-av", region, capability, support.BASE)
        self.assertEqual(during_spike.outcome, ConclusionOutcome.BLOCKED)
        self.assertIn("takeover-rate", {b.subject for b in during_spike.blockers})

        # 窗口前移一天后尖峰滑出 (BASE-6d, BASE+1d]，结论转为就绪
        after_spike = service.evaluate("city-av", region, capability, support.BASE + support.DAY)
        self.assertEqual(after_spike.outcome, ConclusionOutcome.READY)
        self.assertEqual(after_spike.blockers, ())

    def test_data_gap_when_feed_stops(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7)
        support.record_infra(service, region, 0.85, support.BASE)

        fresh = service.evaluate("city-av", region, capability, support.BASE)
        self.assertEqual(fresh.outcome, ConclusionOutcome.READY)

        # 数据源停更：5 天后重算，观测次数不足且超过更新周期
        stale = service.refresh_all(support.BASE + 5 * support.DAY)
        suburbs = next(c for c in stale if c.region_code == region)
        self.assertEqual(suburbs.outcome, ConclusionOutcome.INSUFFICIENT_DATA)
        self.assertTrue(any(b.code == "data_gap" for b in suburbs.blockers))
        self.assertTrue(any(e.kind == "data_gap" for e in suburbs.exceptions))


class CrossRegionTests(unittest.TestCase):
    """跨区域差异：同一观测值在不同区域阈值下得出不同结论。"""

    def test_region_override_changes_outcome(self):
        service = support.make_service()
        for region in ("downtown", "suburbs"):
            support.record_daily(service, region, "robotaxi-l4", support.BASE, 7, takeover=0.007)
        support.record_infra(service, "downtown", 0.95, support.BASE)
        support.record_infra(service, "suburbs", 0.85, support.BASE)

        suburbs = service.evaluate("city-av", "suburbs", "robotaxi-l4", support.BASE)
        downtown = service.evaluate("city-av", "downtown", "robotaxi-l4", support.BASE)

        self.assertEqual(suburbs.outcome, ConclusionOutcome.READY)  # 0.007 <= 默认阈值 0.01
        self.assertEqual(downtown.outcome, ConclusionOutcome.BLOCKED)  # 0.007 > 城区覆盖阈值 0.004
        self.assertEqual(
            {b.subject for b in downtown.blockers if b.code == "metric_threshold"},
            {"takeover-rate"},
        )


class RuleVersionChangeTests(unittest.TestCase):
    """规则换版：新版本触发重算，旧版本结论保持可追溯。"""

    def test_threshold_tightening_recomputes_candidates(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7, takeover=0.007)
        support.record_infra(service, region, 0.85, support.BASE)

        v1_conclusion = service.evaluate("city-av", region, capability, support.BASE)
        self.assertEqual((v1_conclusion.plan_version, v1_conclusion.outcome), (1, ConclusionOutcome.READY))

        # 换版：接管率阈值从 0.01 收紧到 0.005，登记即触发重算
        plan_v2 = support.make_plan(version=2, takeover_threshold=0.005)
        recomputed = service.register_plan(plan_v2, as_of=support.BASE + support.DAY)
        suburbs_v2 = next(c for c in recomputed if c.region_code == region)
        self.assertEqual((suburbs_v2.plan_version, suburbs_v2.outcome), (2, ConclusionOutcome.BLOCKED))
        self.assertIn("metric_threshold", {b.code for b in suburbs_v2.blockers})

        # 历史结论不被改写，旧版本仍可复算
        history = service.conclusion_history(region, capability)
        v1_history = [c for c in history if c.plan_version == 1]
        self.assertEqual(v1_history[-1].outcome, ConclusionOutcome.READY)
        replay = service.evaluate("city-av", region, capability, support.BASE, plan_version=1)
        self.assertEqual(replay.outcome, ConclusionOutcome.READY)


class SafetyEventTests(unittest.TestCase):
    """重大事件触发重算并阻断；轻微事件不触发。"""

    def test_critical_event_recomputes_and_blocks(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7)
        support.record_infra(service, region, 0.85, support.BASE)
        self.assertEqual(
            service.evaluate("city-av", region, capability, support.BASE).outcome,
            ConclusionOutcome.READY,
        )

        event = SafetyEvent("E-1", region, capability, support.BASE + support.HOUR, EventSeverity.CRITICAL, "碰撞事故")
        recomputed = service.record_event(event)
        self.assertEqual(len(recomputed), 1)
        conclusion = recomputed[0]
        self.assertEqual(conclusion.outcome, ConclusionOutcome.BLOCKED)
        self.assertIn("critical_event", {b.code for b in conclusion.blockers})
        self.assertIn("E-1", {e.subject for e in conclusion.evidence})

    def test_minor_event_does_not_trigger_recompute(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7)
        before = len(service.conclusion_history(region, capability))
        recomputed = service.record_event(
            SafetyEvent("E-2", region, capability, support.BASE, EventSeverity.MINOR, "轻微剐蹭")
        )
        self.assertEqual(recomputed, [])
        self.assertEqual(len(service.conclusion_history(region, capability)), before)


class RectificationTests(unittest.TestCase):
    """整改承诺：逾期阻断，履约后解除。"""

    def test_overdue_commitment_blocks_until_fulfilled(self):
        plan = support.make_plan(rectification_due=support.BASE + support.DAY)
        service = support.make_service(plan=plan)
        region, capability = "downtown", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE + 2 * support.DAY, 7, takeover=0.001)
        support.record_infra(service, region, 0.95, support.BASE + support.DAY)

        overdue = service.evaluate("city-av", region, capability, support.BASE + 2 * support.DAY)
        self.assertEqual(overdue.outcome, ConclusionOutcome.BLOCKED)
        self.assertIn("rectification_overdue", {b.code for b in overdue.blockers})

        service.fulfill_rectification("fix-latency", support.BASE + 2 * support.DAY + support.HOUR)
        fulfilled = service.evaluate("city-av", region, capability, support.BASE + 2 * support.DAY + support.HOUR)
        self.assertNotIn("rectification_overdue", {b.code for b in fulfilled.blockers})
        self.assertEqual(fulfilled.outcome, ConclusionOutcome.READY)

    def test_open_commitment_is_evidence_not_blocker(self):
        service = support.make_service()  # 截止 BASE + 10 天
        region, capability = "downtown", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7, takeover=0.001)
        support.record_infra(service, region, 0.95, support.BASE)

        conclusion = service.evaluate("city-av", region, capability, support.BASE)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.READY)
        self.assertIn("fix-latency", {e.subject for e in conclusion.evidence if e.kind == "rectification"})

    def test_unknown_commitment_is_rejected(self):
        service = support.make_service()
        with self.assertRaises(KeyError):
            service.fulfill_rectification("ghost", support.BASE)


class InfrastructureTests(unittest.TestCase):
    """基础设施：覆盖率不足阻断，读数超期形成数据缺口。"""

    def test_shortfall_blocks(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE, 7)
        support.record_infra(service, region, 0.7, support.BASE)  # 要求 0.8

        conclusion = service.evaluate("city-av", region, capability, support.BASE)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.BLOCKED)
        self.assertIn("infrastructure_unmet", {b.code for b in conclusion.blockers})

    def test_stale_reading_is_data_gap(self):
        service = support.make_service()
        region, capability = "suburbs", "robotaxi-l4"
        support.record_daily(service, region, capability, support.BASE + 4 * support.DAY, 7)
        support.record_infra(service, region, 0.85, support.BASE)  # 更新周期 3 天

        conclusion = service.evaluate("city-av", region, capability, support.BASE + 4 * support.DAY)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.INSUFFICIENT_DATA)
        self.assertIn("rsu-coverage", {e.subject for e in conclusion.exceptions if e.kind == "data_gap"})


class ExemptionTests(unittest.TestCase):
    """豁免：限定范围与期限；到期后受影响能力自动重新暴露。"""

    def setUp(self):
        self.service = support.make_service()
        self.region, self.capability = "downtown", "robotaxi-l4"
        # 城区接管率 0.007：低于默认阈值 0.01，高于城区覆盖阈值 0.004
        support.record_daily(self.service, self.region, self.capability, support.BASE, 7, takeover=0.007)
        support.record_infra(self.service, self.region, 0.95, support.BASE)

    def _grant(self, regions, expires_at, exemption_id="EX-1"):
        exemption = Exemption(
            exemption_id,
            ("takeover-rate",),
            regions,
            (self.capability,),
            "道路施工临时豁免",
            "alice",
            support.BASE,
            expires_at,
        )
        return self.service.grant_exemption(exemption)

    def test_scope_and_expiry_are_mandatory(self):
        with self.assertRaises(ExemptionScopeError):
            Exemption("EX-0", (), (self.region,), (self.capability,), "r", "alice", support.BASE, support.BASE + support.DAY)
        with self.assertRaises(ExemptionScopeError):
            Exemption("EX-0", ("takeover-rate",), (self.region,), (self.capability,), "r", "alice", support.BASE, support.BASE)

    def test_scoped_exemption_only_covers_listed_region(self):
        self._grant(("suburbs",), support.BASE + 2 * support.DAY)
        conclusion = self.service.evaluate("city-av", self.region, self.capability, support.BASE)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.BLOCKED)

    def test_active_exemption_masks_breach(self):
        self._grant((self.region,), support.BASE + 2 * support.DAY)
        conclusion = self.service.evaluate("city-av", self.region, self.capability, support.BASE + support.DAY)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.READY)
        applied = [e for e in conclusion.exceptions if e.kind == "exemption_applied"]
        self.assertEqual([e.reference_id for e in applied], ["EX-1"])

    def test_expired_exemption_exposes_capability(self):
        self._grant((self.region,), support.BASE + 2 * support.DAY)
        # 豁免期内没有暴露
        self.assertEqual(self.service.exposure_report(support.BASE + support.DAY), [])

        # 数据持续更新到 BASE+3 天，豁免已于 BASE+2 天到期
        support.record_daily(self.service, self.region, self.capability, support.BASE + 3 * support.DAY, 3, takeover=0.007)
        conclusion = self.service.evaluate("city-av", self.region, self.capability, support.BASE + 3 * support.DAY)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.BLOCKED)
        self.assertIn("metric_threshold", {b.code for b in conclusion.blockers})
        expired = [e for e in conclusion.exceptions if e.kind == "exemption_expired"]
        self.assertEqual([e.reference_id for e in expired], ["EX-1"])

        report = self.service.exposure_report(support.BASE + 3 * support.DAY)
        self.assertEqual(len(report), 1)
        entry = report[0]
        self.assertEqual((entry.exemption_id, entry.region_code, entry.capability_code), ("EX-1", self.region, self.capability))
        self.assertEqual(entry.exposed_metrics, ("takeover-rate",))
        self.assertTrue(entry.currently_blocking)


if __name__ == "__main__":
    unittest.main()
