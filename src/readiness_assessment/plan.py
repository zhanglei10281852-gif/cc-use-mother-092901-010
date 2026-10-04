"""有版本的评估方案：区域、能力、指标、窗口、严重度、整改与基础设施的规则快照。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from .contracts import MetricWindow


class ThresholdDirection(StrEnum):
    """指标阈值方向：上限（越低越好）或下限（越高越好）。"""

    UPPER_BOUND = "upper_bound"
    LOWER_BOUND = "lower_bound"


class Aggregation(StrEnum):
    """窗口内观测的聚合方式。"""

    MEAN = "mean"
    LATEST = "latest"
    MAX = "max"


class EventSeverity(StrEnum):
    """安全事件严重度。"""

    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class OperationStage(StrEnum):
    """商业化运营阶段。"""

    PILOT = "pilot"
    EXPANDED = "expanded"
    COMMERCIAL = "commercial"


@dataclass(frozen=True)
class ObservationWindow:
    """滚动观测窗口：窗口长度、数据更新周期与最低覆盖率。

    不同数据源（安全事件、接管率、设施覆盖、整改材料）按各自周期更新，
    窗口用 update_cycle 与 min_coverage 刻画每个数据源的更新节奏，
    评估时据此识别数据缺口。
    """

    window_code: str
    length: timedelta
    update_cycle: timedelta
    min_coverage: float = 0.8

    def __post_init__(self) -> None:
        if self.length <= timedelta(0):
            raise ValueError("观测窗口长度必须为正")
        if self.update_cycle <= timedelta(0):
            raise ValueError("数据更新周期必须为正")
        if not 0.0 < self.min_coverage <= 1.0:
            raise ValueError("最低覆盖率必须位于 (0, 1]")

    def span(self, as_of: datetime) -> MetricWindow:
        """as_of 时刻对应的滚动区间 (as_of - length, as_of]。"""
        return MetricWindow(as_of - self.length, as_of)

    def expected_updates(self) -> int:
        return max(1, int(self.length / self.update_cycle))

    def required_updates(self) -> int:
        """窗口内判定数据无缺口所需的最低观测次数。"""
        return max(1, math.ceil(self.expected_updates() * self.min_coverage))


@dataclass(frozen=True)
class MetricDefinition:
    """指标定义：阈值、方向、聚合方式与所属观测窗口。"""

    metric_code: str
    unit: str
    direction: ThresholdDirection
    threshold: float
    window_code: str
    aggregation: Aggregation = Aggregation.MEAN

    def satisfied_by(self, value: float, threshold: float | None = None) -> bool:
        limit = self.threshold if threshold is None else threshold
        if self.direction is ThresholdDirection.UPPER_BOUND:
            return value <= limit
        return value >= limit


@dataclass(frozen=True)
class OperationRegion:
    """运营区域，可按区域覆盖指标阈值（跨区域差异）。"""

    region_code: str
    name: str
    threshold_overrides: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "threshold_overrides", MappingProxyType(dict(self.threshold_overrides)))

    def threshold_for(self, metric: MetricDefinition) -> float:
        """区域差异化阈值：无覆盖时回退到指标默认阈值。"""
        return self.threshold_overrides.get(metric.metric_code, metric.threshold)


@dataclass(frozen=True)
class VehicleCapability:
    """车辆能力及其准入所需指标。"""

    capability_code: str
    name: str
    required_metrics: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_metrics", tuple(self.required_metrics))
        if not self.required_metrics:
            raise ValueError("车辆能力必须声明至少一项准入指标")


@dataclass(frozen=True)
class SeverityPolicy:
    """事件严重度策略：哪些级别构成阻断、回溯多长时间。"""

    blocking_severities: frozenset[EventSeverity]
    lookback: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocking_severities", frozenset(self.blocking_severities))
        if not self.blocking_severities:
            raise ValueError("严重度策略必须至少包含一个阻断级别")
        if self.lookback <= timedelta(0):
            raise ValueError("事件回溯时长必须为正")


@dataclass(frozen=True)
class RectificationCommitment:
    """企业整改承诺：限定区域与能力，带有履约截止时刻。"""

    commitment_id: str
    region_code: str
    capability_code: str
    summary: str
    due_at: datetime


@dataclass(frozen=True)
class InfrastructureCondition:
    """基础设施条件：区域覆盖率要求与读数更新周期。"""

    condition_code: str
    region_code: str
    required_ratio: float
    update_cycle: timedelta

    def __post_init__(self) -> None:
        if not 0.0 < self.required_ratio <= 1.0:
            raise ValueError("基础设施要求覆盖率必须位于 (0, 1]")
        if self.update_cycle <= timedelta(0):
            raise ValueError("基础设施数据更新周期必须为正")


def _ensure_unique(codes: list[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for code in codes:
        if code in seen:
            duplicates.add(code)
        seen.add(code)
    if duplicates:
        raise ValueError(f"{label}编码重复: {sorted(duplicates)}")


@dataclass(frozen=True)
class AssessmentPlan:
    """有版本的评估方案。

    每次规则换版（阈值、窗口、严重度策略等变化）产生一个新版本；
    旧版本及其历史结论保持可追溯，不被改写。
    """

    plan_id: str
    version: int
    effective_from: datetime
    regions: tuple[OperationRegion, ...]
    capabilities: tuple[VehicleCapability, ...]
    metrics: tuple[MetricDefinition, ...]
    windows: tuple[ObservationWindow, ...]
    severity_policy: SeverityPolicy
    rectifications: tuple[RectificationCommitment, ...] = ()
    infrastructure: tuple[InfrastructureCondition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "regions", tuple(self.regions))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "metrics", tuple(self.metrics))
        object.__setattr__(self, "windows", tuple(self.windows))
        object.__setattr__(self, "rectifications", tuple(self.rectifications))
        object.__setattr__(self, "infrastructure", tuple(self.infrastructure))
        if self.version < 1:
            raise ValueError("方案版本必须为正整数")
        self._check_integrity()

    def _check_integrity(self) -> None:
        _ensure_unique([r.region_code for r in self.regions], "运营区域")
        _ensure_unique([c.capability_code for c in self.capabilities], "车辆能力")
        _ensure_unique([m.metric_code for m in self.metrics], "指标")
        _ensure_unique([w.window_code for w in self.windows], "观测窗口")
        _ensure_unique([r.commitment_id for r in self.rectifications], "整改承诺")

        window_codes = {w.window_code for w in self.windows}
        metric_codes = {m.metric_code for m in self.metrics}
        region_codes = {r.region_code for r in self.regions}
        capability_codes = {c.capability_code for c in self.capabilities}

        for metric in self.metrics:
            if metric.window_code not in window_codes:
                raise ValueError(f"指标 {metric.metric_code} 引用了未定义的观测窗口 {metric.window_code}")
        for capability in self.capabilities:
            unknown = [code for code in capability.required_metrics if code not in metric_codes]
            if unknown:
                raise ValueError(f"能力 {capability.capability_code} 引用了未定义指标: {unknown}")
        for region in self.regions:
            unknown = [code for code in region.threshold_overrides if code not in metric_codes]
            if unknown:
                raise ValueError(f"区域 {region.region_code} 覆盖了未定义指标的阈值: {unknown}")
        for rect in self.rectifications:
            if rect.region_code not in region_codes:
                raise ValueError(f"整改承诺 {rect.commitment_id} 引用了未定义区域 {rect.region_code}")
            if rect.capability_code not in capability_codes:
                raise ValueError(f"整改承诺 {rect.commitment_id} 引用了未定义能力 {rect.capability_code}")
        for cond in self.infrastructure:
            if cond.region_code not in region_codes:
                raise ValueError(f"基础设施条件 {cond.condition_code} 引用了未定义区域 {cond.region_code}")

    def region(self, region_code: str) -> OperationRegion:
        for region in self.regions:
            if region.region_code == region_code:
                return region
        raise KeyError(f"方案 {self.plan_id} 未定义运营区域 {region_code}")

    def capability(self, capability_code: str) -> VehicleCapability:
        for capability in self.capabilities:
            if capability.capability_code == capability_code:
                return capability
        raise KeyError(f"方案 {self.plan_id} 未定义车辆能力 {capability_code}")

    def metric(self, metric_code: str) -> MetricDefinition:
        for metric in self.metrics:
            if metric.metric_code == metric_code:
                return metric
        raise KeyError(f"方案 {self.plan_id} 未定义指标 {metric_code}")

    def window(self, window_code: str) -> ObservationWindow:
        for window in self.windows:
            if window.window_code == window_code:
                return window
        raise KeyError(f"方案 {self.plan_id} 未定义观测窗口 {window_code}")
