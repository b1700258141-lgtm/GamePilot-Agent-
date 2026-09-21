# TASK-003C-V1：DeepSeek Flash 真实模型分级验收

日期：2026-09-21。状态：**Gate A 与 normal 三目标 Gate B 已通过；benchmark 预算与整批费用汇总已由 V2 完成；Gate C 未授权、未执行。**

下一实施切片见 `docs/claude-tasks/TASK-003C-V2-benchmark-budget-cost.md`；它只完成无付费预算与
费用证据，不执行本文件中的 Gate B/C。

对应里程碑：完成 TASK-003C 的真实模型兼容性与小样本能力验收。TASK-003C-R1 已证明
离线工程链路可靠；本任务不再开发一套新 Agent，而是在可审计的预算边界内验证
DeepSeek `deepseek-v4-flash` 的真实工具调用、usage、费用、目标覆盖和缺陷检出表现。

开始前必须阅读：

- `AGENTS.md`
- `docs/PHASE_3_PLAN.md`
- `docs/claude-tasks/TASK-003C-langgraph-test-agent.md`
- `docs/claude-tasks/TASK-003C-R1-review-fixes.md`
- README 的“游戏测试 Agent”章节

## 1. 为什么这是下一个任务

当前已完成：LangGraph 图、独立判定、预算、报告、无模型重跑、12 格评测及异常传播。
当前唯一未验证的核心事实是：选定的真实模型能否通过 Anthropic 兼容入口稳定调用工具，
并在相同规则、seed 和预算下完成三个目标。

不能直接执行现有 12 格真实评测：当前每格默认最多 12 次模型调用、每次最多 512 输出 Token，
12 格理论上最多 144 次调用和 73,728 输出 Token；benchmark 入口也不能传显式单价，
无法在整批摘要中复算费用。因此先补齐预算与费用证据，再进行分级付费验收。

## 2. 目标

1. 让 Agent benchmark 接收并记录收紧后的 `BudgetSpec`，真实执行不能隐式使用宽松默认值。
2. 让 benchmark 接收显式输入/输出单价和币种，汇总实际 Token 与费用；usage 未知时保持 unknown。
3. 用三道独立授权门逐级验证：单格兼容性、normal 三目标、完整 12 格。
4. 保存所有成功与失败证据；失败后不自动改提示词、不自动重跑、不只展示最好结果。
5. 形成一份可用于面试讲解的真实模型验收记录，同时保留离线替身与真实能力成绩的边界。

## 3. 范围

### 3.1 本任务包含

- 为 `gamepilot.benchmark agent` 增加与单次 Agent CLI 一致的预算参数：
  `--max-actions`、`--max-model-calls`、`--max-format-retries`、`--model-timeout`、
  `--http-timeout`、`--total-timeout`、`--max-output-tokens`。
- `run_agent_eval` 接收一份显式 `BudgetSpec`，所有 12 格使用相同配置，摘要记录实际配置。
- benchmark 增加 `--input-price`、`--output-price`、`--currency`，传入每格 Agent 报告。
- Agent 模型调用记录增加供应商 `stop_reason` 与有界的文本响应；首轮 Gate A 证明当前报告
  只能看到 `no_tool_call`，无法区分输出被截断、纯文本回答或其他兼容性差异。不得记录 SDK
  原始对象、请求头或异常全文，文本字段仍受单次输出 Token 上限约束。
- 评测摘要增加整批 usage/费用汇总：已知/未知格数、prompt/completion/total Token；只有
  12 格 usage 与单价都可靠时才给出总金额，否则总金额为 unknown 并说明原因。
- 保持 SDK `max_retries=0`；应用层模型调用次数就是实际请求次数。
- 分级执行真实模型验收，并记录命令、退出码、报告路径、实际调用数、Token、费用与结论。
- 形成 `docs/validation/TASK-003C-real-model-acceptance.md`；只写非密钥配置和结果。
- 在付费运行前完成一次 Agent 原理复盘提纲：State/Node/Edge、oracle 独立性、四类退出码、
  fake 与真实模型成绩边界。可以提供自检问题，但不得替所有者宣称已经掌握。

### 3.2 本任务不包含

- MCP、RAG、多 Agent、长期记忆、前端、视觉测试或新缺陷；
- 提示词自动搜索、模型路由、并发评测或失败自动重试；
- 改动游戏领域规则、公开 API、仓储、数据库迁移或 testing schema/rules；
- 将密钥写入命令参数、文件、报告、日志或聊天；
- 因一次失败就更换模型，或删除失败产物后重新跑到成功为止；
- 提交、推送、发布或部署。

