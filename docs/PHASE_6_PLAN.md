# 第六阶段：跨种子与独立运行复现性

状态：2026-10-13，TASK-006A/B/C/D/E/F 本机验收通过；TASK-006E 一次真实单格 canary 通过；TASK-006G 四 seed 胜利门槛离线与一次真实八格通过；远程 CI 尚未运行。

## 当前推进顺序（2026-10-13 重排）

按所有者指定的顺序，每项交付后暂停验收；第①、②项已获所有者验收，
第③项设计稿已交付并按所有者“继续吧”实施，当前等待页面验收。

| 优先级 | 当前状态与执行内容 | 交付和验收条件 |
| --- | --- | --- |
| ① 文档精简与早期报告整理 | 已实施并获所有者验收。 | README 说明运行方式、当前能力与限制；旧报告可从本机归档恢复；V6 与 TASK-006A–G 证据保留。 |
| ② 真实模型测试 | 已完成一次固定八格真实运行并获所有者验收。 | 8/8 执行、正常目标 4/4、误报 0、缺陷触发并检出 4/4、重放 8/8；实际调用与 Token、费用口径及原始报告见 TASK-006G 验收。 |
| ③ Agent 自动测试前端设计与实施 | 已实现，待所有者页面验收；见 `docs/AGENT_TEST_UI_DESIGN.md`。 | 保留手动战斗；自动测试入口展示决策、动作、状态/事件与判定原因；支持缺陷再测，seed 与缺陷配置分开。 |

第②项仅限 seed 0–3 × normal/retaliate_after_death × victory 八格，
沿用 TASK-006G 固定模型与配置。整批最多 48 次模型调用、24,576 请求输出
Token；输入 Token 与金额没有硬上限，没有可靠单价时费用记为 unknown。
执行错误立即止损并保留未执行格；有效测试偏差继续收集固定八格证据。
不自动重跑付费批次，也不扩展为完整 48 格真实评测。偏差按游戏规则失败、
Agent 漏检、目标未完成和执行错误分别解释，不为达到通过率改变固定样本。

2026-10-13 第①项收尾核验：AGENTS、README、本计划及归档索引中的 Markdown
本地链接无缺失；归档 ZIP 可读且 CRC 检查通过，包含原 AGENTS、README 与
11 份早期报告；V6 与 TASK-006A–G 验收文档均存在。本轮未删除文件、未调用
真实模型、未开始前端设计。此后所有者验收第①项并授权第②项固定八格，
一次真实运行的结果与原始证据见 `docs/validation/TASK-006G-victory-seed-gate.md`。
所有者随后验收第②项并要求继续，第③项设计见 `docs/AGENT_TEST_UI_DESIGN.md`。
所有者随后要求继续实施第③项。页面与运行接口已接入，离线流程已验证；下一决策点为所有者页面验收。页面真实模型模式尚未实测，不能与 TASK-006G 命令行八格混为一谈。

第六阶段开始前，真实 Agent 的 V6 证据仅覆盖 seed 42 的一次固定 12 格。先扩大**确定性系统**的
样本范围，核对游戏缺陷在不同随机种子下的真实触发、独立规则判定及逐字段重放。
TASK-006A 的确定性切片不调用模型，因此不宣称 Agent 跨 seed 规划能力、长期稳定性或真实费用。

## 固定设计

- 种子固定为 0–31，不按结果筛选；每个 seed 使用 `attack → use_potion` 与
  `attack → attack → attack` 两条脚本。四个隔离内存 profile 共 256 格。
- 原运行走现有 HTTP 执行器和独立判定器；同 profile 从保存的运行报告无模型重放。
- 靶场触发记录与目标规则失败分开统计。未触发时必须通过；触发时必须失败并命中
  指定规则；正常靶场不能触发或失败。所有格必须重放一致。
- 摘要保留固定计划分母、每格状态、触发缺陷 ID、规则失败、运行及重放报告路径。
  退出码 0 表示符合固定脚本预期，1 表示偏差或缺陷完全未覆盖，2 表示执行错误。
- 在离线 CI 中运行，不连接开发数据库，不读取模型密钥，不修改游戏 API、规则或 `gamepilot.testing` 报告 schema。

运行：`python -m gamepilot.benchmark seed-sweep --output-dir artifacts/seed-sweeps`。
任务要求与本机证据见 `docs/claude-tasks/TASK-006A-cross-seed-scripted-eval.md` 和
`docs/validation/TASK-006A-cross-seed-scripted-eval.md`。

## TASK-006B：两次独立运行对比

