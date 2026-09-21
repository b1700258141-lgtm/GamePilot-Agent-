# TASK-003C-V2：Agent benchmark 预算与费用证据

日期：2026-09-21。状态：**已由 Codex 实施并验收；无付费工程准备完成，未执行 Gate B/C。**

对应里程碑：TASK-003C 真实模型分级验收的第二个工程切片。Gate A 已通过；在运行 normal
三目标或真实 12 格前，必须先让 benchmark 接受显式预算，并给出不会把未知 Token 当成 0 的
整批 usage/费用摘要。

开始前必须阅读：

- `AGENTS.md`
- `docs/PHASE_3_PLAN.md`
- `docs/claude-tasks/TASK-003C-langgraph-test-agent.md`
- `docs/claude-tasks/TASK-003C-R1-review-fixes.md`
- `docs/claude-tasks/TASK-003C-V1-real-model-acceptance.md`
- `docs/validation/TASK-003C-real-model-acceptance.md`

当前工作区包含尚未提交的 Gate A 修复：AgentRunReport schema 1.2、模型响应诊断字段和
Anthropic `tool_choice=any`。不得回滚、覆盖或绕过这些修改。

## 1. 当前问题

`gamepilot.benchmark agent` 已能完成 4 profile × 3 目标的离线配对试验，但仍有三处阻塞：

1. `_run_cell` 直接使用模块常量 `AGENT_EVAL_BUDGET`，CLI 不能为一次评测收紧动作、模型调用、
   格式纠正、输出 Token 和总时限；报告里的预算不是一次运行的显式输入。
2. CLI 的 `--timeout` 只传给 `GameClient`，没有形成一份统一的 `BudgetSpec`，容易让 HTTP 超时与
   Agent 总预算分裂。
3. 每格已有 `TokenUsage` / `CostEstimate`，但 12 格摘要没有已知/未知格数、总 Token 和总费用；
   未知 usage 不能被静默当成 0。

如果不先修复这些问题，Gate C 最坏调用量无法从命令和报告复核，金额也无法从证据重算。

## 2. 目标

完成一条纯离线、可自动验收的预算与费用证据链：

```text
CLI 参数
  → 严格 BudgetSpec / AgentPricing
  → 同一预算注入 12 个 Agent 单元
  → 每格 AgentRunReport 记录实际预算、usage、cost
  → Agent benchmark 汇总已知/未知格数、总 Token、总费用
  → CLI 清晰打印并把完整配置写入 JSON
```

默认离线命令的行为和 12/12 replay 结果必须保持不变；没有 `--paid` 时仍然零真实模型请求。

## 3. 实施范围

### 3.1 CLI 预算参数

为 `python -m gamepilot.benchmark agent` 增加：

- `--max-actions`
- `--max-model-calls`
- `--max-format-retries`
- `--model-timeout`
- `--http-timeout`
- `--total-timeout`
- `--max-output-tokens`

默认值全部从 `AGENT_EVAL_BUDGET` / `BudgetSpec` 读取，现有离线默认行为不变。现有 Agent 子命令的
`--timeout` 作为 `--http-timeout` 的兼容别名保留，二者写入同一个 argparse `dest`；不要再维护
第二套独立超时值。所有范围校验只复用 `BudgetSpec`，不要在 CLI 复制一套规则。

### 3.2 价格参数与验证

增加：

- `--input-price`：每百万输入 Token 的价格；
- `--output-price`：每百万输出 Token 的价格；
- `--currency`：币种，例如 `CNY` 或 `USD`。

新增一个严格的小模型 `AgentPricing`（名称可等价，但职责不可分散），包含币种和两项非负单价。
三项必须全部缺省或全部给出；只给一项、两项、负数或空白币种均属于输入错误，必须在创建输出
目录和模型供应商之前 exit 2。不得内置或猜测 DeepSeek 价格。

### 3.3 预算贯穿执行链

`run_agent_eval` 显式接收一份 `BudgetSpec` 和可选 `AgentPricing`。同一份配置用于 12 格，但每格
创建独立 `Budget` 计数器，不能跨格共享已消耗次数。

`_run_cell` 必须使用传入预算完成以下工作：