## 4. 固定模型与安全边界

- provider：`anthropic-compatible`
- model：`deepseek-v4-flash`
- base URL：`https://api.deepseek.com/anthropic`
- 密钥变量名：`GAMEPILOT_AGENT_API_KEY`
- 密钥只从环境读取；程序和实施报告只能记录变量名，不能读取后打印或写入值。
- 未给 `--paid` 时必须保持零模型请求；真实关卡必须显式带 `--paid`。
- 每一道付费关卡都需要所有者单独授权。上一关授权不自动授权下一关。
- 所有者还需给出最大金额与币种，或明确说明当前入口属于已包含额度、无边际按量费用。
  单价未知时可以做兼容性冒烟，但不能声称完成费用验收或启动完整 12 格。

## 5. 无付费实现与回归

先完成本节，期间不得设置、读取或调用真实密钥。

### 5.1 参数与模型

- benchmark 参数的默认值仍等于 `AGENT_EVAL_BUDGET`，保证已有离线命令行为不变。
- 所有预算值沿用 `BudgetSpec` 的严格校验；不能在 CLI 另写一套宽松规则。
- `run_agent_eval(..., budget=...)` 把同一对象用于 12 格并写入报告，不能只修改显示文本。
- 单价必须成对给出且为非负数；只给一个单价或币种无效时以输入错误 exit 2 拒绝。
- Agent benchmark 版本因摘要契约变化升级到 1.2.0；AgentRunReport 因新增模型响应诊断字段
  升级到 1.2，旧 1.1 报告仍应能被明确识别，不能被静默按新字段解释。

### 5.2 整批 usage 与费用口径

- `usage_known_cells` / `usage_unknown_cells` 分母固定为计划格数 12。
- 只有所有已执行格的 usage 均可靠时才汇总 Token；任一格 unknown 时总 Token 标记 unknown，
  同时保留每格原始值，不能把 unknown 当 0。
- 总费用只在全部 usage 可靠且输入、输出单价与币种齐全时计算。
- 金额使用实际 Token × 显式单价，不内置或猜测供应商价格。
- 执行错误、偏差和费用结论彼此独立：低费用不能掩盖失败，能力偏差也不能伪装成执行错误。

### 5.3 必需测试

- CLI 默认值与自定义预算完整传到 12 格报告。
- 非法预算、负单价、只给一个单价均在任何模型调用前 exit 2。
- 12 格 usage 全已知时 Token 与费用精确求和；一格 unknown 时整批金额 unknown。
- 模型错误响应携带可靠 usage 时仍计入对应格与总计。
- fake SDK 覆盖 `stop_reason=max_tokens`、纯文本无工具、工具调用三类响应，报告能保留诊断信息，
  且不记录密钥、请求头或 SDK 原始异常文本。
- 离线 Agent benchmark 保持 12/12 match、exit 0；原脚本 32 组合仍 exit 0。
- 完整非 PostgreSQL 回归、Ruff 检查与格式检查通过。

## 6. 三道真实模型关卡

以下是建议的技术上限，不等于金额授权。执行者必须在每一关开始前记录所有者批准的
最大金额、币种和授权时间；没有这些信息就停在上一关，不得自行调用。

### Gate A：单格协议与费用冒烟

只启动 `normal` localhost 靶场，执行 `healing` 一次：

- seed：42
- max actions：3
- max model calls：4
- max format retries：1
- max output tokens：512
- model timeout：30 s
- HTTP timeout：5 s
- total timeout：60 s
- 理论输出上限：4 × 512 = 2,048 Token；输入 Token 以实际 usage 为准。

通过条件：provider/model/base URL 正确且不是测试替身；真实工具调用成功；exit 0；目标覆盖成立；
RunReport 可无模型 replay 且 match；usage 可用；显式单价存在时费用可复算；报告不含密钥。

Gate A 失败时立即停止。保留原报告并分类为配置/协议/模型格式/游戏执行/能力问题，
不得自动进入 Gate B，也不得在同一授权下改提示词后重跑。

### Gate B：normal 三目标

Gate A 通过并取得第二次授权后，在同一 `normal` profile 分别运行：

| 目标 | max actions | max model calls | 预期 |
| --- | ---: | ---: | --- |
| full-health | 2 | 3 | exit 0、覆盖满血喝药拒绝 |
| healing | 3 | 4 | exit 0、覆盖受伤后治疗 |
| victory | 6 | 6 | exit 0、覆盖获胜与终态拒绝 |

