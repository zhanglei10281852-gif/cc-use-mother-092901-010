"""委员会工作流测试：提出、会签、驳回、撤销，以及并发会签。

并发会签验证：多线程同时为同一决定补签，最终恰好达到要求人数、
只签发一次，超出满额的会签被拒绝；终态决定的任何后续动作都失败。
"""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import T0, make_service
from readiness_assessment.contracts import DecisionStage
from readiness_assessment.domain import InfrastructureCondition, Observation
from readiness_assessment.errors import (
    DecisionClosedError,
    DuplicateCosignError,
    InvalidCommitteeActionError,
    ProposalBlockedError,
)


def _make_ready(svc, region="Z-1", fleet="fleet-a"):
    for day in range(10):
        svc.add_observation(Observation("incident-rate", region, T0 - timedelta(days=day), 0.02))
        svc.add_observation(Observation("takeover-rate", region, T0 - timedelta(days=day), 0.005))
    svc.report_infrastructure(
        InfrastructureCondition("rsu-coverage", region, 0.95, 0.9, T0 - timedelta(days=1))
    )


class CommitteeWorkflowTests(unittest.TestCase):
    def test_proposal_blocked_when_blockers_exist(self):
        svc = make_service()
        # 无任何数据 -> 缺口阻塞
        with self.assertRaises(ProposalBlockedError) as ctx:
            svc.propose("D-1", "fleet-a", "Z-1", "chair")
        self.assertIn("下阶段阻塞", str(ctx.exception))

    def test_full_propose_cosign_issue_flow(self):
        svc = make_service()
        _make_ready(svc)
        view = svc.propose("D-1", "fleet-a", "Z-1", "chair")
        self.assertEqual(view.stage, DecisionStage.DRAFT)
        self.assertEqual(view.cosignatures_remaining, 2)

        view = svc.cosign("D-1", "m1")
        self.assertEqual(view.stage, DecisionStage.REVIEW)
        self.assertEqual(view.cosignatures_remaining, 1)

        view = svc.cosign("D-1", "m2")
        self.assertEqual(view.stage, DecisionStage.APPROVED)
        self.assertTrue(view.is_issued)
        self.assertEqual(view.cosigners, ("m1", "m2"))
        # 事件链：提出 + 两次会签 + 满额签发
        kinds = [e.kind.value for e in svc.decisions.events("D-1")]
        self.assertEqual(kinds, ["proposed", "cosigned", "cosigned", "issued"])

    def test_proposer_cannot_cosign_or_reject_own_proposal(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        with self.assertRaises(InvalidCommitteeActionError):
            svc.cosign("D-1", "chair")

    def test_duplicate_cosign_rejected(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        svc.cosign("D-1", "m1")
        with self.assertRaises(DuplicateCosignError):
            svc.cosign("D-1", "m1")

    def test_reject_is_terminal_and_records_reason(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        view = svc.reject("D-1", "m1", "观测窗口样本不足")
        self.assertEqual(view.stage, DecisionStage.REJECTED)
        self.assertEqual(view.reject_reason, "观测窗口样本不足")
        self.assertTrue(view.is_terminal)
        with self.assertRaises(DecisionClosedError):
            svc.cosign("D-1", "m2")

    def test_reject_requires_reason(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        with self.assertRaises(InvalidCommitteeActionError):
            svc.reject("D-1", "m1", "  ")

    def test_only_proposer_can_withdraw(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        with self.assertRaises(InvalidCommitteeActionError):
            svc.withdraw("D-1", "someone-else")
        view = svc.withdraw("D-1", "chair")
        self.assertEqual(view.stage, DecisionStage.WITHDRAWN)
        with self.assertRaises(DecisionClosedError):
            svc.cosign("D-1", "m1")

    def test_issued_decision_is_immutable_to_later_data_changes(self):
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")
        svc.cosign("D-1", "m1")
        svc.cosign("D-1", "m2")
        # 签发后追加恶劣数据
        for _ in range(5):
            svc.add_observation(
                Observation("incident-rate", "Z-1", T0 - timedelta(hours=1), 0.9)
            )
        view = svc.get_decision("D-1")
        self.assertEqual(view.stage, DecisionStage.APPROVED)
        self.assertTrue(view.conclusion["ready"])
        with self.assertRaises(DecisionClosedError):
            svc.reject("D-1", "m1", "迟来的驳回")

    def test_concurrent_cosignatures_issue_exactly_once(self):
        """10 个线程并发会签，仅要求的 2 人成功并签发，其余被拒。"""
        svc = make_service()
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")

        outcomes: list[str] = []
        barrier_lock_members = [f"m{i}" for i in range(10)]

        def attempt(member: str) -> None:
            try:
                view = svc.cosign("D-1", member)
                outcomes.append(f"ok:{view.stage.value}")
            except (DecisionClosedError, DuplicateCosignError, InvalidCommitteeActionError):
                outcomes.append("rejected")

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(attempt, barrier_lock_members))

        view = svc.get_decision("D-1")
        self.assertEqual(view.stage, DecisionStage.APPROVED)
        self.assertEqual(len(view.cosigners), 2)
        self.assertEqual(outcomes.count("ok:approved"), 1)
        self.assertEqual(outcomes.count("ok:review"), 1)
        self.assertEqual(outcomes.count("rejected"), 8)
        # ISSUED 事件恰好一条
        issued = [e for e in svc.decisions.events("D-1") if e.kind.value == "issued"]
        self.assertEqual(len(issued), 1)

    def test_concurrent_cosign_and_reject_only_one_wins(self):
        """并发"满额签发"与"驳回"：两者都试图进入终态，恰有一方失败。"""
        from factories import make_scheme
        svc = make_service(scheme=make_scheme(min_cosignatures=1))
        _make_ready(svc)
        svc.propose("D-1", "fleet-a", "Z-1", "chair")

        def cosign() -> str:
            try:
                view = svc.cosign("D-1", "m1")
                return f"cosigned:{view.stage.value}"
            except DecisionClosedError:
                return "lost"

        def reject() -> str:
            try:
                svc.reject("D-1", "m9", "委员会异议")
                return "rejected"
            except DecisionClosedError:
                return "lost"

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(cosign)
            f2 = pool.submit(reject)
            r1, r2 = f1.result(), f2.result()

        self.assertEqual((r1, r2).count("lost"), 1)
        view = svc.get_decision("D-1")
        self.assertIn(
            view.stage, (DecisionStage.APPROVED, DecisionStage.REJECTED)
        )


if __name__ == "__main__":
    unittest.main()
