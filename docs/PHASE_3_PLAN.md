# 第三阶段建议：游戏测试闭环与首次 Agent 接入

状态：2026-09-21；TASK-003A、TASK-003B（含 R1）已验收；TASK-003C-R1 离线工程链路和 V2 预算/费用工程准备通过。DeepSeek Flash 真实 Gate A 与 normal 三目标 Gate B 已通过；Gate C 未执行，完整真实模型验收仍在进行中。

## 1. 目标与依据

依据 AGENTS.md，TASK-002A/TASK-002B 已验收；当前已有确定性战斗、HTTP 接口、PostgreSQL 持久化和重放恢复，学习复盘仍待完成。

建议下一步形成：测试目标 → 执行动作 → 规则校验 → 保存证据 → 重跑确认的闭环，再让 Agent 负责选择测试路径。游戏获胜不等于测试成功；LLM 的判断不能直接作为缺陷事实。

## 2. 分步交付

| 顺序 | 任务 | 交付与验收 |
| --- | --- | --- |
| 1 | TASK-003A：确定性测试执行器与规则判定基线 | HTTP 工具、固定场景、独立规则检查、JSON 证据与重跑；正常场景通过、错误观测可识别 |
| 2 | TASK-003B：可控缺陷与评测集 | 隔离缺陷靶场、触发轨迹、正常对照；每个缺陷均能通过公开动作触发并检测 |
| 3 | TASK-003C：首次 Agent 工作流 | LangGraph 候选方案、工具调用、步数预算、规则校验与报告；相同评测集和预算下与脚本基线比较 |

TASK-003A 已完成 R1 修复与验收。TASK-003B 任务单 `docs/claude-tasks/TASK-003B-fault-lab-and-benchmark.md` 已按授权实施完成：三个真实状态缺陷、四个独立内存 profile、32 个固定评测组合及重跑，详见 README「缺陷靶场与固定评测集」。TASK-003C 随后采用 LangGraph 完成首次单 Agent 闭环，离线工程链路已通过 R1 复验。

003B 的实现方式与规划一致：以三个窄领域扩展点和路由会话工厂依赖接入独立 lab；不复制完整战斗引擎，不通过改写响应制造缺陷。2026-09-20 首轮独立审查通过了 293 项非 PostgreSQL、29 项 PostgreSQL 回归，以及固定矩阵和真实 HTTP 演示。其后 Codex 经所有者授权完成 R1，修复重放执行错误被降级为退出 1 的问题；302 项非 PostgreSQL 回归、Ruff、固定矩阵和原始反例复验通过，TASK-003B 验收完成。R1 未修改数据库相关路径，也未重复数据库测试；记录见 `docs/claude-tasks/TASK-003B-R1-review-fixes.md`。学习复盘仍待完成。

现有 Pytest 用于验证实现；003A 将操作、观察、判定和证据做成未来 Agent 可以调用的业务能力，提供无模型成本的基线。Pytest 同时验证执行器本身，两者不互相替代。

## 3. 首批缺陷候选（003B 已实现）

| 缺陷 | 公开触发路径 | 判定依据 |
| --- | --- | --- |
| 治疗未截断到上限 | seed=42，attack → use_potion | 药水事件 HP 不超过 100，治疗量为 min(25, 缺失 HP) |
| 喝药未扣数量 | 受伤后 use_potion | 成功喝药后数量恰好减 1 |
| 敌人死亡后仍反击 | seed=42，连续三次 attack | 致死攻击后 won，本回合没有 retaliate |

上述正常路径已在当前领域对象上运行检查；三个缺陷变体已实现并验证：目标规则分别是
`R-POTION-CAP`、`R-POTION-DECREMENTS`、`R-NO-RETALIATE-ON-KILL`，
32 个组合的逐格结果与指标分子/分母见 README 与 `artifacts/benchmarks/` 下的评测摘要。

当前 PostgreSQL 恢复会按正常规则重放。直接把缺陷状态写入现有数据库可能先触发恢复不一致，混淆游戏缺陷与存储故障。因此首批缺陷在独立、显式启用的内存靶场运行，默认服务仍执行正常规则（靶场即使配置为 postgres 也不创建 Engine）。缺陷会话持久化需要另行设计规则版本和恢复兼容。