三次均使用 512 输出 Token、1 次格式纠正和 60 秒总时限。整关理论最多 13 次调用、
6,656 输出 Token。三条都必须 replay match，normal 规则失败为 0。

### Gate C：固定 12 格能力冒烟

Gate B 通过、根据前两关实际 Token/费用估算最坏支出，并取得第三次授权后才运行。

统一预算：max actions 6、max model calls 6、max format retries 1、max output tokens 512、
model timeout 30 s、HTTP timeout 5 s、total timeout 60 s。12 格理论最多 72 次调用和
36,864 输出 Token；实际输入与费用必须由 Gate A/B 证据估算并写入授权记录。

固定门槛：

- 12 格全部执行，执行错误 0/12；
- 同 profile replay：12/12 match、12/12 可比；
- normal 误报 0/3；
- 预定机会检出 3/3，去重缺陷覆盖 3/3；
- provider/model/预算、每格调用数、Token、费用和总费用均有证据；
- benchmark 总结 exit 0。

未达到能力指标时报告 deviation / exit 1，这是有效的真实模型结论，不自动视为代码缺陷。
出现 timeout、429、5xx、协议或报告错误时 exit 2，先分析工程原因，再由所有者决定是否付费重跑。

## 7. 运行与证据要求

Gate A/B 使用 localhost HTTP 靶场验证实际客户端链路；Gate C 使用现有 benchmark 的进程内 ASGI
四 profile 隔离，以保持固定矩阵和确定性，不重复搭建四个 localhost 服务。真实模型调用仍访问
外部供应商。该收口决策与预算见 `TASK-003C-V3-real-gate-c-benchmark.md`。建议产物目录：

```text
artifacts/
├─ real-model-smoke/<gate-a-run-id>/
├─ real-model-normal/<gate-b-run-id>/
└─ agent-benchmarks-real/<gate-c-run-id>/
```

最终验证文档至少记录：

- 执行日期、Git revision/dirty、Python 与依赖版本；
- 非密钥 provider/model/base URL/API Key 环境变量名；
- 每关批准的金额、币种、技术预算和实际支出；
- 完整命令（密钥值用环境变量名表示）、退出码和报告绝对路径；
- 每格 stop reason、goal coverage、规则失败、replay、模型调用数和 usage；
- Gate C 六项门槛的分子/分母；
- 首次失败证据、是否重跑、重跑的独立授权与原因；
- “工程链路通过”与“真实模型能力结果”分开表述。

## 8. 验收命令

无付费准备至少运行：

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -m "not postgres" `
  --basetemp artifacts/task-003c-v1-pytest-tmp
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m gamepilot.benchmark run `
  --output-dir artifacts/task-003c-v1-script-benchmark
.venv\Scripts\python.exe -m gamepilot.benchmark agent `
  --output-dir artifacts/task-003c-v1-offline-agent-benchmark
```

真实命令应在实施时根据新增的已验证 CLI 参数写入 validation 文档。不要在任务单里放密钥值，
也不要通过 shell 历史或命令输出确认密钥内容。

数据库路径不在本任务范围，本轮默认不复验 PostgreSQL；若修改领域/API/仓储/数据库或全局依赖，
必须说明原因并补跑 29 项真实 PostgreSQL 回归。

## 9. 完成定义

- 无付费预算/费用支持已经实现并通过完整回归；
- 所有者完成 Agent 原理复盘，但只有所有者本人可以确认是否掌握；
- Gate A、B、C 均分别获得授权并留下不可覆盖的真实报告；
- 真实 12 格结果按首次运行如实给出，成功或能力偏差都不被隐藏；
- Codex 独立检查代码、报告、usage、费用和退出码；
- 更新 README、AGENTS、PHASE_3_PLAN 和 validation 文档；
- 未提交、推送或发布。

在 Gate C 完成前，只能写“真实模型验收进行中”；不得宣称 TASK-003C 最终验收完成。

## 10. 可转交 Claude Code 的提示词

> 请阅读 AGENTS.md、docs/PHASE_3_PLAN.md、
> docs/claude-tasks/TASK-003C-langgraph-test-agent.md、
> docs/claude-tasks/TASK-003C-R1-review-fixes.md 和
> docs/claude-tasks/TASK-003C-V1-real-model-acceptance.md。先只实施第 5 节的无付费预算、
> usage 与费用汇总能力并完成离线回归，不读取或调用真实密钥。不要执行 Gate A/B/C；
> 每道真实模型关卡都必须等待项目所有者另行给出金额/币种上限并明确授权。
> 保持领域/API/仓储/数据库、testing schema/rules 和原 32 组合不变，不扩展 MCP/RAG/UI，
> 不提交或推送。完成后报告修改文件、测试命令与结果，并标记“无付费准备完成，待 Gate A 授权”。

