# 商业化运营准入评估

面向智能网联汽车从规模示范走向常态运营的准入委员会评估服务。把**运营区域、
车辆能力、指标定义、观测窗口、事件严重度、整改承诺和基础设施条件**组合为
**有版本的评估方案**，在数据缺口、阈值换版或重大事件发生时重算候选结论，
而**已经签发的阶段决定保持不可变**；任何豁免都限定能力范围与期限，
到期后自动暴露受影响能力。

## 领域模型

| 概念 | 类型 | 说明 |
|---|---|---|
| 运营区域 | `OperatingRegion` | 区域编码 + 按区域差异化的指标阈值覆盖 |
| 车辆档案 | `VehicleProfile` | 车队与其声明的车辆能力集合 |
| 指标定义 | `MetricDefinition` | 阈值方向（上限/下限）、默认阈值、滚动窗口天数；可绑定能力；`infrastructure=True` 表示基础设施覆盖率指标 |
| 观测样本 | `Observation` | 带时间戳的指标样本，滚动窗口据此聚合 |
| 安全事件 | `SafetyEvent` | `low/medium/high/critical` 四级严重度 |
| 整改承诺 | `RemediationCommitment` | 有期限；未闭环/逾期构成阻塞，支持闭环 |
| 基础设施条件 | `InfrastructureCondition` | 覆盖率快照，取评估时刻前最新一条 |
| 豁免 | `Exemption` | **必须**限定能力集合与起止期限；重大事件不可豁免 |
| 评估方案 | `AssessmentScheme` | 上述规则的**不可变版本**，经 `SchemeRegistry` 发布 |

## 核心机制

- **滚动窗口**：每个指标按 `window_days` 取 `[as_of - N天, as_of)` 半开区间，
  窗口内均值为观测值；无样本即 `data-gap` 阻塞，旧样本随窗口滑出后缺口会重新出现。
- **重算触发**：登记申请、补报观测、上报事件、整改更新、设施快照、豁免授予、
  区域阈值调整、方案换版、时钟推进（豁免到期）都会重算；候选结论按触发原因留痕。
- **规则换版**：方案发布后不可修改；阈值或重大事件门槛变更只能发新版本，
  按发布时间解析当前生效版本，未来版本不提前生效。
- **豁免到期暴露**：`sweep_time_advances()` 推进时钟后，到期豁免在例外中
  标记 `active=False`，受影响能力恢复常规考核，失败/缺口重新形成阻塞。
- **已签发决定不可变**：委员会动作采用事件溯源（proposed/cosigned/issued/
  rejected/withdrawn）。提案时固化候选结论快照；签发（APPROVED）或驳回、
  撤销后进入终态，任何后续重算都不触碰它。
- **并发会签**：所有委员会动作在锁内完成"校验+追加事件"，会签满额与签发
  原子发生，多线程下恰好满额、恰好签发一次。
- **结论接口**：每个候选结论都给出 `findings`（逐项判定）、`evidence`
  （窗口、样本数、观测值、阈值）、`exceptions`（豁免/重大事件及是否有效）、
  `blockers`（下阶段阻塞原因）。

## 目录结构

```
src/readiness_assessment/
  contracts.py   # 基础契约（DecisionStage / MetricWindow / AssessmentMetric）
  domain.py      # 不可变领域值对象与构造期自检
  scheme.py      # 版本化评估方案与方案簿
  engine.py      # 滚动窗口聚合 + 纯函数评估引擎
  decisions.py   # 事件溯源的委员会决定（线程安全）
  service.py     # AdmissionService 门面：数据接入、重算编排、委员会接口
  errors.py      # 领域异常
tests/           # unittest 自动化测试
run_cli.py       # 端到端生命周期冒烟演示
```

## 运行

```bash
# 自动化测试（覆盖滚动窗口、规则换版、跨区域差异、并发会签等）
python -m unittest discover -s tests -v

# 编译检查
python -m compileall -q src tests run_cli.py

# 命令行端到端演示
python run_cli.py
```

## 最小用法

```python
from datetime import datetime, timezone
from readiness_assessment import (
    AdmissionService, AssessmentScheme, MetricDefinition, MetricDirection,
    OperatingRegion, VehicleProfile, VehicleCapability, Observation,
    InfrastructureCondition,
)

svc = AdmissionService()
svc.publish_scheme(AssessmentScheme(
    scheme_id="commercial-admission", version=1,
    published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    metrics=frozenset({
        MetricDefinition("incident-rate", "事故率", MetricDirection.MAXIMUM,
                         0.05, 30, frozenset({VehicleCapability.AUTONOMOUS_URBAN})),
    }),
))
svc.register_application(
    VehicleProfile("fleet-a", frozenset({VehicleCapability.AUTONOMOUS_URBAN}), "Z-1"),
    OperatingRegion("Z-1", "一号示范区"),
    "commercial-admission",
)
candidate = svc.latest_candidate("fleet-a", "Z-1")
print(candidate.ready, candidate.blockers)   # False，数据缺口阻塞
```
