"""滚动窗口聚合与评估引擎（纯函数，无副作用）。

输入：方案版本、车辆档案、区域、观测样本、事件、整改承诺、
基础设施条件、豁免、评估时刻。
输出：不可变的 :class:`CandidateConclusion`，含每项指标的证据、
例外与下阶段阻塞原因。

引擎本身不持久化任何状态，因此"数据缺口、阈值变更、重大事件触发重算"
由服务层在不同输入下重新调用本引擎完成；历史候选结论保留不动，
已经签发的阶段决定更不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .domain import (
    Blocker,
    Evidence,
    ExceptionRecord,
    Exemption,
    InfrastructureCondition,
    ItemStatus,
    MetricDefinition,
    MetricDirection,
    MetricFinding,
    Observation,
    OperatingRegion,
    RemediationCommitment,
    SafetyEvent,
    Severity,
    VehicleCapability,
    VehicleProfile,
)
from .scheme import AssessmentScheme

_SEVERITY_ORDER = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def severity_at_least(actual: Severity, threshold: Severity) -> bool:
    return _SEVERITY_ORDER[actual] >= _SEVERITY_ORDER[threshold]


# ---------------------------------------------------------------------------
# 滚动窗口
# ---------------------------------------------------------------------------
def rolling_window(
    metric: MetricDefinition, as_of: datetime
) -> tuple[datetime, datetime]:
    """返回以 ``as_of`` 为右端点（不含）的滚动观测窗口 [start, end)。"""
    return as_of - metric.window_duration, as_of


def observations_in_window(
    observations: list[Observation],
    metric_code: str,
    region_code: str,
    starts_at: datetime,
    ends_at: datetime,
) -> list[Observation]:
    """筛选落在 [starts_at, ends_at) 内、且区域与指标匹配的样本。"""
    return [
        obs
        for obs in observations
        if obs.metric_code == metric_code
        and obs.region_code == region_code
        and starts_at <= obs.observed_at < ends_at
    ]


def aggregate(samples: list[Observation]) -> float | None:
    """窗口聚合：无样本即数据缺口；有样本取算术平均。

    事件率、接管率等比率指标以窗口内各上报周期样本的均值作为口径；
    样本量（sample_count）同时进入证据，供委员会判断统计充分性。
    """
    if not samples:
        return None
    return sum(s.value for s in samples) / len(samples)


# ---------------------------------------------------------------------------
# 候选结论
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CandidateConclusion:
    """一次评估计算的完整结果（候选结论，尚未成为阶段决定）。"""

    scheme_id: str
    scheme_version: int
    fleet_id: str
    region_code: str
    evaluated_at: datetime
    findings: tuple[MetricFinding, ...]
    evidence: tuple[Evidence, ...]
    exceptions: tuple[ExceptionRecord, ...]
    blockers: tuple[Blocker, ...]
    ready: bool

    def finding(self, metric_code: str) -> MetricFinding:
        for item in self.findings:
            if item.metric_code == metric_code:
                return item
        raise KeyError(metric_code)

    def to_dict(self) -> dict:
        return {
            "scheme_id": self.scheme_id,
            "scheme_version": self.scheme_version,
            "fleet_id": self.fleet_id,
            "region_code": self.region_code,
            "evaluated_at": self.evaluated_at.isoformat(),
            "ready": self.ready,
            "findings": [f.to_dict() for f in self.findings],
            "evidence": [e.to_dict() for e in self.evidence],
            "exceptions": [e.to_dict() for e in self.exceptions],
            "blockers": [b.to_dict() for b in self.blockers],
        }


@dataclass(frozen=True)
class EvaluationInputs:
    """一次评估的全部输入，打包便于在服务层缓存与比较。"""

    scheme: AssessmentScheme
    profile: VehicleProfile
    region: OperatingRegion
    as_of: datetime
    observations: list[Observation] = field(default_factory=list)
    events: list[SafetyEvent] = field(default_factory=list)
    commitments: list[RemediationCommitment] = field(default_factory=list)
    infrastructure: list[InfrastructureCondition] = field(default_factory=list)
    exemptions: list[Exemption] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.profile.region_code != self.region.code:
            raise ValueError("车辆档案所属区域与评估区域不一致")
        if self.as_of.tzinfo is None:
            raise ValueError("评估时刻必须带时区")


# ---------------------------------------------------------------------------
# 核心评估
# ---------------------------------------------------------------------------
def _meets(value: float, threshold: float, direction: MetricDirection) -> bool:
    if direction is MetricDirection.MAXIMUM:
        return value <= threshold
    return value >= threshold


def evaluate(inputs: EvaluationInputs) -> CandidateConclusion:
    """根据方案版本与全部输入计算候选结论。"""
    scheme = inputs.scheme
    profile = inputs.profile
    region = inputs.region
    as_of = inputs.as_of

    findings: list[MetricFinding] = []
    evidence: list[Evidence] = []
    exceptions: list[ExceptionRecord] = []
    blockers: list[Blocker] = []

    applicable_metrics = scheme.metrics_for(profile.capabilities)

    # --- 方案覆盖完整性：每项声明能力至少被一个指标考核 ----------------
    assessed_capabilities: set[VehicleCapability] = set()
    for applicable in applicable_metrics:
        assessed_capabilities |= applicable.capabilities
    for capability in sorted(profile.capabilities, key=lambda c: c.value):
        if capability not in assessed_capabilities:
            blockers.append(
                Blocker(
                    code="scheme-coverage",
                    capability=capability.value,
                    reason=f"当前方案版本未定义任何考核能力 {capability.value} 的指标，无法形成准入依据",
                )
            )

    # --- 指标逐项判定（滚动窗口 / 基础设施快照 / 豁免） ------------------
    for metric in applicable_metrics:
        threshold = region.threshold_for(metric.code, metric.default_threshold)
        starts_at, ends_at = rolling_window(metric, as_of)

        if metric.infrastructure:
            value, sample_count = _infrastructure_value(inputs.infrastructure, metric, region.code, as_of)
        else:
            samples = observations_in_window(
                inputs.observations, metric.code, region.code, starts_at, ends_at
            )
            value = aggregate(samples)
            sample_count = len(samples)

        evidence.append(
            Evidence(
                metric_code=metric.code,
                window_days=metric.window_days,
                sample_count=sample_count,
                observed_value=value,
                threshold=threshold,
                window_starts_at=starts_at,
                window_ends_at=ends_at,
            )
        )

        active_exemption = _find_active_exemption(
            inputs.exemptions, metric.code, profile.capabilities, region.code, as_of
        )

        if value is None:
            status = ItemStatus.DATA_GAP
            blockers.append(
                Blocker(
                    code=metric.code,
                    capability=_capability_label(metric.capabilities),
                    reason=f"指标 {metric.code} 在最近 {metric.window_days} 天窗口内无观测样本，存在数据缺口",
                )
            )
        elif active_exemption is not None:
            status = ItemStatus.EXEMPTED
            exceptions.append(
                ExceptionRecord(
                    kind="exemption",
                    reference=active_exemption.exemption_id,
                    capability=_capability_label(metric.capabilities),
                    detail=(
                        f"指标 {metric.code} 在豁免有效期内"
                        f"（至 {active_exemption.valid_until.isoformat()} 到期）；"
                        f"观测值 {value:.4g} 对照阈值 {threshold:g}"
                    ),
                    active=True,
                )
            )
        elif _meets(value, threshold, metric.direction):
            status = ItemStatus.PASS
        else:
            status = ItemStatus.FAIL
            blockers.append(
                Blocker(
                    code=metric.code,
                    capability=_capability_label(metric.capabilities),
                    reason=(
                        f"指标 {metric.code} 观测值 {value:.4g} "
                        f"{'超过' if metric.direction is MetricDirection.MAXIMUM else '低于'}"
                        f"阈值 {threshold:g}"
                    ),
                )
            )

        findings.append(
            MetricFinding(
                metric_code=metric.code,
                capabilities=metric.capabilities,
                status=status,
                observed_value=value,
                threshold=threshold,
                window_days=metric.window_days,
                exemption_id=active_exemption.exemption_id if active_exemption else None,
            )
        )

    # --- 已到期豁免：自动暴露受影响能力 --------------------------------
    for exemption in inputs.exemptions:
        if exemption.region_code != region.code:
            continue
        if not (exemption.capabilities & profile.capabilities):
            continue
        if exemption.is_active(as_of):
            continue
        metric = _optional_metric(scheme, exemption.metric_code)
        if metric is None or not (not metric.capabilities or metric.capabilities & profile.capabilities):
            continue
        exceptions.append(
            ExceptionRecord(
                kind="exemption",
                reference=exemption.exemption_id,
                capability=_capability_label(exemption.capabilities),
                detail=(
                    f"豁免已于 {exemption.valid_until.isoformat()} 到期，"
                    f"受影响能力恢复按指标 {exemption.metric_code} 常规阈值考核"
                ),
                active=False,
            )
        )
        # 到期豁免不再掩盖失败：若该指标当前未通过，确保形成阻塞。
        finding = next((f for f in findings if f.metric_code == exemption.metric_code), None)
        if finding is not None and finding.status in (ItemStatus.FAIL, ItemStatus.DATA_GAP):
            _ensure_blocker(
                blockers,
                exemption.metric_code,
                _capability_label(exemption.capabilities),
                f"豁免 {exemption.exemption_id} 已到期，受影响能力指标 {exemption.metric_code} "
                + ("存在数据缺口" if finding.status is ItemStatus.DATA_GAP else "未达标"),
            )

    # --- 重大安全事件 ---------------------------------------------------
    for event in inputs.events:
        if event.region_code != region.code:
            continue
        if event.capability not in profile.capabilities:
            continue
        if not severity_at_least(event.severity, scheme.major_event_severity):
            continue
        # 重大事件一律不可豁免：即使存在匹配豁免，也只记录为已失效的例外。
        matching_exemption = _find_active_exemption(
            inputs.exemptions,
            _event_metric_codes(applicable_metrics, event.capability),
            profile.capabilities,
            region.code,
            as_of,
        )
        exceptions.append(
            ExceptionRecord(
                kind="major-event",
                reference=event.event_id,
                capability=event.capability.value,
                detail=(
                    f"{event.severity.value} 级安全事件发生于 {event.occurred_at.isoformat()}"
                    + ("；该能力存在豁免但重大事件不可豁免" if matching_exemption else "")
                    + (f"：{event.description}" if event.description else "")
                ),
                active=True,
            )
        )
        blockers.append(
            Blocker(
                code="major-event",
                capability=event.capability.value,
                reason=f"重大安全事件 {event.event_id}（{event.severity.value}）未完成专项整改前不得进入下一阶段",
            )
        )

    # --- 整改承诺 -------------------------------------------------------
    for commitment in inputs.commitments:
        if commitment.capability not in profile.capabilities:
            continue
        deadline = scheme.remediation_deadline(commitment.due_at)
        if commitment.is_closed:
            continue
        if as_of > deadline:
            blockers.append(
                Blocker(
                    code="remediation-overdue",
                    capability=commitment.capability.value,
                    reason=(
                        f"整改承诺 {commitment.commitment_id} 已于 "
                        f"{deadline.isoformat()} 逾期未闭环"
                    ),
                )
            )
        else:
            blockers.append(
                Blocker(
                    code="remediation-open",
                    capability=commitment.capability.value,
                    reason=(
                        f"整改承诺 {commitment.commitment_id} 尚未闭环，"
                        f"期限 {deadline.isoformat()}"
                    ),
                )
            )

    ready = not blockers
    return CandidateConclusion(
        scheme_id=scheme.scheme_id,
        scheme_version=scheme.version,
        fleet_id=profile.fleet_id,
        region_code=region.code,
        evaluated_at=as_of,
        findings=tuple(findings),
        evidence=tuple(evidence),
        exceptions=tuple(exceptions),
        blockers=tuple(blockers),
        ready=ready,
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _infrastructure_value(
    conditions: list[InfrastructureCondition],
    metric: MetricDefinition,
    region_code: str,
    as_of: datetime,
) -> tuple[float | None, int]:
    """取评估时刻之前最新一条同编码基础设施快照；无快照即数据缺口。"""
    candidates = [
        cond
        for cond in conditions
        if cond.code == metric.code
        and cond.region_code == region_code
        and cond.measured_at <= as_of
    ]
    if not candidates:
        return None, 0
    latest = max(candidates, key=lambda c: c.measured_at)
    return latest.coverage_ratio, 1


def _find_active_exemption(
    exemptions: list[Exemption],
    metric_code: str | list[str],
    capabilities: frozenset[VehicleCapability],
    region_code: str,
    as_of: datetime,
) -> Exemption | None:
    codes = {metric_code} if isinstance(metric_code, str) else set(metric_code)
    for exemption in exemptions:
        if (
            exemption.metric_code in codes
            and exemption.region_code == region_code
            and exemption.capabilities & capabilities
            and exemption.is_active(as_of)
        ):
            return exemption
    return None


def _event_metric_codes(
    metrics: list[MetricDefinition], capability: VehicleCapability
) -> list[str]:
    return [m.code for m in metrics if not m.capabilities or capability in m.capabilities]


def _optional_metric(scheme: AssessmentScheme, code: str) -> MetricDefinition | None:
    try:
        return scheme.metric(code)
    except Exception:
        return None


def _capability_label(capabilities: frozenset[VehicleCapability]) -> str:
    if not capabilities:
        return "*"
    return ",".join(sorted(c.value for c in capabilities))


def _ensure_blocker(
    blockers: list[Blocker], code: str, capability: str, reason: str
) -> None:
    if not any(b.code == code and b.capability == capability and b.reason == reason for b in blockers):
        blockers.append(Blocker(code=code, capability=capability, reason=reason))