- 构造 `Budget`；
- 设置 `GameClient` HTTP timeout；
- 计算每次模型/HTTP 请求的剩余时限；
- 构造 AgentRunReport 的 `budget`；
- 把可选单价传给 `build_agent_report`，形成逐格 cost。

顶层 AgentEvalReport 的 `budget_seconds` 与 `budget` 必须来自本次参数，不能继续读取模块常量。

### 3.4 整批 usage 与费用口径

在 `AgentEvalSummary` 增加：

- `usage_known_cells`
- `usage_unknown_cells`
- `usage: TokenUsage`
- `cost: CostEstimate`

口径固定如下：

- 分母始终是计划格数；未执行格也属于 usage unknown，不能缩小分母。
- 只有所有计划格的 usage 都可用时，汇总 `usage.available=True` 并累加
  prompt/completion/total Token；否则汇总 usage 必须为 unknown，三个计数保持 `None`。
- 逐格原始 usage 始终保留，因此汇总 unknown 不会删除局部证据。
- 总费用只在全部 usage 可用且 `AgentPricing` 完整时计算；任一条件不满足时 `amount=None`，
  `method` / `note` 必须说明是缺价格还是 Token 不完整。
- 金额按未四舍五入的 Token 总数计算；JSON 保留数值，CLI 展示可以格式化到 6 位小数。
- 不允许把 unknown 当 0，也不允许只合计成功格或已知格后称为整批总费用。

建议提取一个无 I/O 的纯函数汇总 `AgentUsageCell`，便于直接测试部分未知、全部已知和缺价格。

### 3.5 报告与版本

- `AGENT_BENCHMARK_VERSION` 从 1.1.0 升到 1.2.0。
- AgentRunReport 保持 schema 1.2；testing RunReport / ReplayReport 保持 schema 1.1；规则版本不变。
- AgentEvalReport 顶层增加可选 `pricing`，记录币种和两项单价；没有显式单价时为 `null`。
- `provider`、`budget`、`pricing` 均不得包含 API Key、请求头或 SDK 原始对象。
- 报告继续排他创建，不覆盖已有证据。
- CLI 在逐格结果前打印预算，在最终结论附近打印 usage 已知/未知格数、总 Token 和总费用。

## 4. 不在本任务范围

- 不执行真实 Gate B、Gate C 或任何付费模型调用；
- 不修改 prompt、目标定义、Agent 工具、LangGraph 拓扑或 Gate A 的 `tool_choice` 修复；
- 不修改领域规则、公开游戏 API、仓储、数据库、缺陷 profile 或固定 12 格清单；
- 不新增 MCP、RAG、多 Agent、前端、视觉测试或 Docker；
- 不改动原 32 组合脚本 benchmark 的 schema、版本和指标；
- 不提交、推送、发布或删除既有报告。

## 5. 预计修改文件

- `src/gamepilot/benchmark/cli.py`
- `src/gamepilot/benchmark/agent_eval.py`
- `src/gamepilot/benchmark/agent_models.py`
- `src/gamepilot/benchmark/agent_manifest.py`
- `tests/integration/test_agent_eval.py`
- `tests/integration/test_agent_eval_errors.py`（仅在错误口径需要时）
- 必要的窄单元测试文件
- `README.md`
- `AGENTS.md`
- `docs/PHASE_3_PLAN.md`

若需要修改清单之外的功能文件，先在交付报告中说明必要性；不得顺手重构。

## 6. 必测场景

### 6.1 参数与预算

- 默认 CLI 仍得到 `AGENT_EVAL_BUDGET`，离线 12 格行为不变；
- 自定义七项预算逐字段进入顶层报告和每格 AgentRunReport；
- 每格获得新的计数器，但共享同一预算规格；
- `--timeout` 兼容别名与 `--http-timeout` 写入相同字段；
- 非法预算由 `BudgetSpec` 拒绝，exit 2，且不创建输出目录。

### 6.2 usage 与费用

- 12/12 usage 已知：总 Token 精确等于逐格相加；
- 任意一格 usage unknown：汇总 usage unknown，不能出现 0 或部分总数冒充完整总数；
- usage 全部已知且价格完整：总费用可由 Token 和单价复算；
- 缺价格：Token 仍可汇总，但总费用 unknown；
- usage unknown 且价格存在：总费用 unknown，原因是 Token 不完整；
- 负价格、单边价格、缺币种、空白币种：输入错误 exit 2，零模型请求、零产物。

