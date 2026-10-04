"""命令行冒烟：方案登记 → 数据写入 → 评估 → 豁免到期 → 规则换版 → 委员会会签。"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from readiness_assessment.decisions import Committee, ImmutableDecisionError
from readiness_assessment.exemptions import Exemption
from readiness_assessment.observations import InfrastructureReading, MetricObservation
from readiness_assessment.plan import (
    Aggregation,
    AssessmentPlan,
    EventSeverity,
    InfrastructureCondition,
    MetricDefinition,
    ObservationWindow,
    OperationRegion,
    OperationStage,
    RectificationCommitment,
    SeverityPolicy,
    ThresholdDirection,
    VehicleCapability,
)
from readiness_assessment.service import AssessmentService

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def build_plan(version: int, takeover_threshold: float) -> AssessmentPlan:
    return AssessmentPlan(
        plan_id="city-av",
        version=version,
        effective_from=BASE,
        regions=(
            OperationRegion("downtown", "核心城区", {"takeover-rate": 0.004}),
            OperationRegion("suburbs", "近郊", {}),
        ),
        capabilities=(
            VehicleCapability("robotaxi-l4", "L4 无人出租", ("takeover-rate", "incident-rate", "system-availability")),
        ),
        metrics=(
            MetricDefinition("takeover-rate", "次/千公里", ThresholdDirection.UPPER_BOUND, takeover_threshold, "ops-7d", Aggregation.MEAN),
            MetricDefinition("incident-rate", "起/万公里", ThresholdDirection.UPPER_BOUND, 0.005, "ops-7d", Aggregation.MEAN),
            MetricDefinition("system-availability", "可用率", ThresholdDirection.LOWER_BOUND, 0.99, "ops-7d", Aggregation.LATEST),
        ),
        windows=(ObservationWindow("ops-7d", 7 * DAY, DAY, min_coverage=0.8),),
        severity_policy=SeverityPolicy({EventSeverity.MAJOR, EventSeverity.CRITICAL}, lookback=30 * DAY),
        rectifications=(
            RectificationCommitment("fix-latency", "downtown", "robotaxi-l4", "降低感知时延", BASE + 10 * DAY),
        ),
        infrastructure=(
            InfrastructureCondition("rsu-coverage", "downtown", 0.9, 3 * DAY),
            InfrastructureCondition("rsu-coverage", "suburbs", 0.8, 3 * DAY),
        ),
    )


def feed(service: AssessmentService, region: str, end: datetime, days: int, takeover: float) -> None:
    for offset in range(days - 1, -1, -1):
        at = end - offset * DAY
        service.record_observation(MetricObservation("takeover-rate", region, "robotaxi-l4", at, takeover))
        service.record_observation(MetricObservation("incident-rate", region, "robotaxi-l4", at, 0.001))
        service.record_observation(MetricObservation("system-availability", region, "robotaxi-l4", at, 0.999))


def show(title: str, payload) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    service = AssessmentService(Committee(("alice", "bob", "carol"), quorum=2))
    service.register_plan(build_plan(version=1, takeover_threshold=0.01))

    feed(service, "downtown", BASE, 7, takeover=0.007)  # 城区阈值 0.004 → 越限
    feed(service, "suburbs", BASE, 7, takeover=0.003)
    service.record_infrastructure(InfrastructureReading("rsu-coverage", "downtown", 0.95, BASE))
    service.record_infrastructure(InfrastructureReading("rsu-coverage", "suburbs", 0.85, BASE))

    board = service.board("city-av", BASE)
    show("委员会看板（证据 / 例外 / 阻塞原因）", board)

    # 临时豁免：限定范围与期限，城区接管率越限被暂时掩盖
    service.grant_exemption(
        Exemption(
            "EX-1", ("takeover-rate",), ("downtown",), ("robotaxi-l4",),
            "道路施工临时豁免", "alice", BASE, BASE + 2 * DAY,
        )
    )
    masked = service.evaluate("city-av", "downtown", "robotaxi-l4", BASE + DAY)
    show("豁免生效中的城区结论", service.conclusion_view(masked.conclusion_id))

    # 豁免到期：受影响能力自动重新暴露
    feed(service, "downtown", BASE + 3 * DAY, 3, takeover=0.007)
    exposed = service.exposure_report(BASE + 3 * DAY)
    show("豁免到期暴露报告", [e.__dict__ for e in exposed])

    # 规则换版：阈值收紧触发重算，旧版本结论仍可追溯
    recomputed = service.register_plan(build_plan(version=2, takeover_threshold=0.002), as_of=BASE + 4 * DAY)
    show("规则换版后的候选结论", [service.conclusion_view(c.conclusion_id) for c in recomputed])

    # 委员会：提出 → 会签 → 签发；签发后不可变
    suburbs_ready = service.evaluate("city-av", "suburbs", "robotaxi-l4", BASE, plan_version=1)
    decision = service.propose_decision(
        conclusion_id=suburbs_ready.conclusion_id,
        target_stage=OperationStage.COMMERCIAL,
        proposed_by="alice",
        proposed_at=BASE,
    )
    service.countersign(decision.decision_id, "bob", BASE + timedelta(hours=1))
    service.countersign(decision.decision_id, "carol", BASE + timedelta(hours=2))
    show("已签发的阶段决定", service.decision_view(decision.decision_id))
    try:
        service.withdraw(decision.decision_id, "alice", BASE + timedelta(hours=3))
    except ImmutableDecisionError as exc:
        show("签发后撤销被拒绝", {"error": str(exc)})


if __name__ == "__main__":
    main()
