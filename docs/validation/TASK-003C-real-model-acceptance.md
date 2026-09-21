# TASK-003C 真实模型验收记录

日期：2026-09-21。当前结论：**Gate A 与 normal 三目标 Gate B 已通过；Gate C 尚未执行，完整真实模型验收仍在进行中。**

## Gate A：normal × healing

### 首次运行：保留的失败证据

首次运行使用 256 输出 Token、最多 4 次模型调用和 1 次格式纠正。真实接口、认证与 usage
链路成功，但两轮均为 `no_tool_call`，最终 `model_format_error` / exit 2；0 个游戏动作，
prompt=1,142、completion=512、total=1,654 Token。证据：

- `artifacts/real-model-smoke/gate-a-20260921/20260921T055627Z-574f70-agent.json`
- `artifacts/real-model-smoke/gate-a-20260921/20260921T055627Z-574f70.json`

### 诊断运行：确认根因

Agent 报告 schema 升级到 1.2，模型调用记录新增 `provider_stop_reason` 与 `response_text`。
在 1,024 输出 Token 下，首轮仍恰好耗尽 1,024 Token，`stop_reason=max_tokens`，且没有文本或
`tool_use`；格式纠正后的两轮分别使用 144 和 105 Token，正确调用 `attack`、`use_potion`。
本次 exit 0、目标覆盖成立、replay match；总用量 prompt=651、completion=1,273、total=1,924，
耗时 8,970.86 ms。证据：

- `artifacts/real-model-smoke/gate-a-diagnostic-20260921/20260921T062359Z-fe7672-agent.json`
- `artifacts/real-model-smoke/gate-a-diagnostic-20260921/20260921T062359Z-fe7672.json`
- `artifacts/real-model-smoke/gate-a-diagnostic-20260921/replay/20260921T062524Z-047404.json`

### 修复后运行：Gate A 通过

供应商请求现在显式使用 Anthropic 工具约束
`tool_choice={type:any, disable_parallel_tool_use:true}`，与工作流“每轮恰好一个工具”的协议一致。
复验把上限收紧为 512 输出 Token、3 次模型调用、0 次格式纠正，结果如下：

| 项目 | 结果 |
| --- | --- |
| Provider | `anthropic-compatible`，真实供应商，非测试替身 |
| Model / Base URL | `deepseek-v4-flash` / `https://api.deepseek.com/anthropic` |
| 目标 / seed | `healing` / 42 |
| 预算 | actions 3；model calls 3；format retries 0；output 512；total 60 s |
| 实际耗时 | 3,192.10 ms |
| 模型调用 | 2；两次均为 `tool_call` / `stop_reason=tool_use` |
| 游戏动作 | `attack`、`use_potion` |
| Token | prompt 382；completion 390；total 772 |
| 费用 | unknown；本次未提供可靠单价与币种 |
| 停止 / 退出码 | `goal_met` / 0 |
| 目标覆盖 | 2/2 条件满足，规则失败 0 |
| 无模型 replay | 1/1 match，exit 0 |

证据：

- `artifacts/real-model-smoke/gate-a-tool-choice-20260921/20260921T062814Z-75ccde-agent.json`
- `artifacts/real-model-smoke/gate-a-tool-choice-20260921/20260921T062814Z-75ccde.json`
- `artifacts/real-model-smoke/gate-a-tool-choice-20260921/replay/20260921T062842Z-7f436f.json`

## 判定

- Gate A 通过：真实模型完成两个原生工具调用，目标覆盖与独立规则判定通过，运行报告可无模型重放。
- 首次失败根因是模型在未强制工具选择时耗尽输出预算；协议级 `tool_choice` 修复后无需格式纠正，
  调用数、Token 与耗时均下降。
- Gate C 固定 12 格和整批费用验收尚未执行；离线 12 格不能作为真实能力成绩。
- 三次运行均保持 SDK `max_retries=0`。报告与工作区扫描未发现形如 API Key 的文本，进程环境变量
  已在命令结束后清除。

密钥曾直接出现在聊天上下文中。完成本轮后应撤销或轮换该密钥，后续不要通过聊天或命令参数传递。

## Gate B：normal 三目标

所有者在 2026-09-21 明确授权直接使用旧密钥验证。三项目标均使用 seed 42、512 输出 Token、
1 次格式纠正、30 秒模型超时、5 秒 HTTP 超时和 60 秒总时限；动作与调用上限分别按任务单设置。

| 目标 | 结果 | 动作 / 调用 / 纠正 | Token（prompt / completion / total） | replay |
| --- | --- | --- | --- | --- |
| full-health | goal_met / exit 0 | 1 / 1 / 0 | 289 / 118 / 407 | 1/1 match |
| healing | goal_met / exit 0 | 2 / 3 / 1 | 654 / 970 / 1,624 | 1/1 match |
| victory | goal_met / exit 0 | 4 / 4 / 0 | 1,055 / 542 / 1,597 | 1/1 match |
| 合计 | 三目标通过 | 7 / 8 / 1 | 1,998 / 1,630 / 3,628 | 3/3 match |

三条报告均标记 `anthropic-compatible`、`deepseek-v4-flash`、非测试替身，目标覆盖成立且规则失败为 0。
healing 首轮返回 `stop_reason=max_tokens` 且没有工具调用，随后使用唯一一次格式纠正完成两个动作；
没有超出预算。费用因没有可靠单价和币种继续记录为 unknown。

证据：

- `artifacts/real-model-normal/gate-b-20260921/full-health/20260921T080649Z-6d5a90-agent.json`
- `artifacts/real-model-normal/gate-b-20260921/full-health/replay/20260921T080757Z-40f5d2.json`
- `artifacts/real-model-normal/gate-b-20260921/healing/20260921T080658Z-4e94d4-agent.json`
- `artifacts/real-model-normal/gate-b-20260921/healing/replay/20260921T080800Z-6091d7.json`
- `artifacts/real-model-normal/gate-b-20260921/victory/20260921T080712Z-8a6e49-agent.json`
- `artifacts/real-model-normal/gate-b-20260921/victory/replay/20260921T080803Z-be07cb.json`

首次沙箱内尝试因网络隔离在 full-health 第一次模型调用时得到 connection error，usage unknown，
未继续后两项目标；失败证据保留在同一 full-health 目录。随后获准的联网进程完成上述有效结果。
全部 Agent 报告扫描未发现形如 API Key 的文本，环境变量已清除，本地服务已停止。Gate C 未执行。