### 6.3 回归与安全

- 默认离线 12 格仍为 12/12 replay match、exit 0，并明确标注测试替身；
- 原 32 组合脚本 benchmark 保持 exit 0；
- `--provider anthropic-compatible` 缺 `--paid` 或缺密钥时继续前置拒绝；
- 测试不得访问公网或使用真实密钥；
- 报告与控制台不出现密钥值。

## 7. 验收命令

至少执行：

```powershell
.venv\Scripts\python.exe -m pytest tests/integration/test_agent_eval.py `
  tests/integration/test_agent_eval_errors.py -q
.venv\Scripts\python.exe -m pytest -m "not postgres" -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .

.venv\Scripts\python.exe -m gamepilot.benchmark run `
  --output-dir artifacts/task-003c-v2-script-benchmark

.venv\Scripts\python.exe -m gamepilot.benchmark agent `
  --provider offline `
  --max-actions 6 `
  --max-model-calls 6 `
  --max-format-retries 1 `
  --model-timeout 30 `
  --http-timeout 5 `
  --total-timeout 60 `
  --max-output-tokens 512 `
  --input-price 1 `
  --output-price 2 `
  --currency TEST `
  --output-dir artifacts/task-003c-v2-agent-benchmark
```

最后一条仍使用离线测试替身，usage 预期可能为 unknown，因此总费用也应为 unknown；这条命令
验证的是参数、预算、报告与 unknown 口径，不是费用金额或 Agent 能力。

本任务不涉及数据库路径，默认不重复 29 项 PostgreSQL 集成测试；如果实际修改领域/API/仓储、
数据库或全局依赖，则必须补跑并说明原因。

## 8. 完成定义

- CLI 能显式控制七项预算，并把同一规格真实传入全部 12 格；
- 价格参数严格 all-or-none，非法输入在任何产物或模型请求前失败；
- 摘要能可靠区分完整 usage、未知 usage、缺价格和可计算费用；
- agent benchmark 1.2.0 的 JSON 与控制台信息一致；
- 离线 12 格、原 32 组合、完整非 PostgreSQL 回归与 Ruff 全部通过；
- README、AGENTS、PHASE_3_PLAN 同步实际结果；
- 未调用真实模型，未提交或推送。

完成本任务后，项目状态应写为“Gate B/C 的预算与费用工程准备完成”；仍不能宣称真实 12 格
验收完成。下一决策点才是轮换密钥、确认金额预算并单独授权 Gate B。

## 9. Claude Code 交付报告要求

完成后请报告：

1. 修改文件；
2. CLI 与 Python API 的最终参数；
3. usage/费用汇总的 unknown 语义；
4. benchmark schema/version 变化；
5. 执行过的命令、退出码和测试数量；
6. 离线 12 格与原 32 组合结果；
7. 未完成事项和风险；
8. 明确声明未调用真实模型、未提交或推送。

## 10. 实际交付与验收

2026-09-21，经所有者授权改由 Codex 直接实施。最终实现了七项 CLI 预算、严格 all-or-none
价格输入、同一预算规格贯穿全部单元、逐格独立计数器，以及保守的整批 usage/费用汇总。
agent benchmark 版本升级为 1.2.0；AgentRunReport 保持 1.2，testing 报告与规则版本不变。

验证结果：

- 窄测试：`48 passed`；
- 全量非 PostgreSQL：`466 passed, 29 deselected`；
- Ruff：检查通过，118 个文件格式一致；
- 离线 12 格：exit 0，12/12 重跑一致；显式预算和 TEST 单价进入顶层及逐格报告；
  离线替身 12/12 usage 未知，因此整批 Token 与费用保持 unknown；
- 原 32 组合脚本 benchmark：exit 0，32/32 符合清单、7/7 指标达标；
- 非法预算、价格缺项、负数、空白币种和非有限价格均在创建产物前 exit 2。

本任务没有修改领域、API、仓储或数据库路径，因此未重复 29 项 PostgreSQL 集成测试；没有调用
真实模型，没有执行 Gate B/C，也没有提交或推送。
