"""商业化运营准入评估服务：状态管理、重算触发、委员会协作与查询接口。"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime

from .decisions import Committee, StageDecision
from .evaluation import CandidateConclusion, evaluate_capability
from .exemptions import Exemption
from .observations import InfrastructureReading, MetricObservation, SafetyEvent
from .plan import AssessmentPlan, OperationStage


@dataclass(frozen=True)
class ExposureEntry:
    """豁免到期后重新暴露的能力项。"""

    exemption_id: str
    region_code: str
    capability_code: str
    exposed_metrics: tuple[str, ...]
    currently_blocking: bool


class AssessmentService:
    """准入评估服务。

    数据缺口、阈值变更（规则换版）或重大事件发生时重算候选结论；
    已签发的阶段决定由委员会保管，任何重算都不会改写它。
    """

    def __init__(self, committee: Committee) -> None:
        self.committee = committee
        self._lock = threading.RLock()
        self._plans: dict[str, list[AssessmentPlan]] = {}
        self._observations: list[MetricObservation] = []
        self._events: list[SafetyEvent] = []
        self._readings: list[InfrastructureReading] = []
        self._fulfillments: dict[str, datetime] = {}
        self._exemptions: dict[str, Exemption] = {}
        self._conclusions: list[CandidateConclusion] = []
        self._seq = 0

    # ---- 方案版本（规则换版） ----

    def register_plan(self, plan: AssessmentPlan, as_of: datetime | None = None) -> list[CandidateConclusion]:
        """登记新版本方案并按新版本重算全部候选结论。"""
        with self._lock:
            versions = self._plans.setdefault(plan.plan_id, [])
            if versions and plan.version <= versions[-1].version:
                raise ValueError(f"方案 {plan.plan_id} 的版本必须递增，当前最新为 v{versions[-1].version}")
            versions.append(plan)
            return self._refresh_all(plan, as_of or plan.effective_from)

    def plan(self, plan_id: str, version: int | None = None) -> AssessmentPlan:
        versions = self._plans.get(plan_id)
        if not versions:
            raise KeyError(f"未登记的评估方案 {plan_id}")
        if version is None:
            return versions[-1]
        for plan in versions:
            if plan.version == version:
                return plan
        raise KeyError(f"方案 {plan_id} 没有版本 v{version}")

    @property
    def latest_plan(self) -> AssessmentPlan | None:
        latest = [versions[-1] for versions in self._plans.values() if versions]
        if not latest:
            return None
        return max(latest, key=lambda p: (p.effective_from, p.version))

    # ---- 运行数据写入（触发重算） ----

    def record_observation(self, observation: MetricObservation) -> list[CandidateConclusion]:
        with self._lock:
            self._observations.append(observation)
            return self._refresh(observation.region_code, observation.capability_code, observation.observed_at)

    def record_event(self, event: SafetyEvent) -> list[CandidateConclusion]:
        """重大安全事件立即触发相关候选结论重算。"""
        with self._lock:
            self._events.append(event)
            plan = self.latest_plan
            if plan is None or event.severity not in plan.severity_policy.blocking_severities:
                return []
            return self._refresh(event.region_code, event.capability_code, event.occurred_at)

    def record_infrastructure(self, reading: InfrastructureReading) -> list[CandidateConclusion]:
        with self._lock:
            self._readings.append(reading)
            plan = self.latest_plan
            if plan is None:
                return []
            results: list[CandidateConclusion] = []
            for capability in plan.capabilities:
                results.extend(self._refresh(reading.region_code, capability.capability_code, reading.updated_at))
            return results

    def fulfill_rectification(self, commitment_id: str, at: datetime) -> list[CandidateConclusion]:
        with self._lock:
            plan = self.latest_plan
            match = [r for r in plan.rectifications if r.commitment_id == commitment_id] if plan else []
            if not match:
                raise KeyError(f"当前方案未包含整改承诺 {commitment_id}")
            self._fulfillments[commitment_id] = at
            return self._refresh(match[0].region_code, match[0].capability_code, at)

    def grant_exemption(self, exemption: Exemption) -> list[CandidateConclusion]:
        """授予限定范围与期限的豁免，并重算受影响的候选结论。"""
        with self._lock:
            self._exemptions[exemption.exemption_id] = exemption
            results: list[CandidateConclusion] = []
            for region_code in exemption.region_codes:
                for capability_code in exemption.capability_codes:
                    results.extend(self._refresh(region_code, capability_code, exemption.granted_at))
            return results

    # ---- 评估 ----

    def evaluate(
        self,
        plan_id: str,
        region_code: str,
        capability_code: str,
        as_of: datetime,
        *,
        plan_version: int | None = None,
        store: bool = True,
    ) -> CandidateConclusion:
        """在 as_of 时刻计算候选结论；默认留档，历史结论不被改写。"""
        with self._lock:
            plan = self.plan(plan_id, plan_version)
            region = plan.region(region_code)
            capability = plan.capability(capability_code)
            self._seq += 1
            conclusion = evaluate_capability(
                conclusion_id=f"C-{self._seq:05d}",
                plan=plan,
                region=region,
                capability=capability,
                as_of=as_of,
                observations=tuple(self._observations),
                events=tuple(self._events),
                readings=tuple(self._readings),
                fulfillments=dict(self._fulfillments),
                exemptions=tuple(self._exemptions.values()),
            )
            if store:
                self._conclusions.append(conclusion)
            return conclusion

    def evaluate_all(self, plan_id: str, as_of: datetime) -> list[CandidateConclusion]:
        plan = self.plan(plan_id)
        return [
            self.evaluate(plan_id, region.region_code, capability.capability_code, as_of)
            for region in plan.regions
            for capability in plan.capabilities
        ]

    def refresh_all(self, as_of: datetime) -> list[CandidateConclusion]:
        """按指定时刻重算最新方案下全部候选结论（暴露数据缺口等时间驱动变化）。"""
        plan = self.latest_plan
        if plan is None:
            return []
        return self.evaluate_all(plan.plan_id, as_of)

    def _refresh(self, region_code: str, capability_code: str, as_of: datetime) -> list[CandidateConclusion]:
        plan = self.latest_plan
        if plan is None:
            return []
        try:
            return [self.evaluate(plan.plan_id, region_code, capability_code, as_of)]
        except KeyError:
            return []

    def _refresh_all(self, plan: AssessmentPlan, as_of: datetime) -> list[CandidateConclusion]:
        return [
            self.evaluate(plan.plan_id, region.region_code, capability.capability_code, as_of)
            for region in plan.regions
            for capability in plan.capabilities
        ]

    # ---- 历史与豁免到期暴露 ----

    def conclusion(self, conclusion_id: str) -> CandidateConclusion:
        for conclusion in self._conclusions:
            if conclusion.conclusion_id == conclusion_id:
                return conclusion
        raise KeyError(f"未知结论编号 {conclusion_id}")

    def conclusion_history(self, region_code: str, capability_code: str) -> tuple[CandidateConclusion, ...]:
        return tuple(
            c for c in self._conclusions if c.region_code == region_code and c.capability_code == capability_code
        )

    def exposure_report(self, as_of: datetime) -> list[ExposureEntry]:
        """列出截至 as_of 已到期的豁免及其重新暴露的能力。"""
        with self._lock:
            plan = self.latest_plan
            if plan is None:
                return []
            entries: list[ExposureEntry] = []
            for exemption in self._exemptions.values():
                if exemption.active_at(as_of):
                    continue
                for region_code in exemption.region_codes:
                    for capability_code in exemption.capability_codes:
                        try:
                            conclusion = self.evaluate(plan.plan_id, region_code, capability_code, as_of, store=False)
                        except KeyError:
                            continue
                        exposed = tuple(
                            code
                            for code in exemption.metric_codes
                            if any(b.code == "metric_threshold" and b.subject == code for b in conclusion.blockers)
                        )
                        entries.append(
                            ExposureEntry(
                                exemption_id=exemption.exemption_id,
                                region_code=region_code,
                                capability_code=capability_code,
                                exposed_metrics=exposed,
                                currently_blocking=bool(exposed),
                            )
                        )
            return entries

    # ---- 委员会协作 ----

    def propose_decision(
        self,
        *,
        conclusion_id: str,
        target_stage: OperationStage,
        proposed_by: str,
        proposed_at: datetime,
        decision_id: str | None = None,
        reason: str = "",
    ) -> StageDecision:
        conclusion = self.conclusion(conclusion_id)
        return self.committee.propose(
            decision_id=decision_id or f"D-{conclusion_id}",
            plan_id=conclusion.plan_id,
            plan_version=conclusion.plan_version,
            region_code=conclusion.region_code,
            capability_code=conclusion.capability_code,
            target_stage=target_stage,
            conclusion_id=conclusion.conclusion_id,
            proposed_by=proposed_by,
            proposed_at=proposed_at,
            reason=reason,
        )

    def countersign(self, decision_id: str, member: str, at: datetime) -> StageDecision:
        return self.committee.countersign(decision_id, member, at)

    def reject(self, decision_id: str, member: str, reason: str, at: datetime) -> StageDecision:
        return self.committee.reject(decision_id, member, reason, at)

    def withdraw(self, decision_id: str, member: str, at: datetime) -> StageDecision:
        return self.committee.withdraw(decision_id, member, at)

    def decision(self, decision_id: str) -> StageDecision:
        return self.committee.get(decision_id)

    # ---- 查询接口：展示证据、例外与下阶段阻塞原因 ----

    def conclusion_view(self, conclusion_id: str) -> dict:
        conclusion = self.conclusion(conclusion_id)
        return {
            "conclusion_id": conclusion.conclusion_id,
            "plan_id": conclusion.plan_id,
            "plan_version": conclusion.plan_version,
            "region_code": conclusion.region_code,
            "capability_code": conclusion.capability_code,
            "computed_at": conclusion.computed_at.isoformat(),
            "outcome": conclusion.outcome.value,
            "evidence": [asdict(e) for e in conclusion.evidence],
            "exceptions": [asdict(e) for e in conclusion.exceptions],
            "blockers": [asdict(b) for b in conclusion.blockers],
        }

    def decision_view(self, decision_id: str) -> dict:
        decision = self.committee.get(decision_id)
        return {
            "decision_id": decision.decision_id,
            "status": decision.status.value,
            "plan_id": decision.plan_id,
            "plan_version": decision.plan_version,
            "region_code": decision.region_code,
            "capability_code": decision.capability_code,
            "target_stage": decision.target_stage.value,
            "conclusion_id": decision.conclusion_id,
            "proposed_by": decision.proposed_by,
            "proposed_at": decision.proposed_at.isoformat(),
            "signatures": list(decision.signatures),
            "reason": decision.reason,
            "resolved_at": decision.resolved_at.isoformat() if decision.resolved_at else None,
            "conclusion": self.conclusion_view(decision.conclusion_id),
        }

    def board(self, plan_id: str, as_of: datetime) -> dict:
        """委员会看板：重算每个区域×能力的候选结论并附最新决定状态。"""
        conclusions = self.evaluate_all(plan_id, as_of)
        plan = self.plan(plan_id)
        latest_decisions: dict[tuple[str, str], StageDecision] = {}
        for decision in self.committee.decisions():
            key = (decision.region_code, decision.capability_code)
            current = latest_decisions.get(key)
            if current is None or (decision.resolved_at or decision.proposed_at) >= (
                current.resolved_at or current.proposed_at
            ):
                latest_decisions[key] = decision
        items = []
        for conclusion in conclusions:
            view = self.conclusion_view(conclusion.conclusion_id)
            decision = latest_decisions.get((conclusion.region_code, conclusion.capability_code))
            view["decision"] = (
                {"decision_id": decision.decision_id, "status": decision.status.value} if decision else None
            )
            items.append(view)
        return {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "as_of": as_of.isoformat(),
            "items": items,
        }
