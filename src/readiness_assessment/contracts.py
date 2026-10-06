"""运营指标与阶段决定的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DecisionStage(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class MetricWindow:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("指标窗口结束时间必须晚于开始时间")


@dataclass(frozen=True)
class AssessmentMetric:
    metric_code: str
    region_code: str
    window: MetricWindow
    value: float
    stage: DecisionStage = DecisionStage.DRAFT
