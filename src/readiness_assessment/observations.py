"""运行期观测数据：指标读数、安全事件与基础设施读数。"""

from dataclasses import dataclass
from datetime import datetime

from .plan import EventSeverity


@dataclass(frozen=True)
class MetricObservation:
    """一次指标观测，按各自数据源的更新周期到达。"""

    metric_code: str
    region_code: str
    capability_code: str
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class SafetyEvent:
    """安全事件，重大事件会触发候选结论重算。"""

    event_id: str
    region_code: str
    capability_code: str
    occurred_at: datetime
    severity: EventSeverity
    summary: str


@dataclass(frozen=True)
class InfrastructureReading:
    """基础设施覆盖率读数，按设施数据源的更新周期刷新。"""

    condition_code: str
    region_code: str
    observed_ratio: float
    updated_at: datetime
