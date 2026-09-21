"""Agent 评测报告的数据模型。

与 `benchmark.models` 分开：那套模型描述的是「脚本 × 缺陷」矩阵，
这套描述的是「Agent × 缺陷」配对试验，两者的分母与结论含义不同，
混在一起会让「32 组合的脚本成绩」和「12 格的 Agent 成绩」互相冒充。

关键约定：

- **分母来自固定清单**，不来自实际跑到的格数：没跑到、执行错误都不缩小分母；
- **`not_applicable` 是合法结论**：变体未触发时的误报指标在分母为 0 时就该标 N/A，
  而不是记成 0/0 通过；
- **Token/费用未知不参与合计**：`usage_available=False` 的格子在汇总里只报未知格数，
  不按 0 计入总费用。
"""

from typing import Literal

from pydantic import Field

from gamepilot.agent.models import CostEstimate, TokenUsage
from gamepilot.testing.models import CaseStatus, RunEnvironment

from .models import BenchmarkModel

# 比脚本重跑多一种结论：Agent 可能连可重跑的会话都没创建成功。
AgentReplayOutcome = Literal["match", "mismatch", "not_comparable", "not_executed"]

AgentMetricStatus = Literal["ok", "deviation", "execution_error", "not_applicable"]


class AgentUsageCell(BenchmarkModel):
    """一格的实际开销。unknown 照实记录，不填 0。"""

    actions_attempted: int = 0
    model_calls: int = 0
    format_retries: int = 0
    duration_ms: float = 0.0
    usage: TokenUsage = Field(default_factory=TokenUsage.unknown)
    cost: CostEstimate = Field(default_factory=CostEstimate)


class AgentCellEvidence(BenchmarkModel):
    """一格（profile × 目标）的完整证据：Agent 结论、对照结论与无模型重跑。"""

    profile: str
    goal_id: str
    goal: str
    fault_id: str | None
    is_designated: bool
    target_rules: list[str]

    # Agent（实验组）
    run_id: str
    agent_report_path: str
    agent_report_error: str | None = None
    run_report_path: str | None = None
    run_report_error: str | None = None
    stop_reason: str
    exit_code: int
    goal_met: bool = False
    completed: bool = False
    case_status: CaseStatus | None = None
    case_error: str | None = None
    accepted_actions: list[str] = Field(default_factory=list)
    spend: AgentUsageCell = Field(default_factory=AgentUsageCell)

    # 独立判定与靶场触发
    failing_rules: list[str] = Field(default_factory=list)
    target_rules_hit: list[str] = Field(default_factory=list)
    target_rules_missed: list[str] = Field(default_factory=list)
    other_failing_rules: list[str] = Field(default_factory=list)
    triggered: bool = False
    trigger_detail: str | None = None
    # 不在预定机会清单里、但缺陷真实触发且规则确实失败。
    # 对脚本矩阵来说「计划外触发」意味着清单假设有误；对 Agent 来说这是额外检出，
    # 不是异常标记——两者不能共用一个字段名。
    beyond_designated: bool = False

    # 同 profile 无模型重跑
    replay_run_id: str = ""
    replay_path: str = ""
    replay_outcome: AgentReplayOutcome = "not_executed"
    replay_status: CaseStatus | None = None
    replay_error: str | None = None
    replay_differences: int = 0

    # 对照组（原脚本场景，同样 seed/profile/新会话/动作上限/oracle；不消耗模型预算）
    control_case_id: str = ""
    control_run_id: str = ""
    control_report_path: str = ""
    control_case_status: CaseStatus | None = None
    control_case_error: str | None = None
    control_report_error: str | None = None
    control_action_count: int = 0
    control_duration_ms: float = 0.0
    control_failing_rules: list[str] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)

    @property
    def detected(self) -> bool:
        """一次真实检出：靶场确认触发，且目标规则确实失败。"""
        return self.triggered and bool(self.target_rules_hit)


class AgentMetricResult(BenchmarkModel):
    """一项指标：分子、分母、目标与明细。

    `not_applicable` 表示分母为 0（例如没有任何未触发的变体格），
    这时不声称通过也不声称未通过。

    `required` 区分**门槛项**与**信息项**：目标覆盖等只是「如实列出」，
    不能拿来抬高通过率，因此不计入通过数，也不参与最终状态判定。
    指标自己声明性质，汇总与命令行都不再另维护一份清单。
    """

    metric_id: str
    description: str
    numerator: int
    denominator: int
    target: str
    status: AgentMetricStatus
    required: bool = True
    details: list[str] = Field(default_factory=list)

    @property
    def ratio(self) -> str:
        if self.denominator == 0:
            return "N/A"
        return f"{self.numerator}/{self.denominator}"

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "not_applicable")


class AgentEvalSummary(BenchmarkModel):
    """总览：计数、指标与最终状态。分母全部来自清单。"""

    cells_planned: int
    cells_executed: int
    goals_met: int
    goals_incomplete: int
    agent_execution_errors: int
    replay_execution_errors: int
    control_execution_errors: int
    execution_errors: int
    designated_opportunities: int
    designated_detected: int
    distinct_defects_detected: int
    normal_false_positives: int
    replay_matches: int
    replay_comparable: int
    replay_mismatches: int
    replay_not_comparable: int
    replay_not_executed: int
    provider_is_test_double: bool
    # 只统计门槛指标：信息项不能抬高通过率。
    metrics_passed: int
    metrics_total: int
    status: AgentMetricStatus
    exit_code: int
    # 真实模型的验收门槛与工程门槛分开表达，不混成一个通过/失败。
    acceptance_note: str = ""


class AgentEvalReport(BenchmarkModel):
    """一次 12 格配对试验的完整摘要（不覆盖已有文件）。"""

    agent_benchmark_version: str
    manifest_source: str
    mode: Literal["agent-benchmark"] = "agent-benchmark"
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    seed: int
    base_url: str
    profiles: list[str]
    profile_map: dict[str, str]
    goal_ids: list[str]
    goal_texts: dict[str, str]
    budget_seconds: float
    budget: dict[str, object] = Field(default_factory=dict)
    provider: dict[str, object] = Field(default_factory=dict)
    rules_version: str
    schema_version: str
    rules_source: str
    environment: RunEnvironment
    summary: AgentEvalSummary
    cells: list[AgentCellEvidence] = Field(default_factory=list)
    metrics: list[AgentMetricResult] = Field(default_factory=list)


__all__ = [
    "AgentCellEvidence",
    "AgentEvalReport",
    "AgentEvalSummary",
    "AgentMetricResult",
    "AgentMetricStatus",
    "AgentReplayOutcome",
    "AgentUsageCell",
]
