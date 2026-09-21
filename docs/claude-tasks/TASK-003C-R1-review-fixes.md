# TASK-003C-R1：首次 Agent 工作流独立审查返工

日期：2026-09-21。状态：**R1 已由 Codex 完成修复与独立复验，离线工程链路通过；真实模型验收待完成。**

必读 `AGENTS.md`、`docs/claude-tasks/TASK-003C-langgraph-test-agent.md` 和本文件。
本轮只修复下述错误传播、用量、时限与模型协议问题，并同步必要文档；不扩展 MCP、RAG、
多 Agent、数据库、前端或缺陷类型，不提交或推送。真实模型验收仍需所有者确认付费预算。

## 1. P1：Agent 评测把模型错误和重跑执行错误降级

位置：`src/gamepilot/benchmark/agent_eval.py` 的 `build_agent_metrics`、`_status`、
`_run_cell` 与汇总装配，以及 `agent_models.py`。

当前总状态只把 `cell.case_status == "error"` 当执行错误。它没有检查：

- 单格 Agent 报告已经明确给出的 `exit_code == 2` / `stop_reason=model_error`；
- 同 profile 重跑的 `replay_status == "error"` 或报告读取/写入错误；
- 脚本对照组的 `control_case_status == "error"`；
- 其他已有的分阶段错误证据。

同时，`replay_comparable` 把 `not_comparable` 也计入“可比”，而该枚举值的含义正是无法比较。
`replay_mismatch` 只统计 mismatch，因此重跑执行失败不会让任何门槛指标失败。

独立探针已复现：

| 注入 | 单格事实 | 当前总结果 | 正确结果 |
| --- | --- | --- | --- |
| 12 格模型调用均返回 timeout | 每格 `stop_reason=model_error`、exit 2；游戏前缀 case 为 pass | `execution_errors=0`、status=deviation、exit 1 | execution_error、exit 2 |
| 第一格同 profile replay 创建请求返回 503 | `replay_outcome=not_comparable`、`replay_status=error`，其余 11 格 match | status=ok、exit 0、`replay_comparable=12` | execution_error、exit 2、可比 11/12 |

证据位于 `artifacts/review-003c-probes/eval-model-error/` 与
`artifacts/review-003c-probes/eval-replay-error/`。这些本机产物不要求提交。

### 修复要求

1. 分阶段保留并统计 Agent 原始运行、同 profile 重跑、脚本对照及报告 I/O 的执行错误；
   任一阶段错误都必须主导总状态 `execution_error` 和退出码 2。
2. 单格 `exit_code=2` 不能因为对应 RunReport 的游戏前缀是 pass 而被忽略；模型超时、拒绝、
   429/5xx、格式层错误属于评测执行错误，不是“模型能力偏差”或漏检。
3. `not_comparable` 不得计入 `replay_comparable`；可比只包括 match/mismatch。
   单列 match、mismatch、not_comparable、not_executed 和重跑执行错误，分母保持 12。
4. 普通可比较 mismatch 仍为偏差/exit 1；执行错误与 mismatch 同时存在时 exit 2 优先。
5. 脚本对照执行错误不能被当成 Agent 检测结论，也必须进入总错误状态与证据。
6. 评测报告写入/读取错误若无法生成完整摘要，CLI 至少退出 2；若能保留分阶段摘要，
   不得把未执行格从清单分母中删除。

### 必需回归

- 模型 timeout / 429 / 5xx：单格 exit 2，总评测 exit 2，执行错误分阶段可定位。
- 仅同 profile replay 503 / timeout：not_comparable、可比 11/12、总评测 exit 2。
- 仅脚本对照 503 / timeout：总评测 exit 2。
- 重跑 mismatch 且无执行错误：exit 1；执行错误与 mismatch 并存：exit 2。
- 正常离线 12 格仍 exit 0、12/12 match、12/12 可比。
- 集成测试必须穿过真实 `run_agent_eval`、重跑和 CLI，不只直接构造最终 Summary。

## 2. P1：真实供应商返回 usage 时，总用量仍永久为 unknown

位置：`src/gamepilot/agent/graph.py` 的 usage 初值及累加；
`src/gamepilot/agent/models.py` 的 `TokenUsage.plus`。

工作流初始状态把 usage 设为 `TokenUsage.unknown()`，每次调用执行
`unknown.plus(reply.usage)`；而 `plus` 的约定是任一方 unknown 则总计 unknown。
因此即使第一次真实响应明确返回 prompt=100、completion=20，最终仍为 unknown，
显式单价也无法估算费用。这会让真实模型的 Token、费用和预算证据失效。

