# 第六阶段首个切片：跨种子离线脚本评测

状态：2026-10-12，TASK-006A 本机验收通过；远程 CI 尚未运行。

真实 Agent 的 V6 证据仅覆盖 seed 42 的一次固定 12 格。先扩大**确定性系统**的
样本范围，核对游戏缺陷在不同随机种子下的真实触发、独立规则判定及逐字段重放。
此切片不调用模型，因此不宣称 Agent 跨 seed 规划能力、长期稳定性或真实费用。

## 固定设计

- 种子固定为 0–31，不按结果筛选；每个 seed 使用 `attack → use_potion` 与
  `attack → attack → attack` 两条脚本。四个隔离内存 profile 共 256 格。
- 原运行走现有 HTTP 执行器和独立判定器；同 profile 从保存的运行报告无模型重放。
- 靶场触发记录与目标规则失败分开统计。未触发时必须通过；触发时必须失败并命中
  指定规则；正常靶场不能触发或失败。所有格必须重放一致。
- 摘要保留固定计划分母、每格状态、触发缺陷 ID、规则失败、运行及重放报告路径。
  退出码 0 表示符合固定脚本预期，1 表示偏差或缺陷完全未覆盖，2 表示执行错误。
- 在离线 CI 中运行，不连接开发数据库，不读取模型密钥，不修改 API、规则或报告 schema。

运行：`python -m gamepilot.benchmark seed-sweep --output-dir artifacts/seed-sweeps`。
任务要求与本机证据见 `docs/claude-tasks/TASK-006A-cross-seed-scripted-eval.md` 和
`docs/validation/TASK-006A-cross-seed-scripted-eval.md`。

后续若开展真实 Agent 跨种子评测，须单独冻结任务集、样本量、模型版本、
预算与费用口径，并与本离线脚本结果分开呈现。
