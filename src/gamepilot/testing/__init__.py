"""确定性测试执行器与独立规则判定基线。

这个包**不导入任何其他 `gamepilot` 模块**：它把被测服务当成外部 HTTP 服务，
只通过线上响应取证，判定规则独立来自 `docs/PHASE_1_PLAN.md#5-最小游戏规则`，
不从被测实现里取「正确答案」。因此判定器不会因为被测代码写错而一起写错。

模块划分：

- `models`：场景、HTTP 观测、规则检查与报告的数据模型；
- `client`：可注入 `httpx.AsyncClient` 的 HTTP 工具；
- `oracle`：只读请求、预期、快照与事件的纯判定函数；
- `scenarios`：内置场景数据；
- `runner`：顺序执行器与报告写入；
- `replay`：在新会话中重跑报告并逐字段比对；
- `cli`：`run` / `replay` 命令行与退出码。

不包含：LLM、LangGraph、MCP、缺陷开关、并发、自动重试与轨迹最小化。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
