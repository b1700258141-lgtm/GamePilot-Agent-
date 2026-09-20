# TASK-003B-R1：独立审查与返工

日期：2026-09-20。状态：项目所有者授权 Codex 直接修复，R1 修复与复验完成，TASK-003B 验收通过。第 1～4 节保留首轮审查记录，最终结论见第 5 节。

必读 AGENTS.md、TASK-003B-fault-lab-and-benchmark.md 和本文件。本轮只修复评测错误传播及必要测试，不开始 003C，不修改正常游戏规则、独立判定器或数据库结构，不提交或推送。

## 1. P2：重放执行错误被降级为普通评测偏差

位置：`src/gamepilot/benchmark/runner.py` 的 `_evidence`、`_run_negative_validation`、`_status` 和 `_summary`，以及相应的 benchmark 报告模型。

当前 `_status` 只读取原始运行的 `case_status`。同 profile 重放仅向摘要保留 outcome，负向验证更被压缩为 `negative.passed` 布尔值；底层 `ReplayCaseReport.execution.status/error` 没有进入评测总状态。

已独立复现两种情况（其他请求仍访问真实的四个隔离 ASGI 靶场）：

| 注入点 | 实际底层结果 | 当前评测结果 | 要求 |
| --- | --- | --- | --- |
| 第一次同 profile 重放的创建请求返回 HTTP 503 | 原始场景 pass；新执行 error；重放 not_comparable | status=deviation、exit_code=1、execution_errors=0，重放 31/32 | 执行错误主导，退出 2 |
| 32 次同 profile 重放全部成功后，负向验证创建请求返回 HTTP 503 | 新执行 error；负向结果 not_comparable | status=deviation、exit_code=1、execution_errors=0，七项指标仍全部达标 | 负向验证未获得可比较结果，退出 2 |

影响：下游 CI 无法通过约定的 1/2 区分“规则或复现结论存在偏差”和“执行环境失败、尚无结论”；摘要还会把重放故障显示成执行错误为零。当前不会错误返回 0，但仍违反任务单第 7 节的 error 优先契约。

### 修复要求

1. 在 benchmark 证据模型中保留原始运行、同 profile 重放、负向验证各阶段是否执行失败及原因，来自已有底层执行报告，不从“是否 mismatch”反推。
2. 任一阶段执行失败时，总状态为 execution_error，CLI 退出 2；即使同时存在漏检、误报或真正的重放 mismatch，也必须以 2 为准。
3. 明确摘要中错误计数的阶段和口径。原有 32 个原始运行的错误指标可以保留，但需明确命名或说明，并单列同 profile 重放和负向验证错误，避免把 33 次额外执行混进原来的分母。
4. 真正的同 profile mismatch 仍为评测偏差、退出 1；负向验证的预期 mismatch 仍通过，意外 match 为偏差、退出 1。不要简单把所有负向验证失败都变成执行错误。
5. 保留所有原始与重放报告；不要通过改写 testing 的 pass/fail/error、修改规则或放宽 replay 校验解决问题。

### 必需回归

- 原始运行成功、仅同 profile replay 返回 503 / timeout：真实执行报告为 error、结果 not_comparable，总退出 2。
- 同 profile 全部成功、仅负向验证返回 503 / timeout：总退出 2，并可定位该阶段的错误证据。
- 重放执行错误与另一个组合的 mismatch 同时出现：2 优先。
- 同 profile 真正 mismatch 但没有执行错误：退出 1；负向验证意外 match：退出 1；正常矩阵：退出 0。
- 至少有集成测试穿过真实 replay 执行和 benchmark CLI，不只构造 `_summary` 的参数或直接伪造最终退出码。

复现思路：包装 benchmark runner 使用的 `replay_report`，正常情况下调用原函数；在指定调用处，给原函数注入使用 MockTransport 的 GameClient，使创建请求返回 HTTP 503。第一次调用对应 normal/initial-state 的重放，第 33 次对应负向验证。响应注入仅用于此异常路径测试，不替代缺陷靶场。

本机证据：`artifacts/review-003b-evidence/replay-error-results.json`；两个完整评测目录位于其 `replay-error-1/`、`replay-error-33/` 下。以上复现说明不依赖提交本机产物。

## 2. 独立验证与适用边界

- 非 PostgreSQL 回归：293 passed，29 deselected；两条依赖弃用警告。
- Ruff 静态和格式检查均通过，84 个文件。
- 独立 benchmark CLI：32/32 组合符合清单，缺陷覆盖 3/3，真实触发与目标检测均 9/9，正常误报 0/8、未触发变体误报 0/15、原始执行错误 0/32，同 profile 重放 32/32，负向验证 mismatch；退出 0。
- 四个 profile 的真实 localhost HTTP 独立 CLI：normal run 退出 0，三个缺陷 run 均退出 1；各自 replay 均退出 0，均 8/8 match。服务由本次验证创建并在 finally 中关闭，证据在 `artifacts/review-003b-evidence/http/`。
- 正常领域实现与 Git HEAD 的修改前实现直接对比：7 个种子、21 条轨迹、140 次动作前缀快照与异常完全一致，覆盖胜负、药水、非法动作和后续随机进度。并非只拿新实现与自身 replay 比较。
- 真实 PostgreSQL 回归：29 passed，293 deselected。首次连接因本机 WSL/PostgreSQL 不可达出现 29 项 setup error；临时保持 WSL 会话、待 TCP 可达后，显式指定既有 `gamepilot_test` 并完整重跑通过。保活进程已关闭，未更改数据库配置、开发数据或迁移。日志在 `artifacts/review-003b-evidence/postgres.log`。

