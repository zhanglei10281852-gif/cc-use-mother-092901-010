"""命令行端到端冒烟：演示一次商业化运营准入的完整生命周期。

  登记申请 -> 数据缺口阻塞 -> 补齐观测与设施 -> 接管率超标
  -> 期限内豁免 -> 提出/会签/签发 -> 豁免到期自动暴露
  -> 已签发决定保持不可变

运行：python run_cli.py
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from readiness_assessment import (
    AdmissionService,
    AssessmentScheme,
    Exemption,
    InfrastructureCondition,
    MetricDefinition,
    MetricDirection,
    Observation,
    OperatingRegion,
    Severity,
    VehicleCapability,
    VehicleProfile,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, tzinfo=UTC)
CAP = frozenset({VehicleCapability.AUTONOMOUS_URBAN})


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def summary(svc, fleet="fleet-a", region="Z-1"):
    c = svc.latest_candidate(fleet, region)
    print(json.dumps(
        {
            "ready": c.ready,
            "scheme_version": c.scheme_version,
            "findings": [(f.metric_code, f.status.value) for f in c.findings],
            "exceptions": [(e.kind, e.reference, e.active) for e in c.exceptions],
            "blockers": [b.code for b in c.blockers],
        },
        ensure_ascii=False,
    ))
    return c


def main() -> None:
    svc = AdmissionService(clock=lambda: T0)
    metrics = frozenset({
        MetricDefinition("incident-rate", "万车事故率", MetricDirection.MAXIMUM, 0.05, 30, CAP),
        MetricDefinition("takeover-rate", "百公里接管率", MetricDirection.MAXIMUM, 0.01, 30, CAP),
        MetricDefinition("rsu-coverage", "路侧设施覆盖率", MetricDirection.MINIMUM, 0.9, 30,
                         CAP, infrastructure=True),
    })
    svc.publish_scheme(
        AssessmentScheme("commercial-admission", 1, datetime(2026, 9, 1, tzinfo=UTC),
                         metrics, major_event_severity=Severity.HIGH, min_cosignatures=2)
    )

    banner("登记申请（无数据，全部数据缺口）")
    svc.register_application(
        VehicleProfile("fleet-a", CAP, "Z-1"),
        OperatingRegion("Z-1", "一号示范区"),
        "commercial-admission",
    )
    summary(svc)

    banner("补齐 30 天滚动窗口观测与基础设施快照")
    for day in range(20):
        t = T0 - timedelta(days=day)
        svc.add_observation(Observation("incident-rate", "Z-1", t, 0.02))
        svc.add_observation(Observation("takeover-rate", "Z-1", t, 0.005))
    svc.report_infrastructure(InfrastructureCondition("rsu-coverage", "Z-1", 0.95, 0.9, T0))
    summary(svc)

    banner("接管率近期超标 -> 阻塞；授予 7 天期限豁免 -> 可提出")
    for day in range(5):
        svc.add_observation(Observation("takeover-rate", "Z-1", T0 - timedelta(days=day, hours=1), 0.04))
    summary(svc)
    svc.grant_exemption(
        Exemption("EX-2026-001", "takeover-rate", CAP, "Z-1",
                  T0 - timedelta(days=1), T0 + timedelta(days=7),
                  note="企业承诺更换感知算法，给予 7 天整改窗口")
    )
    summary(svc)

    banner("委员会：提出 -> 会签满额 -> 签发（决定不可变）")
    svc.propose("D-2026-001", "fleet-a", "Z-1", "chair")
    svc.cosign("D-2026-001", "member-a")
    view = svc.cosign("D-2026-001", "member-b")
    print("decision stage:", view.stage.value, "| cosigners:", list(view.cosigners))

    banner("时钟推进 10 天：豁免到期，受影响能力自动暴露")
    svc.sweep_time_advances(T0 + timedelta(days=10))
    c = summary(svc)
    expired = next(e for e in c.exceptions if e.kind == "exemption" and not e.active)
    print("到期豁免:", expired.reference, "-", expired.detail)

    banner("已签发阶段决定仍保持签发时快照")
    issued = svc.get_decision("D-2026-001")
    print(json.dumps(
        {"stage": issued.stage.value,
         "snapshot_ready": issued.conclusion["ready"],
         "snapshot_version": issued.conclusion["scheme_version"]},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
