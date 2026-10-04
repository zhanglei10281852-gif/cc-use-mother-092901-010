"""委员会阶段决定：提出、会签、驳回、撤销；签发后不可变。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from .plan import OperationStage


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    ISSUED = "issued"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class DecisionError(Exception):
    """阶段决定操作被拒绝。"""


class ImmutableDecisionError(DecisionError):
    """已签发的阶段决定不可变更。"""


class UnknownMemberError(DecisionError):
    """操作者不是委员会成员。"""


class DuplicateSignatureError(DecisionError):
    """同一成员重复会签。"""


class InvalidTransitionError(DecisionError):
    """当前状态下不允许该操作。"""


@dataclass(frozen=True)
class StageDecision:
    """阶段决定记录；签发（ISSUED）后任何字段都不得再变。"""

    decision_id: str
    plan_id: str
    plan_version: int
    region_code: str
    capability_code: str
    target_stage: OperationStage
    conclusion_id: str
    proposed_by: str
    proposed_at: datetime
    status: DecisionStatus
    signatures: tuple[str, ...] = ()
    reason: str = ""
    resolved_at: datetime | None = None


class Committee:
    """委员会：会签达到法定人数即签发；所有状态迁移在锁内完成，支持并发会签。"""

    def __init__(self, members: Iterable[str], quorum: int) -> None:
        members = frozenset(members)
        if not members:
            raise ValueError("委员会至少需要一名成员")
        if not 1 <= quorum <= len(members):
            raise ValueError("会签法定人数必须介于 1 与成员总数之间")
        self._members = members
        self._quorum = quorum
        self._decisions: dict[str, StageDecision] = {}
        self._lock = threading.Lock()

    @property
    def quorum(self) -> int:
        return self._quorum

    @property
    def members(self) -> frozenset[str]:
        return self._members

    def propose(
        self,
        *,
        decision_id: str,
        plan_id: str,
        plan_version: int,
        region_code: str,
        capability_code: str,
        target_stage: OperationStage,
        conclusion_id: str,
        proposed_by: str,
        proposed_at: datetime,
        reason: str = "",
    ) -> StageDecision:
        with self._lock:
            self._require_member(proposed_by)
            if decision_id in self._decisions:
                raise DecisionError(f"决定编号 {decision_id} 已存在")
            decision = StageDecision(
                decision_id=decision_id,
                plan_id=plan_id,
                plan_version=plan_version,
                region_code=region_code,
                capability_code=capability_code,
                target_stage=target_stage,
                conclusion_id=conclusion_id,
                proposed_by=proposed_by,
                proposed_at=proposed_at,
                status=DecisionStatus.PROPOSED,
                reason=reason,
            )
            self._decisions[decision_id] = decision
            return decision

    def countersign(self, decision_id: str, member: str, at: datetime) -> StageDecision:
        """会签；达到法定人数即签发。签发后再次会签将被拒绝。"""
        with self._lock:
            decision = self._mutable(decision_id)
            self._require_member(member)
            if member in decision.signatures:
                raise DuplicateSignatureError(f"成员 {member} 已会签过决定 {decision_id}")
            signatures = decision.signatures + (member,)
            issued = len(signatures) >= self._quorum
            decision = replace(
                decision,
                signatures=signatures,
                status=DecisionStatus.ISSUED if issued else DecisionStatus.PROPOSED,
                resolved_at=at if issued else None,
            )
            self._decisions[decision_id] = decision
            return decision

    def reject(self, decision_id: str, member: str, reason: str, at: datetime) -> StageDecision:
        with self._lock:
            decision = self._mutable(decision_id)
            self._require_member(member)
            if not reason:
                raise DecisionError("驳回必须说明理由")
            decision = replace(decision, status=DecisionStatus.REJECTED, reason=reason, resolved_at=at)
            self._decisions[decision_id] = decision
            return decision

    def withdraw(self, decision_id: str, member: str, at: datetime) -> StageDecision:
        with self._lock:
            decision = self._mutable(decision_id)
            self._require_member(member)
            if member != decision.proposed_by:
                raise InvalidTransitionError("仅提案人可以撤销决定")
            decision = replace(decision, status=DecisionStatus.WITHDRAWN, resolved_at=at)
            self._decisions[decision_id] = decision
            return decision

    def get(self, decision_id: str) -> StageDecision:
        try:
            return self._decisions[decision_id]
        except KeyError:
            raise KeyError(f"未知决定编号 {decision_id}") from None

    def decisions(self) -> tuple[StageDecision, ...]:
        return tuple(self._decisions.values())

    def _mutable(self, decision_id: str) -> StageDecision:
        decision = self.get(decision_id)
        if decision.status is DecisionStatus.ISSUED:
            raise ImmutableDecisionError(f"决定 {decision_id} 已签发，保持不可变")
        if decision.status is not DecisionStatus.PROPOSED:
            raise InvalidTransitionError(f"决定 {decision_id} 已处于 {decision.status.value} 状态，无法再变更")
        return decision

    def _require_member(self, member: str) -> None:
        if member not in self._members:
            raise UnknownMemberError(f"{member} 不是委员会成员")
