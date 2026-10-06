"""准入评估领域模型：运营区域、车辆能力、指标定义、观测窗口、
事件严重度、整改承诺、基础设施条件与期限性豁免。

所有对象均为不可变值对象（frozen dataclass），并在构造时自检，
使非法领域状态无法表达。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .errors import AdmissionDomainError


def _require(value: str) -> str:
    if not value or not value.strip():
        raise AdmissionDomainError("编码不能为空")
    return value.strip()


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------
class VehicleCapability(str, Enum):
    """车辆能力维度。"""

    AUTONOMOUS_URBAN = "autonomous-urban"          # 城市道路自动驾驶
    AUTONOMOUS_HIGHWAY = "autonomous-highway"      # 快速路自动驾驶
    REMOTE_OPERATION = "remote-operation"          # 远程接管
    FLEET_DISPATCH = "fleet-dispatch"              # 编队/调度


class Severity(str, Enum):
    """事件严重度，由低到高。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# 运营区域与车辆档案
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OperatingRegion:
    """运营区域。阈值可按区域差异化覆盖。"""

    code: str
    name: str
    threshold_overrides: frozenset[tuple[str, float]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require(self.code)
        for metric_code, limit in self.threshold_overrides:
            _require(metric_code)
            if limit < 0:
                raise AdmissionDomainError(f"区域阈值不能为负: {metric_code}")

    def threshold_for(self, metric_code: str, default: float) -> float:
        for code, limit in self.threshold_overrides:
            if code == metric_code:
                return limit
        return default


@dataclass(frozen=True)
class VehicleProfile:
    """申请准入的车辆/车队档案。"""

    fleet_id: str
    capabilities: frozenset[VehicleCapability]
    region_code: str

    def __post_init__(self) -> None:
        _require(self.fleet_id)
        _require(self.region_code)
        if not self.capabilities:
            raise AdmissionDomainError("车辆档案必须声明至少一项能力")


# ---------------------------------------------------------------------------
# 指标定义与观测
# ---------------------------------------------------------------------------
class MetricDirection(str, Enum):
    """指标方向：低于上限达标，或高于下限达标。"""

    MAXIMUM = "maximum"   # 不得超过阈值（事故率、接管率）
    MINIMUM = "minimum"   # 不得低于阈值（覆盖率）


@dataclass(frozen=True)
class MetricDefinition:
    """指标定义（随方案版本发布）。

    - ``lower_is_better`` 已由 ``direction`` 取代但语义等价；
    - ``window_days`` 规定滚动观测窗口长度；
    - 指标可绑定能力或基础设施：空集合表示对整份申请生效。
    """

    code: str
    name: str
    direction: MetricDirection
    default_threshold: float
    window_days: int
    capabilities: frozenset[VehicleCapability] = field(default_factory=frozenset)
    infrastructure: bool = False

    def __post_init__(self) -> None:
        _require(self.code)
        if self.default_threshold < 0:
            raise AdmissionDomainError(f"默认阈值不能为负: {self.code}")
        if self.window_days <= 0:
            raise AdmissionDomainError(f"观测窗口必须为正整数天: {self.code}")

    @property
    def window_duration(self) -> timedelta:
        return timedelta(days=self.window_days)


@dataclass(frozen=True)
class Observation:
    """一条指标观测样本（带时间戳，滚动窗口据此聚合）。"""

    metric_code: str
    region_code: str
    observed_at: datetime
    value: float

    def __post_init__(self) -> None:
        _require(self.metric_code)
        _require(self.region_code)
        if self.observed_at.tzinfo is None:
            raise AdmissionDomainError("观测时间必须带时区")
        if self.value < 0:
            raise AdmissionDomainError(f"观测值不能为负: {self.metric_code}")


# ---------------------------------------------------------------------------
# 安全事件、整改承诺、基础设施条件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SafetyEvent:
    """安全事件。"""

    event_id: str
    region_code: str
    occurred_at: datetime
    severity: Severity
    capability: VehicleCapability
    description: str = ""

    def __post_init__(self) -> None:
        _require(self.event_id)
        _require(self.region_code)
        if self.occurred_at.tzinfo is None:
            raise AdmissionDomainError("事件时间必须带时区")


@dataclass(frozen=True)
class RemediationCommitment:
    """企业整改承诺。

    - 有 ``closed_at`` 视为已闭环；
    - ``due_at`` 为承诺期限，逾期未闭环构成阻塞。
    """

    commitment_id: str
    capability: VehicleCapability
    due_at: datetime
    closed_at: datetime | None = None
    note: str = ""

    def __post_init__(self) -> None:
        _require(self.commitment_id)
        if self.due_at.tzinfo is None:
            raise AdmissionDomainError("整改期限必须带时区")
        if self.closed_at is not None and self.closed_at.tzinfo is None:
            raise AdmissionDomainError("闭环时间必须带时区")
        # 允许逾期闭环（仍是历史事实）；是否逾期由评估引擎按方案宽限期判定。

    @property
    def is_closed(self) -> bool:
        return self.closed_at is not None


@dataclass(frozen=True)
class InfrastructureCondition:
    """基础设施条件（路侧设施覆盖、通信链路等）。"""

    code: str
    region_code: str
    coverage_ratio: float
    required_ratio: float
    measured_at: datetime

    def __post_init__(self) -> None:
        _require(self.code)
        _require(self.region_code)
        if not 0.0 <= self.coverage_ratio <= 1.0:
            raise AdmissionDomainError("覆盖率须在 [0, 1] 区间")
        if not 0.0 <= self.required_ratio <= 1.0:
            raise AdmissionDomainError("要求覆盖率须在 [0, 1] 区间")
        if self.measured_at.tzinfo is None:
            raise AdmissionDomainError("测量时间必须带时区")

    @property
    def satisfied(self) -> bool:
        return self.coverage_ratio >= self.required_ratio


# ---------------------------------------------------------------------------
# 期限性豁免
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Exemption:
    """豁免：必须限定能力范围与期限，到期后自动暴露受影响能力。

    - ``capabilities`` 为空时退化为整份申请豁免，这是被禁止的
      （不允许"临时豁免当作永久达标"）；
    - 豁免到期不等于历史失效：在有效区间内签发的决定仍以当时状态为准；
    - 豁免不可用于重大（critical）事件，见引擎校验。
    """

    exemption_id: str
    metric_code: str
    capabilities: frozenset[VehicleCapability]
    region_code: str
    valid_from: datetime
    valid_until: datetime
    note: str = ""

    def __post_init__(self) -> None:
        _require(self.exemption_id)
        _require(self.metric_code)
        _require(self.region_code)
        if not self.capabilities:
            raise AdmissionDomainError("豁免必须限定至少一项能力范围")
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None:
            raise AdmissionDomainError("豁免期限必须带时区")
        if self.valid_until <= self.valid_from:
            raise AdmissionDomainError("豁免截止时间必须晚于生效时间")

    def is_active(self, at: datetime) -> bool:
        return self.valid_from <= at < self.valid_until


# ---------------------------------------------------------------------------
# 评估结论构件：证据 / 例外 / 阻塞原因
# ---------------------------------------------------------------------------
class ItemStatus(str, Enum):
    PASS = "pass"
    EXEMPTED = "exempted"
    FAIL = "fail"
    DATA_GAP = "data-gap"


@dataclass(frozen=True)
class Evidence:
    """一条结论所依据的证据。"""

    metric_code: str
    window_days: int
    sample_count: int
    observed_value: float | None
    threshold: float
    window_starts_at: datetime
    window_ends_at: datetime

    def to_dict(self) -> dict:
        return {
            "metric_code": self.metric_code,
            "window_days": self.window_days,
            "sample_count": self.sample_count,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "window_starts_at": self.window_starts_at.isoformat(),
            "window_ends_at": self.window_ends_at.isoformat(),
        }


@dataclass(frozen=True)
class ExceptionRecord:
    """评估中的例外记录（豁免或重大事件）。"""

    kind: str            # "exemption" | "major-event"
    reference: str       # 豁免编号 / 事件编号
    capability: str
    detail: str
    active: bool

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "capability": self.capability,
            "detail": self.detail,
            "active": self.active,
        }


@dataclass(frozen=True)
class Blocker:
    """进入下一阶段的阻塞原因。"""

    code: str
    capability: str
    reason: str

    def to_dict(self) -> dict:
        return {"code": self.code, "capability": self.capability, "reason": self.reason}


@dataclass(frozen=True)
class MetricFinding:
    """单个指标的判定结果。"""

    metric_code: str
    capabilities: frozenset[VehicleCapability]
    status: ItemStatus
    observed_value: float | None
    threshold: float
    window_days: int
    exemption_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "metric_code": self.metric_code,
            "capabilities": sorted(c.value for c in self.capabilities),
            "status": self.status.value,
            "observed_value": self.observed_value,
            "threshold": self.threshold,
            "window_days": self.window_days,
            "exemption_id": self.exemption_id,
        }