TASK-006A 的 replay 在同一个进程中运行，不能单独证明重启 Python 进程后仍相同。
第二次运行使用同一固定清单，`seed-compare` 严格读取两份摘要、16 份逐 profile
运行及重放报告、文件指纹和逐格结论；比较时只排除会话 ID、时间与耗时，
状态、事件、动作和规则检查都参与。CI 连续执行两次并上传全部证据。
不能把同一运行重复引用当作两次独立样本，也不能把损坏或缺失的证据记作漂移。
任务及验收见 `docs/claude-tasks/TASK-006B-independent-seed-reproducibility.md`、
`docs/validation/TASK-006B-independent-seed-reproducibility.md`。

## TASK-006C：四种 seed 的离线 Agent 预检

在 seed 0–3、四个 profile 和三个目标的固定 48 格中，用观测驱动的测试替身
走真实 LangGraph 工作流、HTTP 靶场、独立规则判定、无模型重跑和同 seed 脚本对照。
胜利脚本对 seed 2 使用四次攻击，其余三种 seed 使用三次；固定对照需先通过
真实 HTTP 测试。记录正常目标覆盖、预定缺陷实际触发与条件检出，未触发机会
单独保留，不能算成漏检或检出。发生执行错误时固定分母并停止后续格。

运行：`python -m gamepilot.benchmark agent-seed-preflight --output-dir artifacts/agent-seed-preflights`。
任务及验收见 `docs/claude-tasks/TASK-006C-agent-seed-preflight.md`、
`docs/validation/TASK-006C-agent-seed-preflight.md`。该命令只支持离线测试替身；
它证明跨 seed 工程链路可运行，不代表真实模型能在新 seed 自主规划或检出缺陷。

后续若开展真实 Agent 跨种子评测，须单独冻结任务集、样本量、模型版本、
预算与费用口径，并与上述离线结果分开呈现。

## TASK-006D：缺陷机会可达性

TASK-006C 的 12 个预定缺陷格中，seed 0 的治疗越界格没有实际触发。
不能直接把它当成模型漏检。按每格 6 次动作预算穷举两种公开游戏动作的
全部序列，分别记录可达性与最短触发路径；HTTP 脚本对照再由独立规则
判定器核查。预检报告将计划格、预算内可达格和实际检出格分开统计。
任务与验收见 `docs/claude-tasks/TASK-006D-agent-seed-reachability.md`、
`docs/validation/TASK-006D-agent-seed-reachability.md`。

## TASK-006E：固定新 seed 的单格配对 canary

先固定 `seed 2 × retaliate_after_death × victory` 一格，走现有 Agent、
独立规则判定、无模型重放和同 seed 对照。该格需要四次攻击，能检验
旧 seed 42 三次攻击路径以外的能力。每次最多 6 次模型调用、512 输出
Token/次，合计最多请求 3,072 输出 Token；费用在缺可靠单价时仍为 unknown。
CLI 默认离线，真实模型需要显式 `--provider anthropic-compatible --paid`。
任务与验收见 `docs/claude-tasks/TASK-006E-agent-seed-canary.md`、
`docs/validation/TASK-006E-agent-seed-canary.md`。一次真实 seed 2 单格已用
4 次调用、1,135 Token 检出缺陷并重放一致，金额仍为 unknown；
它不能外推固定 48 格，完整矩阵需另行定义与决策。

## TASK-006F：单格证据可重复核验

对 TASK-006E 摘要和四份原始报告进行只读交叉核验：固定身份与预算、
安全相对引用、游戏动作与规则事实、重放及对照来源、逐次 Token 汇总。
可直接复核既有真实报告，不再次请求供应商。离线 CI 在生成测试替身
canary 后运行同一核验命令。任务及验收见
`docs/claude-tasks/TASK-006F-agent-seed-evidence-verifier.md`、
`docs/validation/TASK-006F-agent-seed-evidence-verifier.md`。

## TASK-006G：四 seed 胜利目标配对门槛

在连续 seed 0–3 固定八格：每个 seed 的 normal 胜利目标与死亡后反击
缺陷胜利目标配对。normal 要求目标覆盖且无误报；四个预定缺陷机会均在
预算内可达，须真实触发并命中目标规则；每格无模型重放与同 seed 脚本
对照。每格最多 6 次模型调用、512 输出 Token/次，整批最多 48 次调用、
最多请求 24,576 输出 Token；输入与金额无已知硬上限。默认离线测试替身，
真实模式须显式付费开关。离线八格 8/8 符合预期；2026-10-13 一次真实八格
执行 8/8、正常目标 4/4、预定缺陷检出 4/4、重放 8/8，30 次模型调用，
输入 6,691 / 输出 1,664 Token，费用 unknown。
任务及验收见 `docs/claude-tasks/TASK-006G-victory-seed-gate.md`、
`docs/validation/TASK-006G-victory-seed-gate.md`。它仅覆盖胜利目标和
一种缺陷，不能替代四 seed 48 格真实评测。
