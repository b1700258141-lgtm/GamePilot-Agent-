"""GamePilot 测试 Agent：模型决定动作，程序决定结论。

这一层的职责边界是刻意收窄的：

- **模型的可见输入**只有公开规则、自然语言目标、最新公开快照与剩余预算
  （`prompts`）；它看不到 profile、fault_id、靶场记录器、评测清单、脚本步骤
  与标准触发轨迹；
- **模型能做的事**只有 `perform_action` 与 `finish` 两个工具（`tools`）：
  session_id、URL、seed、输出路径、预算与规则数值都不在它的能力范围内；
- **模型不能决定结论**：动作预期由公开规则在请求之前算出（`planning`），
  是否达成目标由确定性覆盖条件判定（`goals`），违规由既有独立判定器确认
  （`testing.oracle`）。模型的 `finish(summary)` 只是它的观点，不参与判定。

执行能力复用 `gamepilot.testing.execution`，因此 Agent 的产物就是同一份
schema=1.1 的 `RunReport`，可以被既有 replay 原样重跑。本包不导入
`gamepilot.lab`、`gamepilot.benchmark` 与 `gamepilot.testing.scenarios`。

包本体不做导入副作用：`langgraph` 是可选的 `agent` extra，
只有真正执行时才由 `graph` 导入，`--help` 在没有它的环境里也正常工作。
"""

__all__: list[str] = []
