# TASK-003C-V3：DeepSeek Flash 真实 Gate C 固定 12 格验收

日期：2026-09-21。状态：**规划完成，待单独授权执行；本任务不自动使用 Gate B 的授权。**

对应里程碑：完成 TASK-003C 的最后一道真实模型能力关卡。Gate A 已证明单格协议兼容，Gate B
已证明 normal 三目标均可完成，V2 已补齐 12 格 benchmark 的显式预算、Token 和费用证据。本任务
不再新增 Agent 功能，只执行固定 4 profile × 3 目标并形成最终真实模型结论。

开始前必须阅读：

- `AGENTS.md`
- `docs/PHASE_3_PLAN.md`
- `docs/claude-tasks/TASK-003C-V1-real-model-acceptance.md`
- `docs/claude-tasks/TASK-003C-V2-benchmark-budget-cost.md`
- `docs/validation/TASK-003C-real-model-acceptance.md`

## 1. 当前证据与决策

- 真实模型固定为 `deepseek-v4-flash`，Anthropic 兼容入口为
  `https://api.deepseek.com/anthropic`；请求强制单工具选择并禁用并行工具调用。
- Gate A 最终有效运行：2 次调用，prompt 382、completion 390、total 772 Token。
- Gate B 三条有效运行：8 次调用，prompt 1,998、completion 1,630、total 3,628 Token，
  三目标均 exit 0、规则失败 0、replay 3/3 match。
- Gate A/B 有效证据合计 10 次调用、4,400 Token；没有可靠单价，金额仍为 unknown。
- Gate C 使用现有 `gamepilot.benchmark agent` 的进程内 ASGI 四 profile 隔离。真实模型调用仍通过
  外部 DeepSeek API。Gate A/B 已用 localhost 验证实际游戏 HTTP 客户端链路，Gate C 不重复搭建
  四个 localhost 服务，以免把服务生命周期噪声混入 Agent 能力矩阵。

上述最后一项取代 V1 第 7 节“所有真实运行都必须 localhost”的过宽表述：localhost 要求已由
Gate A/B 满足；Gate C 的目标是固定矩阵、缺陷检出和整批费用证据，不重复验收同一网络路径。

## 2. 固定范围

只执行现有清单中的 12 格：

```text
normal × {full-health, healing, victory}
potion_overheal × {full-health, healing, victory}
potion_not_consumed × {full-health, healing, victory}
retaliate_after_death × {full-health, healing, victory}
```

不修改 prompt、目标、工具、图拓扑、领域规则、缺陷 profile、评测清单或指标。出现能力偏差时如实
记录 exit 1，不为了“跑绿”临时改提示词并重跑；出现接口或工程错误时记录 exit 2，停止本任务，
由所有者另行决定是否重跑。

## 3. 预算与费用边界

每格固定：

- max actions：6
- max model calls：6
- max format retries：1
- max output tokens：512
- model timeout：30 s
- HTTP timeout：5 s
- total timeout：60 s

整批技术上限为 72 次模型调用和 36,864 completion Token。输入 Token 没有供应商侧硬上限，
但受 72 次调用、逐格上下文和 60 秒逐格总时限共同约束。

基于 Gate A/B 的只读估算：

| 口径 | prompt | completion | total |
| --- | ---: | ---: | ---: |
| 三目标按四个 profile 线性外推 | 7,992 | 6,520 | 14,512 |
| 10 次有效调用均值外推到 72 次 | 17,136 | 14,544 | 31,680 |
| 协议硬上限 | 无独立硬上限 | 36,864 | 无可靠硬上限 |

执行前必须由所有者给出输入单价、输出单价、币种和可接受最大金额，或者明确放弃本轮金额验收、
只保留实际 Token 证据。项目不得猜测供应商价格。费用公式：

```text
amount = prompt_tokens × input_price_per_million / 1,000,000
       + completion_tokens × output_price_per_million / 1,000,000
```

## 4. 安全与止损

