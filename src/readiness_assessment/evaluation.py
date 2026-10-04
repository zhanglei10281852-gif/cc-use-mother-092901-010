"""候选结论评估引擎：按方案版本与观测状态在指定时刻计算的纯函数。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Iterable, Mapping

from .exemptions import Exemption
from .observations import InfrastructureReading, MetricObservation, SafetyEvent
from .plan import (
    Aggregation,
    AssessmentPlan,
    MetricDefinition,
    ObservationWindow,
    OperationRegion,
    ThresholdDirection,
    VehicleCapability,
)


class ConclusionOutcome(StrEnum):
    """候选结论：可进入下阶段 / 被阻塞 / 数据不足无法判定。"""

    READY = "ready"
    BLOCKED = "blocked"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class Evidence:
    """支撑结论的证据条目。"""

    kind: str  # metric / event / rectification / infrastructure
    subject: str
    detail: str


@dataclass(frozen=True)
class ExceptionNote:
    """结论中的例外：数据缺口、生效中的豁免、已到期的豁免。"""

    kind: str  # data_gap / exemption_applied / exemption_expired
    subject: str
    detail: str
    reference_id: str | None = None


@dataclass(frozen=True)
class Blocker:
    """阻塞进入下阶段的原因。"""

    code: str  # data_gap / metric_threshold / critical_event / rectification_overdue / infrastructure_unmet
    subject: str
    detail: str


@dataclass(frozen=True)
class CandidateConclusion:
    """一次评估计算得到的候选结论；每次重算产生新记录，历史不可改写。"""

    conclusion_id: str
    plan_id: str
    plan_version: int
    region_code: str
    capability_code: str
    computed_at: datetime
    outcome: ConclusionOutcome
    evidence: tuple[Evidence, ...]
    exceptions: tuple[ExceptionNote, ...]
    blockers: tuple[Blocker, ...]


def _aggregate(metric: MetricDefinition, points: list[MetricObservation]) -> float:
    if metric.aggregation is Aggregation.LATEST:
        return max(points, key=lambda p: p.observed_at).value
    if metric.aggregation is Aggregation.MAX:
        return max(p.value for p in points)
    return sum(p.value for p in points) / len(points)


def _fmt_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    return f"{seconds} 秒"


def _data_gap(window: ObservationWindow, points: list[MetricObservation], as_of: datetime) -> str | None:
    """识别数据缺口：观测不足或数据源超过更新周期未刷新。"""
    if not points:
        return f"窗口 {window.window_code} 内没有任何观测数据"
    required = window.required_updates()
    if len(points) < required:
        return f"窗口内观测 {len(points)} 次，低于最低要求的 {required} 次"
    latest = max(p.observed_at for p in points)
    if as_of - latest > window.update_cycle:
        return f"最新观测停留在 {latest.isoformat()}，已超过更新周期 {_fmt_delta(window.update_cycle)}"
    return None


def _find_exemption(
    exemptions: tuple[Exemption, ...],
    metric_code: str,
    region_code: str,
    capability_code: str,
    as_of: datetime,
    *,
    active: bool,
) -> Exemption | None:
    for exemption in exemptions:
        if exemption.active_at(as_of) != active:
            continue
        if exemption.covers(metric_code, region_code, capability_code):
            return exemption
    return None


def _latest_reading(
    readings: Iterable[InfrastructureReading],
    condition_code: str,
    region_code: str,
    as_of: datetime,
) -> InfrastructureReading | None:
    candidates = [
        r
        for r in readings
        if r.condition_code == condition_code and r.region_code == region_code and r.updated_at <= as_of
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.updated_at)


def evaluate_capability(
    *,
    conclusion_id: str,
    plan: AssessmentPlan,
    region: OperationRegion,
    capability: VehicleCapability,
    as_of: datetime,
    observations: Iterable[MetricObservation],
    events: Iterable[SafetyEvent],
    readings: Iterable[InfrastructureReading],
    fulfillments: Mapping[str, datetime],
    exemptions: Iterable[Exemption],
) -> CandidateConclusion:
    """在 as_of 时刻评估某区域某能力的候选结论（不改动任何已有记录）。"""
    observations = tuple(observations)
    events = tuple(events)
    readings = tuple(readings)
    exemptions = tuple(exemptions)

    evidence: list[Evidence] = []
    exceptions: list[ExceptionNote] = []
    blockers: list[Blocker] = []

    # 指标阈值（滚动窗口聚合，区域差异化阈值，豁免只在期限内生效）
    for metric_code in capability.required_metrics:
        metric = plan.metric(metric_code)
        window = plan.window(metric.window_code)
        span = window.span(as_of)
        points = [
            o
            for o in observations
            if o.metric_code == metric_code
            and o.region_code == region.region_code
            and o.capability_code == capability.capability_code
            and span.starts_at < o.observed_at <= span.ends_at
        ]
        gap = _data_gap(window, points, as_of)
        if gap is not None:
            exceptions.append(ExceptionNote("data_gap", metric_code, gap))
            blockers.append(Blocker("data_gap", metric_code, gap))
            continue

        value = _aggregate(metric, points)
        threshold = region.threshold_for(metric)
        direction = "<=" if metric.direction is ThresholdDirection.UPPER_BOUND else ">="
        evidence.append(
            Evidence(
                "metric",
                metric_code,
                f"窗口[{span.starts_at.isoformat()} ~ {span.ends_at.isoformat()}]聚合值 {value:.6g}，"
                f"阈值 {direction} {threshold:.6g}（{len(points)} 次观测）",
            )
        )
        if metric.satisfied_by(value, threshold):
            continue

        active = _find_exemption(exemptions, metric_code, region.region_code, capability.capability_code, as_of, active=True)
        if active is not None:
            exceptions.append(
                ExceptionNote(
                    "exemption_applied",
                    metric_code,
                    f"指标越限但被豁免 {active.exemption_id} 覆盖，豁免至 {active.expires_at.isoformat()} 到期",
                    active.exemption_id,
                )
            )
            continue

        expired = _find_exemption(exemptions, metric_code, region.region_code, capability.capability_code, as_of, active=False)
        if expired is not None:
            exceptions.append(
                ExceptionNote(
                    "exemption_expired",
                    metric_code,
                    f"豁免 {expired.exemption_id} 已于 {expired.expires_at.isoformat()} 到期，越限重新暴露",
                    expired.exemption_id,
                )
            )
        blockers.append(
            Blocker("metric_threshold", metric_code, f"聚合值 {value:.6g} 未满足阈值 {direction} {threshold:.6g}")
        )

    # 重大安全事件（不可豁免）
    lookback_start = as_of - plan.severity_policy.lookback
    for event in events:
        if (
            event.region_code == region.region_code
            and event.capability_code == capability.capability_code
            and lookback_start < event.occurred_at <= as_of
            and event.severity in plan.severity_policy.blocking_severities
        ):
            evidence.append(Evidence("event", event.event_id, f"{event.severity.value} 事件：{event.summary}"))
            blockers.append(
                Blocker(
                    "critical_event",
                    event.event_id,
                    f"{event.severity.value} 安全事件发生于 {event.occurred_at.isoformat()}，处于回溯期内",
                )
            )

    # 整改承诺
    for rect in plan.rectifications:
        if rect.region_code != region.region_code or rect.capability_code != capability.capability_code:
            continue
        fulfilled_at = fulfillments.get(rect.commitment_id)
        if fulfilled_at is not None and fulfilled_at <= as_of:
            evidence.append(Evidence("rectification", rect.commitment_id, f"整改已于 {fulfilled_at.isoformat()} 履约"))
        elif rect.due_at < as_of:
            blockers.append(
                Blocker(
                    "rectification_overdue",
                    rect.commitment_id,
                    f"整改承诺逾期（截止 {rect.due_at.isoformat()}）：{rect.summary}",
                )
            )
        else:
            evidence.append(Evidence("rectification", rect.commitment_id, f"整改进行中，截止 {rect.due_at.isoformat()}"))

    # 基础设施条件（读数缺失或超期视为数据缺口）
    for cond in plan.infrastructure:
        if cond.region_code != region.region_code:
            continue
        reading = _latest_reading(readings, cond.condition_code, cond.region_code, as_of)
        gap: str | None = None
        if reading is None:
            gap = f"基础设施 {cond.condition_code} 在 {as_of.isoformat()} 前没有任何读数"
        elif as_of - reading.updated_at > cond.update_cycle:
            gap = f"基础设施 {cond.condition_code} 读数停留在 {reading.updated_at.isoformat()}，已超过更新周期 {_fmt_delta(cond.update_cycle)}"
        if gap is not None:
            exceptions.append(ExceptionNote("data_gap", cond.condition_code, gap))
            blockers.append(Blocker("data_gap", cond.condition_code, gap))
            continue
        assert reading is not None
        evidence.append(
            Evidence(
                "infrastructure",
                cond.condition_code,
                f"覆盖率 {reading.observed_ratio:.4f}，要求 >= {cond.required_ratio:.4f}",
            )
        )
        if reading.observed_ratio < cond.required_ratio:
            blockers.append(
                Blocker(
                    "infrastructure_unmet",
                    cond.condition_code,
                    f"覆盖率 {reading.observed_ratio:.4f} 低于要求的 {cond.required_ratio:.4f}",
                )
            )

    if any(b.code == "data_gap" for b in blockers):
        outcome = ConclusionOutcome.INSUFFICIENT_DATA
    elif blockers:
        outcome = ConclusionOutcome.BLOCKED
    else:
        outcome = ConclusionOutcome.READY

    return CandidateConclusion(
        conclusion_id=conclusion_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        region_code=region.region_code,
        capability_code=capability.capability_code,
        computed_at=as_of,
        outcome=outcome,
        evidence=tuple(evidence),
        exceptions=tuple(exceptions),
        blockers=tuple(blockers),
    )
