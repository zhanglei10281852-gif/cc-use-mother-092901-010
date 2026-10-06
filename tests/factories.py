"""测试共用构造工厂。"""

from datetime import datetime, timezone

from readiness_assessment.domain import (
    MetricDefinition,
    MetricDirection,
    OperatingRegion,
    VehicleCapability,
    VehicleProfile,
)
from readiness_assessment.scheme import AssessmentScheme
from readiness_assessment.service import AdmissionService

UTC = timezone.utc

# 统一测试时刻
T0 = datetime(2026, 10, 1, tzinfo=UTC)

CAP_URBAN = frozenset({VehicleCapability.AUTONOMOUS_URBAN})
CAP_HIGHWAY = frozenset({VehicleCapability.AUTONOMOUS_HIGHWAY})


def dt(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def incident_metric(threshold=0.05, window_days=30, caps=CAP_URBAN):
    return MetricDefinition(
        "incident-rate", "万车事故率", MetricDirection.MAXIMUM,
        threshold, window_days, caps,
    )


def takeover_metric(threshold=0.01, window_days=30, caps=CAP_URBAN):
    return MetricDefinition(
        "takeover-rate", "百公里接管率", MetricDirection.MAXIMUM,
        threshold, window_days, caps,
    )


def coverage_metric(threshold=0.9, window_days=30, caps=CAP_URBAN):
    return MetricDefinition(
        "rsu-coverage", "路侧设施覆盖率", MetricDirection.MINIMUM,
        threshold, window_days, caps, infrastructure=True,
    )


def standard_metrics():
    return frozenset({incident_metric(), takeover_metric(), coverage_metric()})


def make_scheme(
    version=1,
    metrics=None,
    *,
    published_at=None,
    major_severity=None,
    min_cosignatures=2,
    grace_days=0,
):
    from readiness_assessment.domain import Severity

    return AssessmentScheme(
        scheme_id="commercial-admission",
        version=version,
        published_at=published_at or dt(2026, 9, 1),
        metrics=metrics if metrics is not None else standard_metrics(),
        major_event_severity=major_severity or Severity.HIGH,
        remediation_grace_days=grace_days,
        min_cosignatures=min_cosignatures,
    )


def make_region(code="Z-1", name=None, overrides=frozenset()):
    return OperatingRegion(code, name or f"区域{code}", overrides)


def make_profile(fleet_id="fleet-a", region_code="Z-1", caps=CAP_URBAN):
    return VehicleProfile(fleet_id, caps, region_code)


def make_service(
    *,
    region=None,
    profile=None,
    scheme=None,
    now=T0,
    register=True,
):
    """构造已发布 v1 方案的服务（默认不登记申请，由测试自行登记）。"""
    svc = AdmissionService(clock=lambda: now)
    svc.publish_scheme(scheme or make_scheme())
    if register:
        svc.register_application(
            profile or make_profile(),
            region or make_region(),
            "commercial-admission",
        )
    return svc
