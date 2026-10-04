"""限定范围与期限的豁免；到期后受影响能力自动重新暴露。"""

from dataclasses import dataclass
from datetime import datetime


class ExemptionScopeError(ValueError):
    """豁免范围或期限不合法。"""


@dataclass(frozen=True)
class Exemption:
    """临时豁免：必须显式限定指标、区域与能力范围，并设有到期时刻。

    豁免只在 [granted_at, expires_at) 内生效；到期后评估不再引用它，
    被掩盖的能力越限会重新暴露为阻塞原因。
    """

    exemption_id: str
    metric_codes: tuple[str, ...]
    region_codes: tuple[str, ...]
    capability_codes: tuple[str, ...]
    reason: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_codes", tuple(self.metric_codes))
        object.__setattr__(self, "region_codes", tuple(self.region_codes))
        object.__setattr__(self, "capability_codes", tuple(self.capability_codes))
        if not self.metric_codes or not self.region_codes or not self.capability_codes:
            raise ExemptionScopeError("豁免必须显式限定指标、区域与能力范围，不允许全局豁免")
        if self.expires_at <= self.granted_at:
            raise ExemptionScopeError("豁免必须设置晚于签发时间的到期时间，不允许永久豁免")

    def active_at(self, moment: datetime) -> bool:
        return self.granted_at <= moment < self.expires_at

    def covers(self, metric_code: str, region_code: str, capability_code: str) -> bool:
        return (
            metric_code in self.metric_codes
            and region_code in self.region_codes
            and capability_code in self.capability_codes
        )
