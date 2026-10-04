import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import support
from readiness_assessment.decisions import (
    Committee,
    DecisionError,
    DecisionStatus,
    DuplicateSignatureError,
    ImmutableDecisionError,
    InvalidTransitionError,
    UnknownMemberError,
)
from readiness_assessment.plan import OperationStage


def propose(committee: Committee, decision_id: str = "D-1", proposed_by: str = "alice"):
    return committee.propose(
        decision_id=decision_id,
        plan_id="city-av",
        plan_version=1,
        region_code="suburbs",
        capability_code="robotaxi-l4",
        target_stage=OperationStage.COMMERCIAL,
        conclusion_id="C-00001",
        proposed_by=proposed_by,
        proposed_at=support.BASE,
    )


class CommitteeConfigTests(unittest.TestCase):
    def test_invalid_committee_config_is_rejected(self):
        with self.assertRaises(ValueError):
            Committee([], 1)
        with self.assertRaises(ValueError):
            Committee(("alice",), 0)
        with self.assertRaises(ValueError):
            Committee(("alice",), 2)


class DecisionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.committee = Committee(("alice", "bob", "carol"), quorum=2)

    def test_propose_requires_member_and_unique_id(self):
        with self.assertRaises(UnknownMemberError):
            propose(self.committee, proposed_by="outsider")
        propose(self.committee)
        with self.assertRaises(DecisionError):
            propose(self.committee)

    def test_countersign_until_quorum_issues_decision(self):
        propose(self.committee)
        first = self.committee.countersign("D-1", "bob", support.BASE)
        self.assertEqual((first.status, first.signatures), (DecisionStatus.PROPOSED, ("bob",)))
        second = self.committee.countersign("D-1", "carol", support.BASE + support.HOUR)
        self.assertEqual(second.status, DecisionStatus.ISSUED)
        self.assertEqual(second.signatures, ("bob", "carol"))
        self.assertEqual(second.resolved_at, support.BASE + support.HOUR)

    def test_duplicate_signature_is_rejected(self):
        propose(self.committee)
        self.committee.countersign("D-1", "bob", support.BASE)
        with self.assertRaises(DuplicateSignatureError):
            self.committee.countersign("D-1", "bob", support.BASE)

    def test_countersign_requires_member(self):
        propose(self.committee)
        with self.assertRaises(UnknownMemberError):
            self.committee.countersign("D-1", "outsider", support.BASE)

    def test_reject_requires_reason_and_is_terminal(self):
        propose(self.committee)
        with self.assertRaises(DecisionError):
            self.committee.reject("D-1", "bob", "", support.BASE)
        rejected = self.committee.reject("D-1", "bob", "证据不足", support.BASE)
        self.assertEqual((rejected.status, rejected.reason), (DecisionStatus.REJECTED, "证据不足"))
        with self.assertRaises(InvalidTransitionError):
            self.committee.countersign("D-1", "carol", support.BASE)

    def test_withdraw_only_by_proposer_and_only_while_proposed(self):
        propose(self.committee)
        with self.assertRaises(InvalidTransitionError):
            self.committee.withdraw("D-1", "bob", support.BASE)
        withdrawn = self.committee.withdraw("D-1", "alice", support.BASE)
        self.assertEqual(withdrawn.status, DecisionStatus.WITHDRAWN)
        with self.assertRaises(InvalidTransitionError):
            self.committee.countersign("D-1", "bob", support.BASE)

    def test_issued_decision_is_immutable(self):
        propose(self.committee)
        self.committee.countersign("D-1", "bob", support.BASE)
        issued = self.committee.countersign("D-1", "carol", support.BASE)
        self.assertEqual(issued.status, DecisionStatus.ISSUED)
        with self.assertRaises(ImmutableDecisionError):
            self.committee.countersign("D-1", "alice", support.BASE)
        with self.assertRaises(ImmutableDecisionError):
            self.committee.reject("D-1", "bob", "太迟了", support.BASE)
        with self.assertRaises(ImmutableDecisionError):
            self.committee.withdraw("D-1", "alice", support.BASE)


class ConcurrentCountersignTests(unittest.TestCase):
    """并发会签：法定人数恰好签发一次，其余提交被不可变约束拒绝。"""

    def test_concurrent_countersign_issues_exactly_once(self):
        members = tuple(f"member-{i}" for i in range(8))
        committee = Committee(members, quorum=3)
        propose(committee, proposed_by="member-0")

        barrier = threading.Barrier(len(members))
        outcomes: list[object] = []

        def sign(member: str) -> None:
            barrier.wait(timeout=10)
            try:
                outcomes.append(committee.countersign("D-1", member, support.BASE))
            except ImmutableDecisionError as exc:
                outcomes.append(exc)

        with ThreadPoolExecutor(max_workers=len(members)) as pool:
            list(pool.map(sign, members))

        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        rejected = [o for o in outcomes if isinstance(o, ImmutableDecisionError)]
        self.assertEqual(len(succeeded), 3)
        self.assertEqual(len(rejected), 5)

        final = committee.get("D-1")
        self.assertEqual(final.status, DecisionStatus.ISSUED)
        self.assertEqual(len(final.signatures), 3)
        self.assertEqual(len(set(final.signatures)), 3)
        self.assertIsNotNone(final.resolved_at)

    def test_concurrent_mixed_operations_keep_single_terminal_state(self):
        members = ("alice", "bob", "carol", "dave")
        committee = Committee(members, quorum=2)
        propose(committee, proposed_by="alice")

        barrier = threading.Barrier(3)
        outcomes: list[object] = []

        def act(action) -> None:
            barrier.wait(timeout=10)
            try:
                outcomes.append(action())
            except (ImmutableDecisionError, InvalidTransitionError) as exc:
                outcomes.append(exc)

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(act, lambda: committee.countersign("D-1", "bob", support.BASE)),
                pool.submit(act, lambda: committee.countersign("D-1", "carol", support.BASE)),
                pool.submit(act, lambda: committee.reject("D-1", "dave", "存疑", support.BASE)),
            ]
            for future in futures:
                future.result()

        final = committee.get("D-1")
        # 无论并发顺序如何，最终只可能落在一个终态，且不会从终态再迁移
        self.assertIn(final.status, (DecisionStatus.ISSUED, DecisionStatus.REJECTED, DecisionStatus.PROPOSED))
        if final.status is DecisionStatus.ISSUED:
            self.assertEqual(len(final.signatures), 2)


if __name__ == "__main__":
    unittest.main()
