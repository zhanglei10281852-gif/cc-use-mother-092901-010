"""测试公共构造器：区域、能力、指标、窗口、严重度、整改与基础设施的组合。"""

from datetime import datetime, timedelta, timezone

from readiness_assessment.decisions import Committee
from readiness_assessment.observations import InfrastructureReading, MetricObservation
from readiness_assessment.plan import (
    Aggregation,
    AssessmentPlan,
    EventSeverity,
    InfrastructureCondition,
    MetricDefinition,
    ObservationWindow,
    OperationRegion,
    RectificationCommitment,
    SeverityPolicy,
    ThresholdDirection,
    VehicleCapability,
)
from readiness_assessment.service import AssessmentService

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)

WINDOW = ObservationWindow("ops-7d", length=7 * DAY, update_cycle=DAY, min_coverage=0.8)


def make_metrics(takeover_threshold: float = 0.01) -> tuple[MetricDefinition, ...]:
    return (
        MetricDefinition("takeover-rate", "次/千公里", ThresholdDirection.UPPER_BOUND, takeover_threshold, "ops-7d", Aggregation.MEAN),
        MetricDefinition("incident-rate", "起/万公里", ThresholdDirection.UPPER_BOUND, 0.005, "ops-7d", Aggregation.MEAN),
        MetricDefinition("system-availability", "可用率", ThresholdDirection.LOWER_BOUND, 0.99, "ops-7d", Aggregation.LATEST),
    )


def make_regions() -> tuple[OperationRegion, ...]:
    return (
        OperationRegion("downtown", "核心城区", {"takeover-rate": 0.004}),
        OperationRegion("suburbs", "近郊", {}),
    )


def make_capability() -> tuple[VehicleCapability, ...]:
    return (
        VehicleCapability("robotaxi-l4", "L4 无人出租", ("takeover-rate", "incident-rate", "system-availability")),
    )


def make_plan(version: int = 1, takeover_threshold: float = 0.01, rectification_due: datetime | None = None) -> AssessmentPlan:
    return AssessmentPlan(
        plan_id="city-av",
        version=version,
        effective_from=BASE,
        regions=make_regions(),
        capabilities=make_capability(),
        metrics=make_metrics(takeover_threshold),
        windows=(WINDOW,),
        severity_policy=SeverityPolicy({EventSeverity.MAJOR, EventSeverity.CRITICAL}, lookback=30 * DAY),
        rectifications=(
            RectificationCommitment("fix-latency", "downtown", "robotaxi-l4", "降低感知时延", rectification_due or BASE + 10 * DAY),
        ),
        infrastructure=(
            InfrastructureCondition("rsu-coverage", "downtown", 0.9, 3 * DAY),
            InfrastructureCondition("rsu-coverage", "suburbs", 0.8, 3 * DAY),
        ),
    )


def make_service(plan: AssessmentPlan | None = None, members=("alice", "bob", "carol"), quorum: int = 2) -> AssessmentService:
    service = AssessmentService(Committee(members, quorum))
    service.register_plan(plan or make_plan())
    return service


def record_daily(
    service: AssessmentService,
    region: str,
    capability: str,
    end: datetime,
    days: int,
    takeover: float = 0.001,
    incident: float = 0.001,
    availability: float = 0.999,
) -> None:
    """从 end - days + 1 到 end 每天写入一组观测。"""
    for offset in range(days - 1, -1, -1):
        at = end - offset * DAY
        service.record_observation(MetricObservation("takeover-rate", region, capability, at, takeover))
        service.record_observation(MetricObservation("incident-rate", region, capability, at, incident))
        service.record_observation(MetricObservation("system-availability", region, capability, at, availability))


def record_infra(service: AssessmentService, region: str, ratio: float, at: datetime):
    return service.record_infrastructure(InfrastructureReading("rsu-coverage", region, ratio, at))