独立探针结果：供应商返回 `TokenUsage.of(100, 20)`，最终状态仍为
`available=False, prompt_tokens=None, completion_tokens=None, total_tokens=None`。

### 修复要求

区分“尚无任何调用”与“某次调用 usage 不可用”：第一次有值的 usage 应成为累计起点；
后续所有调用均有值时正常相加，只要实际发生过的任一调用没有可靠 usage，最终才降为 unknown。
失败响应若供应商提供了可靠 usage，也要明确是否计入并用测试锁定，不能默默丢弃。

至少增加穿过 graph → report 的测试，证明一次与多次已知 usage 能正确累计、混入 unknown 才降级，
并验证显式单价可基于最终累计值计算。只测试 `TokenUsage.plus` 辅助函数不够。

## 3. P2：总时限耗尽后，初始化或动作内部仍会发出后续 HTTP 请求

位置：`src/gamepilot/agent/budget.py` 的 `request_timeout`，以及
`src/gamepilot/testing/execution.py` 的 `begin`、409 后验证 GET 和对照执行。

`request_timeout` 在剩余时间小于等于 0 时返回 0.001 秒，不抛出 `time_budget`。
`initialize` 也没有在 create 与初始 GET 之间调用 `check_time`；动作的写请求与验证 GET
之间同样没有检查。因此前一个请求耗尽总时限后，程序仍会发送下一次请求，只是给它 1ms 超时。
这违反“超限后不得再发模型或游戏请求”，也可能把 time budget 变成网络 timeout。

独立可控时钟探针：总预算 1 秒，POST create 完成时把时钟推进到 2 秒；实际请求序列仍为
`POST, GET`，之后才以 `time_budget` 停止。

### 修复要求

Agent 使用的 timeout provider 必须在每一次外部 HTTP 请求之前同时执行截止时间检查；
已到期时直接抛出可归类的预算停止，不发请求。覆盖 create → initial GET、动作 → 409 验证 GET，
以及任何对照路径。脚本 runner 没有 Agent 总预算，原行为不应改变。

用可控时钟分别验证初始化和 409 验证两条边界，断言到期后的请求计数没有增加，
且最终停止原因稳定为 `time_budget`，不是 connection/timeout。

## 4. P2：多工具调用的格式纠正消息不符合 Anthropic 工具结果关联

位置：`src/gamepilot/agent/graph.py` 的 `validate`，以及
`src/gamepilot/agent/provider.py` 的 `_to_api_message`。

模型一次返回多个 tool_use 时，本地会正确拒绝游戏执行，但只为列表第一个 tool_call_id
追加 tool_result，随后重新请求模型。实际发给兼容入口的消息因此是：assistant 含两个 tool_use，
下一条 user 只含第一个 tool_result，第二个 tool_use 没有对应结果。测试替身不校验供应商协议，
所以现有测试会通过；真实 Anthropic 兼容入口可能直接拒绝这次格式纠正请求。

独立探针确认：tool_use 为 `multi-a`、`multi-b`，重试输入只有 `multi-a` 的 error tool_result。

### 修复要求

多工具输出虽然不得执行，但格式纠正对话必须对本轮全部 tool_use 保持协议合法关联。
推荐让一条 user 消息携带全部 error tool_result blocks；如选用其他协议合法方式，需要说明并测试。
不要通过只保留第一个 tool_use、执行其中一个动作或吞掉其余调用来解决。

新增一个会校验 Anthropic 请求消息结构的 fake SDK 客户端：第一次返回两个 tool_use，第二次应收到
全部对应的 tool_result，且游戏动作请求仍为 0。测试不能只用忽略消息结构的 `FakeProvider`。

## 5. 文档与范围收尾

当前 README 已称“离线工程链路已交付”，但 `AGENTS.md` 与 `PHASE_3_PLAN.md` 仍写
“未开始实施”，且以上阻塞项说明离线工程部分也尚未通过独立验收。R1 完成前统一标记为：

> TASK-003C 首轮实施完成，独立审查发现阻塞项，R1 待修复；真实模型验收待完成。

修复后更新 README、AGENTS、PHASE_3_PLAN 和本文件验证节。不得把离线测试替身结果称作
真实 Agent 能力成绩；没有真实模型证据时 TASK-003C 仍不能最终验收。

