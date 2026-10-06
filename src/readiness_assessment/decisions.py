"""委员会阶段决定：提出、会签、驳回、撤销。

采用事件溯源：所有动作只追加不可变事件，状态由事件折叠得到。
生命周期：

    提出 -> DRAFT ──会签(未满额)──> REVIEW ──会签(满额)──> APPROVED（已签发，终态）
                     │                  │
                     └── 驳回/撤销 ──> REJECTED / WITHDRAWN（终态）

APPROVED / REJECTED / WITHDRAWN 均为终态：
已经签发（APPROVED）的阶段决定永久保留提案时的结论快照，
任何后续数据变化、阈值换版或重算都不能修改它。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .contracts import DecisionStage
from .errors import (
    AdmissionDomainError,
    DecisionClosedError,
    DuplicateCosignError,
    InvalidCommitteeActionError,
)


class DecisionEventType(StrEnum):
    PROPOSED = "proposed"
    COSIGNED = "cosigned"
    ISSUED = "issued"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


_TERMINAL = {DecisionStage.APPROVED, DecisionStage.REJECTED, DecisionStage.WITHDRAWN}


@dataclass(frozen=True)
class DecisionEvent:
    """不可变领域事件。"""

    event_id: str
    decision_id: str
    kind: DecisionEventType
    actor: str
    occurred_at: datetime
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionView:
    """决定的只读视图（事件折叠结果）。"""

    decision_id: str
    fleet_id: str
    region_code: str
    scheme_id: str
    scheme_version: int
    stage: DecisionStage
    proposer: str
    cosigners: tuple[str, ...]
    min_cosignatures: int
    conclusion: dict
    reject_reason: str | None
    events: tuple[DecisionEvent, ...]

    @property
    def is_terminal(self) -> bool:
        return self.stage in _TERMINAL

    @property
    def is_issued(self) -> bool:
        return self.stage is DecisionStage.APPROVED

    @property
    def cosignatures_remaining(self) -> int:
        return max(0, self.min_cosignatures - len(self.cosigners))

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "fleet_id": self.fleet_id,
            "region_code": self.region_code,
            "scheme_id": self.scheme_id,
            "scheme_version": self.scheme_version,
            "stage": self.stage.value,
            "proposer": self.proposer,
            "cosigners": list(self.cosigners),
            "min_cosignatures": self.min_cosignatures,
            "cosignatures_remaining": self.cosignatures_remaining,
            "reject_reason": self.reject_reason,
            "conclusion": self.conclusion,
            "events": [
                {
                    "event_id": e.event_id,
                    "kind": e.kind.value,
                    "actor": e.actor,
                    "occurred_at": e.occurred_at.isoformat(),
                }
                for e in self.events
            ],
        }


def _fold(decision_id: str, events: tuple[DecisionEvent, ...]) -> DecisionView:
    if not events:
        raise InvalidCommitteeActionError(f"决定 {decision_id} 不存在")
    proposed = events[0]
    if proposed.kind is not DecisionEventType.PROPOSED:
        raise AdmissionDomainError("决定的首个事件必须是提出")

    stage = DecisionStage.DRAFT
    cosigners: list[str] = []
    reject_reason = None
    for event in events[1:]:
        if stage in _TERMINAL:
            raise AdmissionDomainError(f"决定 {decision_id} 已终态，事件流非法: {event.kind}")
        if event.kind is DecisionEventType.COSIGNED:
            if stage not in (DecisionStage.DRAFT, DecisionStage.REVIEW):
                raise AdmissionDomainError("仅在会签阶段可以追加会签")
            if event.actor in cosigners:
                raise AdmissionDomainError(f"委员 {event.actor} 重复会签")
            cosigners.append(event.actor)
            stage = DecisionStage.REVIEW
        elif event.kind is DecisionEventType.ISSUED:
            if stage is not DecisionStage.REVIEW:
                raise AdmissionDomainError("只有会签中的决定可以签发")
            if len(cosigners) < proposed.payload["min_cosignatures"]:
                raise AdmissionDomainError("会签人数未满，不能签发")
            stage = DecisionStage.APPROVED
        elif event.kind is DecisionEventType.REJECTED:
            stage = DecisionStage.REJECTED
            reject_reason = event.payload.get("reason")
        elif event.kind is DecisionEventType.WITHDRAWN:
            stage = DecisionStage.WITHDRAWN
        else:
            raise AdmissionDomainError(f"未知事件类型: {event.kind}")

    payload = proposed.payload
    return DecisionView(
        decision_id=decision_id,
        fleet_id=payload["fleet_id"],
        region_code=payload["region_code"],
        scheme_id=payload["scheme_id"],
        scheme_version=payload["scheme_version"],
        stage=stage,
        proposer=proposed.actor,
        cosigners=tuple(cosigners),
        min_cosignatures=payload["min_cosignatures"],
        conclusion=payload["conclusion"],
        reject_reason=reject_reason,
        events=events,
    )


class DecisionStore:
    """线程安全的决定仓储：所有动作在锁内完成"校验 + 追加事件"。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, list[DecisionEvent]] = {}
        self._seq = 0

    # -- 内部工具 --------------------------------------------------------
    def _next_event_id(self, decision_id: str) -> str:
        self._seq += 1
        return f"{decision_id}:evt:{self._seq}"

    def _view(self, decision_id: str) -> DecisionView:
        events = self._events.get(decision_id)
        if not events:
            raise InvalidCommitteeActionError(f"决定 {decision_id} 不存在")
        return _fold(decision_id, tuple(events))

    # -- 委员会动作 ------------------------------------------------------
    def propose(
        self,
        decision_id: str,
        *,
        actor: str,
        fleet_id: str,
        region_code: str,
        scheme_id: str,
        scheme_version: int,
        min_cosignatures: int,
        conclusion: dict,
        at: datetime,
    ) -> DecisionView:
        """提出一项阶段决定，conclusion 为提案时刻的候选结论快照。"""
        if at.tzinfo is None:
            raise AdmissionDomainError("动作时间必须带时区")
        with self._lock:
            if decision_id in self._events:
                raise InvalidCommitteeActionError(f"决定 {decision_id} 已存在，不能重复提出")
            event = DecisionEvent(
                event_id=self._next_event_id(decision_id),
                decision_id=decision_id,
                kind=DecisionEventType.PROPOSED,
                actor=actor,
                occurred_at=at,
                payload={
                    "fleet_id": fleet_id,
                    "region_code": region_code,
                    "scheme_id": scheme_id,
                    "scheme_version": scheme_version,
                    "min_cosignatures": min_cosignatures,
                    "conclusion": conclusion,
                },
            )
            self._events[decision_id] = [event]
            return self._view(decision_id)

    def cosign(self, decision_id: str, member: str, at: datetime) -> DecisionView:
        """委员会签。会签满额时原子签发（APPROVED）。"""
        with self._lock:
            view = self._view(decision_id)
            self._ensure_open(decision_id, view.stage)
            if member == view.proposer:
                raise InvalidCommitteeActionError("提案人不能为自己的提议会签")
            if member in view.cosigners:
                raise DuplicateCosignError(f"委员 {member} 已会签过决定 {decision_id}")

            cosigned = DecisionEvent(
                event_id=self._next_event_id(decision_id),
                decision_id=decision_id,
                kind=DecisionEventType.COSIGNED,
                actor=member,
                occurred_at=at,
            )
            self._events[decision_id].append(cosigned)

            # 满额即签发：COSIGNED 与 ISSUED 在同一把锁内追加，
            # 并发会签下保证"恰好满额、恰好签发一次"。
            if len(self._events[decision_id]) - 1 >= view.min_cosignatures:
                self._events[decision_id].append(
                    DecisionEvent(
                        event_id=self._next_event_id(decision_id),
                        decision_id=decision_id,
                        kind=DecisionEventType.ISSUED,
                        actor=member,
                        occurred_at=at,
                        payload={"cosigners": list(view.cosigners) + [member]},
                    )
                )
            return self._view(decision_id)

    def reject(self, decision_id: str, member: str, at: datetime, reason: str) -> DecisionView:
        """驳回（非提案人均可），终态。"""
        if not reason or not reason.strip():
            raise InvalidCommitteeActionError("驳回必须填写理由")
        with self._lock:
            view = self._view(decision_id)
            self._ensure_open(decision_id, view.stage)
            if member == view.proposer:
                raise InvalidCommitteeActionError("提案人不能驳回自己的提议，请使用撤销")
            self._events[decision_id].append(
                DecisionEvent(
                    event_id=self._next_event_id(decision_id),
                    decision_id=decision_id,
                    kind=DecisionEventType.REJECTED,
                    actor=member,
                    occurred_at=at,
                    payload={"reason": reason.strip()},
                )
            )
            return self._view(decision_id)

    def withdraw(self, decision_id: str, actor: str, at: datetime) -> DecisionView:
        """提案人撤销，终态。"""
        with self._lock:
            view = self._view(decision_id)
            self._ensure_open(decision_id, view.stage)
            if actor != view.proposer:
                raise InvalidCommitteeActionError("只有提案人可以撤销决定")
            self._events[decision_id].append(
                DecisionEvent(
                    event_id=self._next_event_id(decision_id),
                    decision_id=decision_id,
                    kind=DecisionEventType.WITHDRAWN,
                    actor=actor,
                    occurred_at=at,
                )
            )
            return self._view(decision_id)

    # -- 查询 ------------------------------------------------------------
    def get(self, decision_id: str) -> DecisionView:
        with self._lock:
            return self._view(decision_id)

    def list_for(self, fleet_id: str, region_code: str) -> list[DecisionView]:
        with self._lock:
            views = [self._view(did) for did in self._events]
        return [
            v
            for v in views
            if v.fleet_id == fleet_id and v.region_code == region_code
        ]

    def issued_for(self, fleet_id: str, region_code: str) -> list[DecisionView]:
        return [v for v in self.list_for(fleet_id, region_code) if v.is_issued]

    def events(self, decision_id: str) -> tuple[DecisionEvent, ...]:
        with self._lock:
            return tuple(self._events[decision_id])

    @staticmethod
    def _ensure_open(decision_id: str, stage: DecisionStage) -> None:
        if stage in _TERMINAL:
            raise DecisionClosedError(
                f"决定 {decision_id} 已处于终态 {stage.value}，不可再追加动作"
            )