- Gate C 必须获得独立授权；Gate B 授权不自动延续。
- 密钥只通过隐藏输入注入当前进程环境，不写入命令行、文件或报告；进程结束后立即清除。
- SDK 保持 `max_retries=0`，不发生隐式付费重试。
- 不在 Gate C 前重复 Gate A/B；若距 Gate B 时间较长，只做一次最小连通预检并单独记录。
- benchmark 固定最多 72 次调用。一次 Gate C 命令结束后，无论 exit 0/1/2 都不自动发起第二轮。
- 报告完成后扫描全部 JSON，确认没有形如 API Key 的文本，并停止所有本地辅助进程。

当前 benchmark 会顺序跑完清单，不具备格间 fail-fast；其风险由 72 次调用和 36,864 输出 Token
硬上限约束。若所有者不能接受该最坏边界，应先另立小任务实现“可审计的中止摘要”，不得在本任务
中临时插入未经测试的中止逻辑。

## 5. 执行命令

以下是审核模板，不在规划阶段执行。密钥变量必须通过隐藏输入在进程内设置，不能把值替换进文档：

```powershell
.venv\Scripts\python.exe -m gamepilot.benchmark agent `
  --provider anthropic-compatible --paid `
  --model deepseek-v4-flash `
  --model-base-url https://api.deepseek.com/anthropic `
  --api-key-env GAMEPILOT_AGENT_API_KEY `
  --max-actions 6 --max-model-calls 6 --max-format-retries 1 `
  --model-timeout 30 --http-timeout 5 --total-timeout 60 `
  --max-output-tokens 512 `
  --input-price <每百万输入 Token 单价> `
  --output-price <每百万输出 Token 单价> `
  --currency <币种> `
  --output-dir artifacts/agent-benchmarks-real/gate-c-20260921
```

如果所有者明确放弃金额验收，三项价格参数必须全部省略，报告金额保持 unknown；不能只给其中一项。

## 6. 验收标准

工程门槛：

- 计划与执行均为 12 格，执行错误 0/12；
- provider 为真实 `anthropic-compatible`，模型与入口正确，`is_test_double=false`；
- 每格预算与顶层预算完全一致；usage 12/12 已知，整批 Token 可由逐格精确相加；
- 同 profile 无模型 replay 12/12 match、12/12 可比；
- 报告不含密钥，运行后环境变量已清除。

能力门槛：

- normal 误报 0/3；
- 三个预定缺陷机会检出 3/3；
- 去重缺陷覆盖 3/3；
- 未触发变体误报 0/6；
- 六项门槛指标全部达标，benchmark exit 0。

费用门槛：

- 若提供完整计价，整批金额可由实际 prompt/completion Token 复算且不超过授权金额；
- 若明确放弃金额验收，只能报告 Token 实耗与 cost unknown，不能宣称费用验收完成。

能力未达标且无工程错误时，exit 1 是有效结论，不等同于实现缺陷。exit 2 表示没有获得完整能力
结论，应保留证据并停止，不自动重跑。

## 7. 交付物

- 不可覆盖的 `agent-benchmark.json`；
- 12 格 AgentRunReport、RunReport、ReplayReport 与脚本对照报告；
- `docs/validation/TASK-003C-real-model-acceptance.md` 的 Gate C 记录；
- `AGENTS.md`、`README.md`、`docs/PHASE_3_PLAN.md` 的最终状态；
- 一份明确区分工程结论、能力结论、Token 与金额结论的交付说明。

## 8. 不在本任务范围

- MCP、RAG、多 Agent、长期记忆、视觉测试、前端或新缺陷；
- 修改数据库、Docker、公开 API 或仓储；
- 根据结果调参后自动重跑；
- 提交、推送、发布或删除旧报告；
- 自动进入下一阶段功能开发。

## 9. 完成定义

Gate C 只在 12 格工程门槛与能力门槛全部满足时通过。金额验收是否完成单独标记。执行完成后，
TASK-003C 才能给出“真实模型固定 12 格小样本通过”的结论；这仍是固定任务集的一次样本，不能外推为
通用游戏测试能力。随后应先完成学习复盘，再规划 MCP 或新的项目阶段。