## 6. 首轮独立验证

- 非 PostgreSQL 回归：`427 passed, 29 deselected`；1 条既有依赖弃用警告。
- Ruff：`ruff check .` 与 `ruff format --check .` 通过（112 个文件）。
- `git diff --check` 通过，仅有既有 LF/CRLF 提示。
- 原 32 组合脚本 benchmark：32/32 符合清单、7/7 指标、exit 0。
- 离线 12 格 Agent benchmark：3/3 预定机会、0/3 normal 误报、12/12 replay match、
  6/6 门槛指标、exit 0；这只是正常路径证据，随后被上述故障探针证明错误路径汇总不可靠。
- 真实模型：未调用；当前仍是任务单明确保留的待验收项。
- PostgreSQL：本轮未复验。TASK-003C 未修改领域/API/仓储/数据库路径，Agent extra 为可选依赖；
  数据库回归不用于替代本轮受影响的 testing/benchmark 回归。

首次 pytest 因沙箱无权访问用户 Temp 目录产生 136 个 setup error；显式把 `--basetemp`
放到工作区后完整重跑得到上述 427 项通过，因此该环境错误不计入代码结论。

## 6.1 R1 修复与最终复验

所有者授权 Codex 直接完成本返工。最终实现与证据如下：

- Agent benchmark 升级到 1.1.0，按 Agent 原始运行、同 profile 重跑、脚本对照和报告 I/O
  保留分阶段错误；任一阶段错误优先得到 `execution_error` / exit 2。
- `replay_comparable` 只统计 match/mismatch，摘要分别记录 match、mismatch、
  not_comparable、not_executed 与重跑执行错误。
- Token 累计区分“尚未调用”与“某次 usage 未知”；一次、多次已知用量、混入 unknown、
  失败响应携带可靠 usage，以及显式单价费用估算均有 graph → report 回归。
- 每次 Agent HTTP 请求通过 timeout provider 先检查总时限；创建后初始 GET、409 后验证 GET
  两条边界均用可控时钟证明到期后没有额外请求，停止原因保持 `time_budget`。
- Agent 报告 schema 升级到 1.1；一次多工具回复被拒绝时，下一条 Anthropic user 消息
  一次携带全部 error `tool_result` blocks，fake SDK 客户端验证两个 ID 均闭合且游戏动作请求为 0。
- 异常回归穿过真实 `run_agent_eval`：模型 timeout/429/5xx、真实重跑创建 503、真实脚本
  对照创建 503 和各阶段报告写入失败均得到总评测 exit 2；普通 replay mismatch 保持 exit 1，
  与执行错误并存时仍由 exit 2 主导；正常离线 12 格仍 exit 0、12/12 match。

最终命令与结果：

- `pytest -q -p no:cacheprovider -m "not postgres" --basetemp artifacts/review-003c-r1-final2-full-tmp`：
  `443 passed, 29 deselected`，1 条既有 Starlette/AnyIO 弃用警告。
- `ruff check .` 与 `ruff format --check .`：通过，114 个文件格式正确。
- `python -m gamepilot.benchmark run --output-dir artifacts/review-003c-r1-script-benchmark`：
  32/32 符合清单、7/7 指标、exit 0。
- `python -m gamepilot.benchmark agent --output-dir artifacts/review-003c-r1-agent-benchmark`：
  3/3 预定机会、0/3 normal 误报、12/12 replay match、6/6 门槛指标、exit 0。
- 本轮未调用真实模型、未复验 PostgreSQL、未提交或推送。TASK-003C 的离线工程链路通过；
  DeepSeek `deepseek-v4-flash` 的能力验收仍需所有者确认付费预算后执行。

## 7. 可直接转交 Claude Code 的提示词

> 请阅读 AGENTS.md、docs/claude-tasks/TASK-003C-langgraph-test-agent.md 和
> docs/claude-tasks/TASK-003C-R1-review-fixes.md，按 R1 修复 Agent 评测分阶段错误传播、
> replay 可比口径、真实 usage 累计、总时限请求前检查及多工具 Anthropic 协议关联。
> 补齐要求的真实流程回归，保持 testing schema/rules、原 32 组合、领域/API/仓储/数据库不变；
> 不扩展 MCP/RAG/UI，不调用付费模型，不提交或推送。完成后报告修改文件、每个反例修复前后、
> 测试命令与结果及证据路径，并标记“R1 实施完成，待 Codex 复验”，不要自行标记验收通过。
