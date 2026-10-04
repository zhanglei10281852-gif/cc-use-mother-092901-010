# 商业化运营准入评估

面向智能网联汽车从示范到常态运营的阶段准入判断：把运营区域、车辆能力、指标定义、观测窗口、事件严重度、整改承诺和基础设施条件组合成**有版本的评估方案**，在数据缺口、阈值变更或重大事件发生时重算候选结论，同时保持已签发阶段决定不可变。

## 模块结构（`src/readiness_assessment/`）

- `contracts.py` — 基础数据契约（指标窗口、评估指标、阶段枚举）
- `plan.py` — 评估方案：`AssessmentPlan`（版本化）组合 `OperationRegion`（区域阈值覆盖）、`VehicleCapability`、`MetricDefinition`（阈值/方向/聚合）、`ObservationWindow`（滚动窗口 + 更新周期 + 最低覆盖率）、`SeverityPolicy`、`RectificationCommitment`、`InfrastructureCondition`
- `observations.py` — 运行期观测：指标读数、安全事件、基础设施读数
- `exemptions.py` — `Exemption`：强制限定指标/区域/能力范围与到期时刻，不允许永久或全局豁免
- `evaluation.py` — 纯函数评估引擎，产出 `CandidateConclusion`（证据 `Evidence`、例外 `ExceptionNote`、阻塞 `Blocker`）
- `decisions.py` — `Committee`：提出 / 会签 / 驳回 / 撤销；会签达法定人数即签发，签发后任何变更抛出 `ImmutableDecisionError`；状态迁移在锁内完成，支持并发会签
- `service.py` — `AssessmentService`：方案版本登记、观测写入触发重算、豁免到期暴露报告、委员会协作与查询接口（`conclusion_view` / `decision_view` / `board`）

## 核心规则

- **滚动窗口**：指标按窗口 `(as_of - length, as_of]` 聚合；观测次数不足或数据源超过更新周期未刷新即判定数据缺口，结论为 `insufficient_data`
- **规则换版**：方案版本只能递增；登记新版本即按新阈值重算全部候选结论，旧版本结论与已签发决定保持可追溯、不可改写
- **重大事件**：命中阻断严重度的事件立即触发相关候选结论重算，且不可被豁免掩盖
- **豁免到期**：评估按 `as_of` 判定豁免是否生效；到期后越限重新暴露为阻塞，`exposure_report` 列出受影响能力
- **跨区域差异**：区域可覆盖指标阈值，同一观测值在不同区域得出不同结论

运行测试：`python -m unittest discover -s tests -v`

编译检查：`python -m compileall -q src tests run_cli.py`

命令行冒烟：`python run_cli.py`
