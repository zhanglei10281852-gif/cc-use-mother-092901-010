"""商业化运营准入评估服务（门面）。

职责编排：
- 维护申请（车辆档案 + 运营区域 + 方案）及其全部输入数据；
- 在数据变化（观测/事件/整改/设施/豁免）、阈值换版、重大事件、
  豁免到期时重新计算候选结论，候选结论按时间留痕；
- 委员会动作委托 :class:`~readiness_assessment.decisions.DecisionStore`，
  提案必须以当前无阻塞的候选结论为依据；
- 已经签发的阶段决定只保存提案时的结论快照，重算永远不触碰它们。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .decisions import DecisionStore, DecisionView
from .domain import (
    Exemption,
    InfrastructureCondition,
    Observation,
    RemediationCommitment,
    OperatingRegion,
    SafetyEvent,
    VehicleProfile,
)
from .engine import CandidateConclusion, EvaluationInputs, evaluate
from .errors import ProposalBlockedError
from .scheme import AssessmentScheme, SchemeRegistry

_UTC = timezone.utc


@dataclass(frozen=True)
class Application:
    profile: VehicleProfile
    region: OperatingRegion
    scheme_id: str


@dataclass(frozen=True)
class CandidateRecord:
    """一次重算的留痕：触发原因 + 候选结论。"""

    reason: str
    conclusion: CandidateConclusion


class AdmissionService:
    def __init__(self, clock=lambda: datetime.now(_UTC)) -> None:
        self._clock = clock
        self.registry = SchemeRegistry()
        self.decisions = DecisionStore()
        self._applications: dict[tuple[str, str], Application] = {}
        # 区域级事实（所有在该区域运营的车队共享）：
        self._observations: dict[str, list[Observation]] = {}
        self._events: dict[str, list[SafetyEvent]] = {}
        self._infrastructure: dict[str, list[InfrastructureCondition]] = {}
        # 车队 + 区域级数据：
        self._commitments: dict[tuple[str, str], list[RemediationCommitment]] = {}
        self._exemptions: dict[str, list[Exemption]] = {}
        self._candidate_history: dict[tuple[str, str], list[CandidateRecord]] = {}

    # -- 申请与方案 ------------------------------------------------------
    def register_application(
        self, profile: VehicleProfile, region: OperatingRegion, scheme_id: str
    ) -> CandidateConclusion:
        if profile.region_code != region.code:
            raise ValueError("车辆档案与区域编码不一致")
        key = (profile.fleet_id, region.code)
        if key in self._applications:
            raise ValueError(f"申请 {key[0]}/{key[1]} 已登记")
        # 允许在方案首个版本发布前登记；评估时才要求存在已发布版本。
        self._applications[key] = Application(
            profile=profile, region=region, scheme_id=scheme_id
        )
        return self._recompute(*key, reason="application-registered")

    def update_region(self, region: OperatingRegion) -> list[CandidateConclusion]:
        """更新区域（如差异化阈值覆盖调整），重算该区域全部申请。"""
        keys = self._applications_in(region.code)
        if not keys:
            raise KeyError(f"区域 {region.code} 尚未登记申请")
        for fleet_id, _ in keys:
            key = (fleet_id, region.code)
            app = self._applications[key]
            self._applications[key] = Application(
                profile=app.profile, region=region, scheme_id=app.scheme_id
            )
        return self._recompute_for_region(
            region.code, reason="region-threshold-changed", multi=True
        )

    def publish_scheme(self, scheme: AssessmentScheme) -> None:
        self.registry.publish(scheme)
        # 阈值/规则换版：使用该方案的申请全部重算。
        for (fleet_id, region_code), app in self._applications.items():
            if app.scheme_id == scheme.scheme_id:
                self._recompute(fleet_id, region_code, reason="scheme-version-changed")

    # -- 数据接入（均触发重算） ------------------------------------------
    def add_observation(self, observation: Observation) -> CandidateConclusion:
        self._observations.setdefault(observation.region_code, []).append(observation)
        return self._recompute_for_region(
            observation.region_code, reason="observation-updated"
        )

    def add_event(self, event: SafetyEvent) -> list[CandidateConclusion]:
        self._events.setdefault(event.region_code, []).append(event)
        scheme_id = self._require_single_scheme(event.region_code)
        scheme = self.registry.latest_version_at(scheme_id, self._clock())
        reason = "major-event" if self._is_major(event, scheme) else "safety-event"
        return self._recompute_for_region(event.region_code, reason=reason, multi=True)

    def record_commitment(
        self, commitment: RemediationCommitment, fleet_id: str, region_code: str
    ) -> CandidateConclusion:
        key = (fleet_id, region_code)
        bucket = self._commitments.setdefault(key, [])
        bucket[:] = [c for c in bucket if c.commitment_id != commitment.commitment_id]
        bucket.append(commitment)
        return self._recompute(*key, reason="remediation-updated")

    def close_commitment(
        self, fleet_id: str, region_code: str, commitment_id: str, closed_at: datetime | None = None
    ) -> CandidateConclusion:
        key = (fleet_id, region_code)
        bucket = self._commitments.setdefault(key, [])
        for index, commitment in enumerate(bucket):
            if commitment.commitment_id == commitment_id:
                bucket[index] = RemediationCommitment(
                    commitment_id=commitment.commitment_id,
                    capability=commitment.capability,
                    due_at=commitment.due_at,
                    closed_at=closed_at or self._clock(),
                    note=commitment.note,
                )
                break
        else:
            raise KeyError(f"整改承诺 {commitment_id} 不存在")
        return self._recompute(*key, reason="remediation-closed")

    def report_infrastructure(self, condition: InfrastructureCondition) -> list[CandidateConclusion]:
        self._infrastructure.setdefault(condition.region_code, []).append(condition)
        return self._recompute_for_region(
            condition.region_code, reason="infrastructure-updated", multi=True
        )

    def grant_exemption(self, exemption: Exemption) -> list[CandidateConclusion]:
        """登记区域级豁免（按指标+能力+期限限定范围），并重算该区域全部申请。"""
        self._exemptions.setdefault(exemption.region_code, []).append(exemption)
        return self._recompute_for_region(
            exemption.region_code, reason="exemption-granted", multi=True
        )

    def sweep_time_advances(self, at: datetime | None = None) -> dict[tuple[str, str], str]:
        """时钟推进后的批量检查：重算所有申请，暴露到期豁免。

        返回每个申请实际的重算原因（"exemption-expiry" 或 "scheduled"）。
        """
        as_of = at or self._clock()
        results: dict[tuple[str, str], str] = {}
        for fleet_id, region_code in list(self._applications):
            previous = self.latest_candidate(fleet_id, region_code)
            conclusion = self._recompute(fleet_id, region_code, reason="scheduled", as_of=as_of)
            reason = "scheduled"
            if previous is not None:
                was_active = {
                    e.reference for e in previous.exceptions if e.kind == "exemption" and e.active
                }
                now_expired = {
                    e.reference
                    for e in conclusion.exceptions
                    if e.kind == "exemption" and not e.active
                }
                if was_active & now_expired:
                    reason = "exemption-expiry"
                    self._candidate_history[(fleet_id, region_code)][-1] = CandidateRecord(
                        reason=reason, conclusion=conclusion
                    )
            results[(fleet_id, region_code)] = reason
        return results

    # -- 候选结论 --------------------------------------------------------
    def evaluate(self, fleet_id: str, region_code: str, *, as_of: datetime | None = None) -> CandidateConclusion:
        return self._recompute(fleet_id, region_code, reason="manual", as_of=as_of)

    def latest_candidate(self, fleet_id: str, region_code: str) -> CandidateConclusion | None:
        history = self._candidate_history.get((fleet_id, region_code))
        return history[-1].conclusion if history else None

    def candidate_history(self, fleet_id: str, region_code: str) -> list[CandidateRecord]:
        return list(self._candidate_history.get((fleet_id, region_code), []))

    # -- 委员会动作 ------------------------------------------------------
    def propose(
        self,
        decision_id: str,
        fleet_id: str,
        region_code: str,
        actor: str,
        *,
        at: datetime | None = None,
    ) -> DecisionView:
        candidate = self.latest_candidate(fleet_id, region_code)
        if candidate is None:
            candidate = self._recompute(fleet_id, region_code, reason="before-proposal")
        assert candidate is not None
        if not candidate.ready:
            reasons = "; ".join(f"[{b.code}] {b.reason}" for b in candidate.blockers)
            raise ProposalBlockedError(
                f"候选结论存在 {len(candidate.blockers)} 项下阶段阻塞，不能提出决定：{reasons}"
            )
        scheme = self.registry.get(candidate.scheme_id, candidate.scheme_version)
        return self.decisions.propose(
            decision_id,
            actor=actor,
            fleet_id=fleet_id,
            region_code=region_code,
            scheme_id=candidate.scheme_id,
            scheme_version=candidate.scheme_version,
            min_cosignatures=scheme.min_cosignatures,
            conclusion=candidate.to_dict(),
            at=at or self._clock(),
        )

    def cosign(self, decision_id: str, member: str, *, at: datetime | None = None) -> DecisionView:
        return self.decisions.cosign(decision_id, member, at or self._clock())

    def reject(self, decision_id: str, member: str, reason: str, *, at: datetime | None = None) -> DecisionView:
        return self.decisions.reject(decision_id, member, at or self._clock(), reason)

    def withdraw(self, decision_id: str, actor: str, *, at: datetime | None = None) -> DecisionView:
        return self.decisions.withdraw(decision_id, actor, at or self._clock())

    def get_decision(self, decision_id: str) -> DecisionView:
        return self.decisions.get(decision_id)

    def decisions_for(self, fleet_id: str, region_code: str) -> list[DecisionView]:
        return self.decisions.list_for(fleet_id, region_code)

    # -- 内部实现 --------------------------------------------------------
    def _applications_in(self, region_code: str) -> list[tuple[str, str]]:
        return [(fid, code) for fid, code in self._applications if code == region_code]

    def _require_single_scheme(self, region_code: str) -> str:
        """区域级事件影响该区域全部申请；它们应共用同一评估方案。"""
        scheme_ids = {
            app.scheme_id for app in self._applications.values() if app.region.code == region_code
        }
        if not scheme_ids:
            raise KeyError(f"区域 {region_code} 尚未登记申请")
        if len(scheme_ids) > 1:
            raise ValueError(f"区域 {region_code} 的申请使用了多个方案，无法统一重算")
        return next(iter(scheme_ids))

    def _recompute_for_region(
        self, region_code: str, *, reason: str, multi: bool = False
    ):
        keys = self._applications_in(region_code)
        if not keys:
            raise KeyError(f"区域 {region_code} 尚未登记申请")
        results = [self._recompute(fid, code, reason=reason) for fid, code in keys]
        return results if multi else results[0]

    def _is_major(self, event: SafetyEvent, scheme: AssessmentScheme) -> bool:
        from .engine import severity_at_least

        return severity_at_least(event.severity, scheme.major_event_severity)

    def _recompute(
        self,
        fleet_id: str,
        region_code: str,
        *,
        reason: str,
        as_of: datetime | None = None,
    ) -> CandidateConclusion:
        key = (fleet_id, region_code)
        app = self._applications.get(key)
        if app is None:
            raise KeyError(f"申请 {fleet_id}/{region_code} 未登记")
        scheme = self.registry.latest_version_at(app.scheme_id, as_of or self._clock())
        inputs = EvaluationInputs(
            scheme=scheme,
            profile=app.profile,
            region=app.region,
            as_of=as_of or self._clock(),
            observations=list(self._observations.get(region_code, [])),
            events=list(self._events.get(region_code, [])),
            commitments=list(self._commitments.get(key, [])),
            infrastructure=list(self._infrastructure.get(region_code, [])),
            exemptions=list(self._exemptions.get(region_code, [])),
        )
        conclusion = evaluate(inputs)
        self._candidate_history.setdefault(key, []).append(
            CandidateRecord(reason=reason, conclusion=conclusion)
        )
        return conclusion
