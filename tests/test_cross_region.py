"""跨区域差异测试。

覆盖：
- 同一方案在不同区域的差异化阈值覆盖；
- 区域级数据（观测、事件、设施）按区域隔离；
- 同区域多车队共享区域级事实，但豁免/整改按能力体现在各自结论；
- 调整一个区域的阈值不影响另一个区域。
"""

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from factories import (
    CAP_URBAN,
    T0,
    make_profile,
    make_region,
    make_service,
)
from readiness_assessment.domain import (
    InfrastructureCondition,
    ItemStatus,
    Observation,
    SafetyEvent,
    Severity,
    VehicleCapability,
)


def _feed(svc, region, incident=0.02, takeover=0.005, coverage=0.95):
    for day in range(10):
        svc.add_observation(Observation("incident-rate", region, T0 - timedelta(days=day), incident))
        svc.add_observation(Observation("takeover-rate", region, T0 - timedelta(days=day), takeover))
    svc.report_infrastructure(
        InfrastructureCondition("rsu-coverage", region, coverage, 0.9, T0 - timedelta(days=1))
    )


class CrossRegionTests(unittest.TestCase):
    def _two_region_service(self):
        svc = make_service(register=False)
        # Z-1 事故率阈值收紧到 0.025；Z-2 使用方案默认 0.05
        z1 = make_region("Z-1", overrides=frozenset({("incident-rate", 0.025)}))
        z2 = make_region("Z-2")
        svc.register_application(make_profile("fleet-a", "Z-1"), z1, "commercial-admission")
        svc.register_application(make_profile("fleet-b", "Z-2"), z2, "commercial-admission")
        return svc

    def test_same_observations_different_verdicts_by_region_threshold(self):
        svc = self._two_region_service()
        _feed(svc, "Z-1", incident=0.03)
        _feed(svc, "Z-2", incident=0.03)
        # Z-1 阈值 0.025：0.03 不达标；Z-2 默认 0.05：达标
        self.assertIs(
            svc.latest_candidate("fleet-a", "Z-1").finding("incident-rate").status,
            ItemStatus.FAIL,
        )
        self.assertIs(
            svc.latest_candidate("fleet-b", "Z-2").finding("incident-rate").status,
            ItemStatus.PASS,
        )
        z1_finding = svc.latest_candidate("fleet-a", "Z-1").finding("incident-rate")
        z2_finding = svc.latest_candidate("fleet-b", "Z-2").finding("incident-rate")
        self.assertEqual((z1_finding.threshold, z2_finding.threshold), (0.025, 0.05))

    def test_region_data_is_isolated(self):
        svc = self._two_region_service()
        # 只给 Z-1 喂数据
        _feed(svc, "Z-1")
        z1 = svc.latest_candidate("fleet-a", "Z-1")
        z2 = svc.latest_candidate("fleet-b", "Z-2")
        self.assertTrue(z1.ready)
        self.assertFalse(z2.ready)
        self.assertTrue(
            all(f.status is ItemStatus.DATA_GAP for f in z2.findings)
        )

    def test_region_specific_event_only_blocks_that_region(self):
        svc = self._two_region_service()
        _feed(svc, "Z-1")
        _feed(svc, "Z-2")
        svc.add_event(
            SafetyEvent("EV-1", "Z-1", T0 - timedelta(days=1), Severity.HIGH,
                        VehicleCapability.AUTONOMOUS_URBAN)
        )
        self.assertTrue(
            any(b.code == "major-event" for b in svc.latest_candidate("fleet-a", "Z-1").blockers)
        )
        self.assertFalse(
            any(b.code == "major-event" for b in svc.latest_candidate("fleet-b", "Z-2").blockers)
        )

    def test_region_threshold_update_does_not_touch_other_region(self):
        svc = self._two_region_service()
        _feed(svc, "Z-1", incident=0.03)
        _feed(svc, "Z-2", incident=0.03)
        # Z-1 进一步收紧到 0.01
        svc.update_region(
            make_region("Z-1", overrides=frozenset({("incident-rate", 0.01)}))
        )
        self.assertIs(
            svc.latest_candidate("fleet-a", "Z-1").finding("incident-rate").status,
            ItemStatus.FAIL,
        )
        # Z-2 仍按默认阈值达标
        self.assertIs(
            svc.latest_candidate("fleet-b", "Z-2").finding("incident-rate").status,
            ItemStatus.PASS,
        )

    def test_shared_region_facts_reach_multiple_fleets_but_exemption_is_scoped(self):
        """同区域两支车队都能看到区域观测；豁免只影响其能力范围。"""
        svc = make_service(register=False)
        z1 = make_region("Z-1")
        svc.register_application(
            make_profile("fleet-a", "Z-1", CAP_URBAN), z1, "commercial-admission"
        )
        svc.register_application(
            make_profile("fleet-b", "Z-1",
                         frozenset({VehicleCapability.AUTONOMOUS_HIGHWAY})),
            z1, "commercial-admission",
        )
        # 区域级观测两支车队都取得到
        for day in range(5):
            svc.add_observation(
                Observation("incident-rate", "Z-1", T0 - timedelta(days=day), 0.02)
            )
        # 指标绑定的是城市道路能力：fleet-b（快速路能力）不适用该指标，
        # 其适用指标集合为空 -> 无指标阻塞，但也没有设施数据
        a = svc.latest_candidate("fleet-a", "Z-1")
        b = svc.latest_candidate("fleet-b", "Z-1")
        self.assertTrue(any(f.metric_code == "incident-rate" for f in a.findings))
        self.assertFalse(any(f.metric_code == "incident-rate" for f in b.findings))
        # fleet-b 声明的快速路能力在当前方案中无任何指标考核 -> 真空通过被禁止
        self.assertFalse(b.ready)
        self.assertTrue(
            any(bl.code == "scheme-coverage" and bl.capability == "autonomous-highway"
                for bl in b.blockers)
        )


if __name__ == "__main__":
    unittest.main()