003B 指标已按「明确分母」落地为 7 项：缺陷覆盖 3/3、触发正确率 9/9、目标规则检出 9/9、
normal 误报 0/8、变体误报 0/15、执行错误 0/32、重跑一致 32/32（另列 9/9 缺陷组合复现）。
固定脚本检测到全部变体只是基线，不代表 Agent 自主发现能力。

## 4. Agent 路线建议

已按建议先让 LangGraph 调用 003A 的 Python 工具适配器，再考虑将稳定工具封装为 MCP。LangGraph 管理工作流与状态，MCP 提供工具互操作，两者可以组合。先验证 Agent 闭环，再单独验收协议通信，降低了首次接入的排错复杂度。

003C 已采用 LangGraph。模型指定为与当前 Claude Code 项目配置一致的 DeepSeek `deepseek-v4-flash`；默认步数预算已落地，真实模型费用仍待确认。Agent 获得规则、目标和公开观测，缺陷标识及标准触发轨迹仅供评测器使用；脚本与 Agent 结果分开报告。

TASK-003C 任务单见 `docs/claude-tasks/TASK-003C-langgraph-test-agent.md`：已实现单 Agent、
逐步工具决策、共享增量执行、预算控制及兼容既有报告的无模型重跑。首次对照选取
既有三个场景目标 × 四个 profile 的 12 组合子集，两组使用相同规则、seed 和动作预算；
原 32 组合继续作为完整脚本回归，不将子集结果冒充全部场景的 Agent 成绩。
离线工程验收与真实模型效果分开记录；DeepSeek Flash 已选定，付费预算待确认。
独立审查发现的评测错误传播、usage 累计、总时限与多工具协议问题已在 R1 修复。
R1 以 443 项非 PostgreSQL 回归、原 32 组合脚本评测、离线 12 格 Agent 评测及异常注入复验通过；
返工与验证记录见 `docs/claude-tasks/TASK-003C-R1-review-fixes.md`。这些离线结果只证明
工程链路，不能作为 Agent 能力成绩。

真实模型验收按 `docs/claude-tasks/TASK-003C-V1-real-model-acceptance.md` 分级执行。
首次 Gate A 因输出预算耗尽而连续没有 `tool_use`；Agent schema 1.2 补齐 stop reason/文本证据后，
诊断确认 `max_tokens`。供应商现用 `tool_choice=any` 且禁止并行工具调用，修复后以 2 次原生工具
调用、0 次格式纠正完成 `normal × healing`，exit 0，replay match。其后 Gate B 三目标均 exit 0、
规则失败 0、无模型 replay match，共 8 次模型调用、3,628 Token；Gate C 没有执行；
benchmark 可配置预算与整批费用汇总已在 V2 完成。

`docs/claude-tasks/TASK-003C-V2-benchmark-budget-cost.md` 已完成：显式 `BudgetSpec` 和价格配置
贯穿 12 格，严格汇总 usage/费用，agent benchmark 升级到 1.2.0。离线 12 格、原 32 组合、
466 项非 PostgreSQL 回归和 Ruff 均通过；V2 本身未调用真实模型。其后 Gate B 已通过，Gate C
仍待独立决定。

下一任务为 `docs/claude-tasks/TASK-003C-V3-real-gate-c-benchmark.md`：复用现有评测器执行真实固定
12 格，不再增加 Agent 功能。Gate A/B 已累计 10 次有效调用、4,400 Token；Gate C 的整批技术
上限为 72 次调用与 36,864 输出 Token，仍需所有者单独授权并确认单价/金额口径后才能执行。

## 5. 学习与范围

003A 开始前安排一次复盘：说明一次 HTTP 动作如何经过领域层、仓储事务和恢复校验，并学习状态转换、边界值与测试判定器（oracle）。不因代码验收自动标记所有者已掌握。

暂不加入前端、视觉测试、RAG、多 Agent、消息队列、应用容器化或新数据库表。公开名称和许可证可独立决定，不阻塞本地闭环。
