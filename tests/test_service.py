import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import support
from readiness_assessment.decisions import DecisionStatus, ImmutableDecisionError
from readiness_assessment.evaluation import ConclusionOutcome
from readiness_assessment.plan import OperationStage


def ready_suburbs_service():
    service = support.make_service()
    support.record_daily(service, "suburbs", "robotaxi-l4", support.BASE, 7)
    support.record_infra(service, "suburbs", 0.85, support.BASE)
    return service


class IssuedDecisionImmutabilityTests(unittest.TestCase):
    """已签发的阶段决定保持不可变：重算改变候选结论，但决定本身不动。"""

    def test_issued_decision_survives_rule_change_and_recompute(self):
        service = ready_suburbs_service()
        conclusion = service.evaluate("city-av", "suburbs", "robotaxi-l4", support.BASE)
        self.assertEqual(conclusion.outcome, ConclusionOutcome.READY)

        decision = service.propose_decision(
            conclusion_id=conclusion.conclusion_id,
            target_stage=OperationStage.COMMERCIAL,
            proposed_by="alice",
            proposed_at=support.BASE,
        )
        service.countersign(decision.decision_id, "bob", support.BASE + support.HOUR)
        issued = service.countersign(decision.decision_id, "carol", support.BASE + 2 * support.HOUR)
        self.assertEqual(issued.status, DecisionStatus.ISSUED)

        # 规则换版收紧阈值 → 候选结论重算为阻塞
        recomputed = service.register_plan(
            support.make_plan(version=2, takeover_threshold=0.0005),
            as_of=support.BASE + support.DAY,
        )
        suburbs_v2 = next(c for c in recomputed if c.region_code == "suburbs")
        self.assertEqual(suburbs_v2.outcome, ConclusionOutcome.BLOCKED)

        # 已签发决定保持签发状态、签名与所依据的 v1 结论不变
        kept = service.decision(decision.decision_id)
        self.assertEqual(kept.status, DecisionStatus.ISSUED)
        self.assertEqual(kept.signatures, ("bob", "carol"))
        self.assertEqual(kept.plan_version, 1)
        self.assertEqual(kept.conclusion_id, conclusion.conclusion_id)
        with self.assertRaises(ImmutableDecisionError):
            service.withdraw(decision.decision_id, "alice", support.BASE + 2 * support.DAY)
        with self.assertRaises(ImmutableDecisionError):
            service.reject(decision.decision_id, "bob", "事后反悔", support.BASE + 2 * support.DAY)


class InterfaceViewTests(unittest.TestCase):
    """接口展示：每项结论的证据、例外与下阶段阻塞原因。"""

    def test_conclusion_view_exposes_evidence_exceptions_blockers(self):
        service = support.make_service()
        support.record_daily(service, "downtown", "robotaxi-l4", support.BASE, 7, takeover=0.007)
        support.record_infra(service, "downtown", 0.95, support.BASE)
        conclusion = service.evaluate("city-av", "downtown", "robotaxi-l4", support.BASE)

        view = service.conclusion_view(conclusion.conclusion_id)
        self.assertEqual(view["outcome"], "blocked")
        self.assertEqual(view["plan_version"], 1)
        self.assertTrue(any(e["kind"] == "metric" and e["subject"] == "takeover-rate" for e in view["evidence"]))
        self.assertTrue(any(b["code"] == "metric_threshold" for b in view["blockers"]))
        self.assertEqual(view["exceptions"], [])

    def test_decision_view_embeds_conclusion(self):
        service = ready_suburbs_service()
        conclusion = service.evaluate("city-av", "suburbs", "robotaxi-l4", support.BASE)
        decision = service.propose_decision(
            conclusion_id=conclusion.conclusion_id,
            target_stage=OperationStage.EXPANDED,
            proposed_by="alice",
            proposed_at=support.BASE,
        )
        view = service.decision_view(decision.decision_id)
        self.assertEqual(view["status"], "proposed")
        self.assertEqual(view["target_stage"], "expanded")
        self.assertEqual(view["conclusion"]["conclusion_id"], conclusion.conclusion_id)
        self.assertEqual(view["conclusion"]["outcome"], "ready")

    def test_board_lists_all_region_capability_pairs(self):
        service = support.make_service()
        support.record_daily(service, "downtown", "robotaxi-l4", support.BASE, 7, takeover=0.007)
        support.record_daily(service, "suburbs", "robotaxi-l4", support.BASE, 7)
        support.record_infra(service, "downtown", 0.95, support.BASE)
        support.record_infra(service, "suburbs", 0.85, support.BASE)

        board = service.board("city-av", support.BASE)
        self.assertEqual(board["plan_version"], 1)
        self.assertEqual(len(board["items"]), 2)
        by_region = {item["region_code"]: item for item in board["items"]}
        self.assertEqual(by_region["suburbs"]["outcome"], "ready")
        self.assertEqual(by_region["downtown"]["outcome"], "blocked")
        self.assertIsNone(by_region["suburbs"]["decision"])

        # 提出决定后看板展示决定状态
        suburbs_ready = service.evaluate("city-av", "suburbs", "robotaxi-l4", support.BASE)
        decision = service.propose_decision(
            conclusion_id=suburbs_ready.conclusion_id,
            target_stage=OperationStage.COMMERCIAL,
            proposed_by="alice",
            proposed_at=support.BASE,
        )
        board = service.board("city-av", support.BASE)
        by_region = {item["region_code"]: item for item in board["items"]}
        self.assertEqual(by_region["suburbs"]["decision"], {"decision_id": decision.decision_id, "status": "proposed"})


if __name__ == "__main__":
    unittest.main()
