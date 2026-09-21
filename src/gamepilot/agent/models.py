"""Agent 任务的数据模型：预算、决策、用量与两份报告的契约。

两份报告刻意分开，职责不同：

1. `gamepilot.testing.models.RunReport`（schema=1.1）：游戏事实与确定性规则检查，
   由 `gamepilot.testing.execution` 产出，可被既有 `replay` 读取；
2. 本模块的 `AgentRunReport`（独立版本号）：目标覆盖、模型决策、预算、停止原因
   与对 RunReport 的引用。

两者用 `run_id` 关联。预算耗尽或模型失败时，已执行前缀的真实规则结果照原样保留，
但 Agent 任务的完成状态由 `AgentSummary.goal_met` / `stop_reason` 表达，
不允许用「前缀 pass」冒充任务通过。

模型可见/不可见的边界不在这里表达，而在 `prompts` 与 `graph` 的输入构造处：
本模块只记录「发生了什么」，包括每轮**实际发给模型**的脱敏输入，便于事后核对。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gamepilot.testing.models import ActionName
from gamepilot.testing.rules import MAX_ACTION_ATTEMPTS

# Agent 报告自己的 schema 版本，与 testing 的 1.1 无关，各自独立演进。
AGENT_SCHEMA_VERSION = "1.1"

# 停止原因：每种都有唯一的判定位置，收尾时只保留最先发生的那一个。
AgentStopReason = Literal[
    "goal_met",  # 覆盖条件全部满足且所有已观察规则通过
    "finish_requested",  # 模型主动申请结束（覆盖不满足时记未完成）
    "rule_failure",  # 独立规则证据确认游戏违规
    "execution_error",  # 游戏侧执行错误（连接/超时/5xx/结构/存储）
    "model_error",  # 模型接口错误（超时、拒绝、429、5xx、网络）
    "model_format_error",  # 格式纠正次数耗尽
    "action_budget",  # 动作尝试预算耗尽
    "model_call_budget",  # 模型调用预算耗尽
    "time_budget",  # 整个任务的总时限耗尽
    "input_error",  # 输入、配置或报告写入错误
]

STOP_REASON_LABELS: dict[str, str] = {
    "goal_met": "覆盖目标已达到且所观察规则全部通过",
    "finish_requested": "模型主动申请结束",
    "rule_failure": "独立规则证据确认游戏违规",
    "execution_error": "游戏侧执行错误",
    "model_error": "模型接口错误",
    "model_format_error": "格式纠正次数耗尽",
    "action_budget": "动作尝试预算耗尽",
    "model_call_budget": "模型调用预算耗尽",
    "time_budget": "任务总时限耗尽",
    "input_error": "输入、配置或报告相关错误",
}


class AgentModel(BaseModel):
    """Agent 报告内使用的基类：未知字段一律拒绝，证据不会被静默忽略。"""

    model_config = ConfigDict(extra="forbid", strict=True)


# --------------------------------------------------------------------- 预算


class BudgetSpec(AgentModel):
    """一次 Agent 任务的预算上限。

    保守默认值来自任务单第 6 节；运行前可显式收紧，任何一项都必须为正。
    动作上限另外不得放宽到超过 testing 既有的 `MAX_ACTION_ATTEMPTS`：
    Agent 生成的 `CaseReport` 必须仍然能被既有 replay 读取。
    """

    max_action_attempts: int = 10
    max_model_calls: int = 12
    max_format_retries: int = 2
    model_timeout_seconds: float = 30.0
    http_timeout_seconds: float = 5.0
    total_timeout_seconds: float = 120.0
    max_output_tokens: int = 512

    @model_validator(mode="after")
    def _check(self) -> "BudgetSpec":
        if not 1 <= self.max_action_attempts <= MAX_ACTION_ATTEMPTS:
            raise ValueError(
                f"max_action_attempts 必须在 1..{MAX_ACTION_ATTEMPTS} 之间"
                f"（不得放宽既有动作上限），当前 {self.max_action_attempts}"
            )
        for name in ("max_model_calls", "max_output_tokens"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正，当前 {getattr(self, name)}")
        if self.max_format_retries < 0:
            raise ValueError(f"max_format_retries 不得为负，当前 {self.max_format_retries}")
        for name in ("model_timeout_seconds", "http_timeout_seconds", "total_timeout_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正，当前 {getattr(self, name)}")
        return self


# ----------------------------------------------------------------- 用量与费用


class TokenUsage(AgentModel):
    """一次或多次模型调用的 Token 用量。

    `available=False` 表示供应商没有返回可用用量——此时三个计数必须保持 None，
    **不能写成 0**：0 是「确认没有消耗」，None 是「不知道」，两者在汇总费用时含义相反。
    """

    available: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "TokenUsage":
        counts = (self.prompt_tokens, self.completion_tokens, self.total_tokens)
        if self.available and any(value is None for value in counts):
            raise ValueError("available=True 时三个计数都必须有值")
        if not self.available and any(value is not None for value in counts):
            raise ValueError("available=False 时三个计数必须留空，不能记 0")
        return self

    @classmethod
    def unknown(cls) -> "TokenUsage":
        return cls(available=False)

    @classmethod
    def of(cls, prompt: int, completion: int, total: int | None = None) -> "TokenUsage":
        return cls(
            available=True,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=completion + prompt if total is None else total,
        )

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        """累加用量；只要有一方未知，合计就记 unknown，不把未知当 0 相加。"""
        if not self.available or not other.available:
            return TokenUsage.unknown()
        assert self.prompt_tokens is not None and other.prompt_tokens is not None
        assert self.completion_tokens is not None and other.completion_tokens is not None
        return TokenUsage.of(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )

    @property
    def label(self) -> str:
        if not self.available:
            return "unknown"
        return (
            f"prompt={self.prompt_tokens} completion={self.completion_tokens}"
            f" total={self.total_tokens}"
        )


class CostEstimate(AgentModel):
    """费用估算。

    只有「供应商计价已知」且「Token 用量已知」时才给出金额；
    否则 `amount` 保持 None 并写明原因。缺少可靠计价时不声称有金额上限，
    这一点在报告里必须能被直接读出来。
    """

    currency: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    amount: float | None = None
    method: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        if self.amount is None:
            return "unknown"
        return f"{self.amount:.6f} {self.currency or '（币种未知）'}"


# --------------------------------------------------------------- 模型与决策


class ToolCallRequest(AgentModel):
    """模型返回的一次工具调用请求。"""

    tool_call_id: str
    name: str
    arguments: dict[str, object] = Field(default_factory=dict)


class ToolResult(AgentModel):
    """回传给供应商的一项工具结果。

    一次模型回复可能包含多个 ``tool_use``。即使整轮会被本地校验拒绝，
    下一条用户消息仍必须为每个 ID 提供对应的 ``tool_result``，否则
    Anthropic 兼容入口会把后续纠正请求判为协议错误。
    """

    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


class ChatMessage(AgentModel):
    """实际发给模型的一条消息（已脱敏）。

    工具调用与工具结果都保留 `tool_call_id`：供应商协议要求工具结果必须与
    对应的 `tool_use` 显式关联，报告里也据此才能看懂「模型看到了什么」。
    """

    role: Literal["system", "user", "assistant"]
    content: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False


class ModelCallRecord(AgentModel):
    """一次模型调用的完整记录：输入、结果、用量与错误分类。"""

    index: int
    kind: Literal["decision", "format_repair"]
    outcome: Literal["tool_call", "no_tool_call", "provider_error"]
    elapsed_ms: float
    usage: TokenUsage
    messages: list[ChatMessage] = Field(default_factory=list)
    error_kind: str | None = None
    error_detail: str | None = None
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)


class DecisionRecord(AgentModel):
    """一次「模型选择 → 程序校验」的结果。

    `accepted=False` 时 `rejection` 写明原因码。被拒绝的请求不会产生任何游戏请求。
    """

    index: int
    model_call_index: int | None = None
    tool: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    action: ActionName | None = None
    accepted: bool = False
    rejection: str | None = None
    detail: str = ""
    summary: str | None = None


# ----------------------------------------------------------------- 目标覆盖


class GoalCondition(AgentModel):
    """覆盖条件的一项：稳定编号 + 是否满足 + 判定依据。"""

    condition_id: str
    met: bool
    detail: str = ""


class GoalCoverage(AgentModel):
    """确定性覆盖结论：条件全部满足才算覆盖目标。

    这是**程序**根据已执行的步骤与通过的规则检查算出来的，
    不是模型自述。`finish(summary)` 里的说明只作为模型观点记录，不参与这里。
    """

    goal_id: str
    goal: str
    met: bool
    conditions: list[GoalCondition] = Field(default_factory=list)

    @property
    def unmet(self) -> list[str]:
        return [item.condition_id for item in self.conditions if not item.met]


# --------------------------------------------------------------- 报告契约


class ProviderInfo(AgentModel):
    """模型供应方信息；只记录非密钥配置。"""

    provider_id: str
    model: str
    base_url: str | None = None
    is_test_double: bool = False
    sampling: dict[str, object] = Field(default_factory=dict)
    api_key_env: str | None = None
    source_revision: str = "unknown"
    source_dirty: bool | None = None


class AgentRunReportRef(AgentModel):
    """对同一 run_id 的 testing 运行报告的引用。"""

    run_id: str
    path: str


class AgentSummary(AgentModel):
    """任务级结论：与游戏前缀的真实规则结果分开表达。"""

    stop_reason: AgentStopReason
    stop_detail: str = ""
    goal_met: bool = False
    completed: bool = False
    exit_code: int = 3
    actions_attempted: int = 0
    model_calls: int = 0
    format_retries: int = 0
    rule_failures: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    usage: TokenUsage = Field(default_factory=TokenUsage.unknown)
    cost: CostEstimate = Field(default_factory=CostEstimate)


class AgentRunReport(AgentModel):
    """一次 Agent 任务的完整报告。

    独立于 testing 的 `RunReport`，不覆盖它，也不改写它的 schema。
    `run_report` 为空表示本次连会话都没创建成功——那时不存在可重跑的游戏轨迹，
    报告只有 Agent 的错误结论。
    """

    schema_version: str = AGENT_SCHEMA_VERSION
    mode: Literal["agent-run"] = "agent-run"
    run_id: str
    goal_id: str
    goal: str
    seed: int
    base_url: str
    started_at: str
    finished_at: str
    duration_ms: float
    provider: ProviderInfo
    budget: BudgetSpec
    prompts_version: str
    rules_version: str
    rules_source: str
    session_id: str | None = None
    run_report: AgentRunReportRef | None = None
    summary: AgentSummary
    coverage: GoalCoverage
    decisions: list[DecisionRecord] = Field(default_factory=list)
    model_calls: list[ModelCallRecord] = Field(default_factory=list)


__all__ = [
    "AGENT_SCHEMA_VERSION",
    "STOP_REASON_LABELS",
    "AgentRunReport",
    "AgentRunReportRef",
    "AgentStopReason",
    "AgentSummary",
    "BudgetSpec",
    "ChatMessage",
    "CostEstimate",
    "DecisionRecord",
    "GoalCondition",
    "GoalCoverage",
    "ModelCallRecord",
    "ProviderInfo",
    "TokenUsage",
    "ToolCallRequest",
    "ToolResult",
]