已核对三个缺陷来自真实领域状态变异，GET 可读到对应历史和终态；正常入口与 lab 实例隔离，testing 没有导入 lab 或 benchmark 标准答案。本轮未修改功能代码，未提交或推送。

## 3. 非阻塞收尾

原任务要求在可用时保存源码版本，目前 BenchmarkReport 仅有 benchmark/rules/schema 版本和 Python/平台信息。建议增加可空的源码 revision 与 dirty 标记，缺少 Git 时明确不可用且不阻断评测；未提交的实现不能仅靠 HEAD SHA 冒充精确源码版本。

本审查只证明固定脚本基线与指定的三个缺陷，不等于 Agent 自主发现能力，也不代表所有网络异常组合均已覆盖。

## 4. 可直接转交 Claude Code 的提示词

请阅读 AGENTS.md、docs/claude-tasks/TASK-003B-fault-lab-and-benchmark.md 和 docs/claude-tasks/TASK-003B-R1-review-fixes.md，修复 R1 中重放执行错误没有主导 benchmark 退出码的问题。按要求保留分阶段错误证据和明确的指标分母，补齐真实 replay 到 CLI 的异常路径回归，确保普通 mismatch 仍退出 1、执行错误退出 2、正常矩阵退出 0。保持 testing 报告契约、规则、靶场和数据库结构不变，不开始 003C，不提交或推送。完成后报告修改文件、复现前后结果、测试命令与结果、报告路径及限制，并标记“返工实施完成，待 Codex 复验”，不要自行标记独立验收通过。

## 5. Codex 修复与最终复验

项目所有者改为授权 Codex 直接实施。本次修改仅涉及 benchmark、相关测试和文档，未改 testing 报告契约、判定规则、领域层、路由、lab、仓储或数据库。

### 实施结果

- 组合证据新增原始运行错误原因，以及同 profile 重放的实际执行状态和原因；负向验证同样保留新执行状态和原因。全部来自底层执行报告，与比较 outcome 分开处理。
- 总状态直接接收负向验证完整证据，检查原始运行和全部重放阶段；任一执行错误主导退出 2，普通 mismatch / 负向意外 match 保持退出 1。
- 摘要保留原始运行 `execution_errors`，新增 `replay_execution_errors` 与 `negative_execution_errors`；CLI 分别显示 /32、/32、/1 和错误证据路径。七项固定指标不增删、不改变分母。
- benchmark 版本升级为 1.1.0；RunReport / ReplayReport 的 schema=1.1、规则版本=1.1.0 不变。原始错误导致不可比较但重放实际成功时，不重复计重放执行错误。
- 新增 `tests/integration/test_benchmark_replay_errors.py`，共 9 个集成用例覆盖两个阶段的 503/timeout、执行错误与真实 mismatch 并存、普通 mismatch / 负向 match，以及原始错误后重放成功。测试经过真实 replay、CLI 与报告落盘，不伪造最终判定。

### 验证结果

- 定向回归：68 passed。
- 完整非 PostgreSQL 回归：302 passed，29 deselected；2 条既有依赖弃用警告。
- `ruff check .` 和 `ruff format --check .` 通过（86 个文件）；`git diff --check` 无空白错误，仅既有换行格式提示。
- 独立 CLI benchmark：32/32 组合符合预期、9/9 目标检出、32/32 replay match、负向 mismatch；7/7 指标达标、三个阶段错误数均 0、退出 0。
- 复跑首轮两个 HTTP 503 探针：两者均由 deviation/1 变为 execution_error/2。同 profile 错误计为 0/1/0，负向验证错误计为 0/0/1（顺序为原始运行/同 profile/负向验证）。原有失败证据未覆盖。

固定矩阵报告：`artifacts/verify-003b-r1-benchmark/20260920T025011Z-de38f7/benchmark.json`。

反例复验：`artifacts/verify-003b-r1-errors-1ccb9d96f1374a298ad095ae1048a894/results.json`，内含完整报告路径。

本轮没有重复 PostgreSQL 和四个 profile 的真实网络演示；上轮独立审查已完成 29 项真实 PostgreSQL 回归及四个 profile 的 HTTP run/replay，本轮对应实现均未改变。新增 503/timeout 路径使用可控传输故障注入；它验证错误传播，不代表对所有真实网络环境做了故障演练。

结论：P2 已关闭，TASK-003B 验收通过；尚需完成所有者学习复盘。第 3 节源码 revision/dirty 元数据仍作为非阻塞追溯增强建议保留，本次未扩展实现。未提交、推送或启动 003C。
