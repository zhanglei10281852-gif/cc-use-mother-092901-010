"""有版本的评估方案：把区域、能力、指标定义、事件分级门槛、
整改规则与基础设施条件组合为不可变方案版本。

方案发布后不可修改；阈值或规则变更通过发布新版本实现（规则换版）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .domain import MetricDefinition, Severity, VehicleCapability
from .errors import AdmissionDomainError, SchemeConflictError, UnknownSchemeError


@dataclass(frozen=True)
class AssessmentScheme:
    """一个不可变的评估方案版本。

    字段构成一次准入判断的完整规则集：
    - metrics：指标定义（含默认阈值与滚动窗口长度）；
      基础设施覆盖率通过 ``infrastructure=True`` 的指标表达，
      取值来自基础设施条件而非观测样本；
    - major_event_severity：达到该级别的事件触发强制重算并阻塞；
    - remediation_grace_days：整改到期后的宽限期；
    - min_cosignatures：阶段决定所需会签人数。
    """

    scheme_id: str
    version: int
    published_at: datetime
    metrics: frozenset[MetricDefinition]
    major_event_severity: Severity = Severity.HIGH
    remediation_grace_days: int = 0
    min_cosignatures: int = 2
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.scheme_id or not self.scheme_id.strip():
            raise AdmissionDomainError("方案编码不能为空")
        if self.version <= 0:
            raise AdmissionDomainError("方案版本号必须为正整数")
        if self.published_at.tzinfo is None:
            raise AdmissionDomainError("发布时间必须带时区")
        if not self.metrics:
            raise AdmissionDomainError("方案至少要定义一个指标")
        codes = [m.code for m in self.metrics]
        if len(set(codes)) != len(codes):
            raise AdmissionDomainError("方案内指标编码重复")
        if self.remediation_grace_days < 0:
            raise AdmissionDomainError("整改宽限期不能为负")
        if self.min_cosignatures < 1:
            raise AdmissionDomainError("会签人数至少为 1")

    @property
    def metric_codes(self) -> frozenset[str]:
        return frozenset(m.code for m in self.metrics)

    def metric(self, code: str) -> MetricDefinition:
        for metric in self.metrics:
            if metric.code == code:
                return metric
        raise UnknownSchemeError(f"方案 {self.scheme_id} v{self.version} 未定义指标 {code}")

    def metrics_for(self, capabilities: frozenset[VehicleCapability]) -> list[MetricDefinition]:
        """适用于给定能力集合的指标：绑定能力为空（全局指标）或与能力相交。"""
        result = []
        for metric in self.metrics:
            if not metric.capabilities or metric.capabilities & capabilities:
                result.append(metric)
        return sorted(result, key=lambda m: m.code)

    def infrastructure_required_for(
        self, capabilities: frozenset[VehicleCapability]
    ) -> frozenset[str]:
        return frozenset(
            infra_code
            for capability, infra_code in self.required_infrastructure
            if capability in capabilities
        )

    def remediation_deadline(self, due_at: datetime) -> datetime:
        return due_at + timedelta(days=self.remediation_grace_days)


class SchemeRegistry:
    """方案簿：登记同一方案的各不可变版本，按时间或编号解析。"""

    def __init__(self) -> None:
        self._versions: dict[str, dict[int, AssessmentScheme]] = {}

    def publish(self, scheme: AssessmentScheme) -> AssessmentScheme:
        versions = self._versions.setdefault(scheme.scheme_id, {})
        if scheme.version in versions:
            raise SchemeConflictError(
                f"方案 {scheme.scheme_id} 版本 {scheme.version} 已存在，已发布方案不可修改"
            )
        versions[scheme.version] = scheme
        return scheme

    def get(self, scheme_id: str, version: int) -> AssessmentScheme:
        try:
            return self._versions[scheme_id][version]
        except KeyError as exc:
            raise UnknownSchemeError(
                f"方案 {scheme_id} 版本 {version} 不存在"
            ) from exc

    def latest(self, scheme_id: str) -> AssessmentScheme:
        versions = self._versions.get(scheme_id)
        if not versions:
            raise UnknownSchemeError(f"方案 {scheme_id} 尚未发布任何版本")
        return versions[max(versions)]

    def latest_version_at(self, scheme_id: str, at: datetime) -> AssessmentScheme:
        """取 ``at`` 时刻（含）之前已发布的最新版本。"""
        versions = self._versions.get(scheme_id, {})
        candidates = [s for s in versions.values() if s.published_at <= at]
        if not candidates:
            raise UnknownSchemeError(f"方案 {scheme_id} 在 {at.isoformat()} 前没有已发布版本")
        return max(candidates, key=lambda s: (s.published_at, s.version))

    def versions(self, scheme_id: str) -> list[AssessmentScheme]:
        return sorted(self._versions.get(scheme_id, {}).values(), key=lambda s: s.version)

    def changed_between(
        self, scheme_id: str, old_version: int, new_version: int
    ) -> dict[str, tuple[float, float]]:
        """返回两个版本之间发生阈值变更的指标及（旧阈值, 新阈值）。

        仅比较两版本共有的指标；新增/删除指标不在此返回。
        """
        old = self.get(scheme_id, old_version)
        new = self.get(scheme_id, new_version)
        old_map = {m.code: m for m in old.metrics}
        changes: dict[str, tuple[float, float]] = {}
        for metric in new.metrics:
            previous = old_map.get(metric.code)
            if previous is not None and previous.default_threshold != metric.default_threshold:
                changes[metric.code] = (previous.default_threshold, metric.default_threshold)
        return changes