## 11. 首次 Gate A 实际记录

2026-09-21，所有者提供新建密钥并明确要求测试真实链路。Codex 将授权限定为一次 Gate A，
使用 `normal × healing`、seed=42、最多 4 次模型调用、256 输出 Token、1 次格式纠正，
未自动重跑，也未进入 Gate B/C。

结果：真实接口连接成功并返回可靠 usage；两次回复均没有 `tool_use`，本地校验分别记录
`no_tool_call`，格式纠正预算耗尽后以 `model_format_error` / exit 2 停止。实际 2 次模型调用、
0 次游戏动作，prompt=1,142、completion=512、total=1,654 Token，费用因未提供单价为 unknown。
游戏会话创建与初始 GET 成功，零动作前缀的 RunReport 为 pass，但 Agent 目标未完成，
没有被错误当作任务通过。

证据：

- `artifacts/real-model-smoke/gate-a-20260921/20260921T055627Z-574f70-agent.json`
- `artifacts/real-model-smoke/gate-a-20260921/20260921T055627Z-574f70.json`
- 两份文件均未发现形如 API Key 的文本；provider 记录为真实
  `anthropic-compatible / deepseek-v4-flash`，`is_test_double=false`，SDK 重试为 0。

随后 Agent 报告 schema 升级到 1.2，新增供应商 stop reason 和文本回复。诊断复验确认首轮
`stop_reason=max_tokens` 且恰好消耗 1,024 输出 Token，没有文本或 `tool_use`；格式纠正后完成
`attack`、`use_potion`，exit 0 且 replay match。

本次密钥曾直接出现在聊天上下文中。报告和工作区没有保存它，但出于凭据卫生，后续运行前
应撤销该密钥并使用新密钥，且不要再通过聊天或命令参数传递。

## 12. Gate A 修复与最终复验

所有者随后明确授权直接使用密钥修复当前问题。根因证据表明，未指定工具选择时模型会把输出
预算耗在不可见推理上。供应商请求因此增加
`tool_choice={type:any, disable_parallel_tool_use:true}`，与本地“每轮恰好一个工具”校验一致，
并在 `ProviderInfo.sampling` 中留存该非密钥配置。

最终复验使用 512 输出 Token、最多 3 次模型调用、0 次格式纠正。模型第一轮直接调用
`attack`，第二轮调用 `use_potion`；`goal_met` / exit 0，2/2 覆盖条件满足，规则失败 0，
无模型 replay 1/1 match。实际 prompt=382、completion=390、total=772 Token，耗时 3,192.10 ms。
完整证据和三次运行对照见 `docs/validation/TASK-003C-real-model-acceptance.md`。

Gate A 至此通过。其后的 V2 已完成 benchmark 可配置预算与整批费用汇总，Gate B 三目标也已
全部通过；Gate C 仍属于后续独立决策，不能据此宣称 TASK-003C 的完整真实模型验收已经完成。

## 13. Gate B 实际记录

2026-09-21，所有者明确授权直接使用旧密钥验证。按第 6 节固定预算在 localhost normal profile
依次执行 full-health、healing、victory；三条均 exit 0、规则失败 0，并各自无模型 replay match。

| 目标 | 动作 | 模型调用 | 格式纠正 | Token（prompt / completion / total） |
| --- | ---: | ---: | ---: | --- |
| full-health | 1 | 1 | 0 | 289 / 118 / 407 |
| healing | 2 | 3 | 1 | 654 / 970 / 1,624 |
| victory | 4 | 4 | 0 | 1,055 / 542 / 1,597 |
| 合计 | 7 | 8 | 1 | 1,998 / 1,630 / 3,628 |

healing 首轮因 `stop_reason=max_tokens` 没有工具调用，使用了预算允许的唯一一次格式纠正，随后
完成 attack 与 use_potion；这属于有效的受控纠正证据。三条累计耗时 14,455.81 ms。没有提供
可靠单价与币种，因此费用保持 unknown，不能据此计算金额。首次沙箱内尝试因网络隔离发生一次
connection error，usage unknown；随后使用获准联网进程完成上述三条，失败报告保留。

完整证据见 `docs/validation/TASK-003C-real-model-acceptance.md`。密钥未写入报告或工作区文件，
运行进程结束后环境变量已清除；Gate C 未自动执行。
