"""评测报告的数据模型。

与执行器的报告分开：`RunReport` / `ReplayReport` 的 schema 保持不变
（执行器不需要知道 profile，也不需要知道标准答案），
profile 映射、目标规则集合与指标只出现在这里的评测摘要中。

评测摘要用于审计，不是可以自动启动任意目标或执行命令的配置：
它只描述「跑了什么、看到了什么、是否符合清单」。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gamepilot.testing.models import CaseStatus, RunEnvironment

MetricStatus = Literal["ok", "deviation", "execution_error"]
ReplayOutcome = Literal["match", "mismatch", "not_comparable"]


class BenchmarkModel(BaseModel):
    """评测模型：与执行器报告一样拒绝未知字段，保证证据可核对。"""

    model_config = ConfigDict(extra="forbid")


class CombinationEvidence(BenchmarkModel):
    """一个组合的完整证据：原始结论、触发情况、重跑结果与逐项判定。"""

    profile: str
    case_id: str
    fault_id: str | None
    expect_trigger: bool
    expect_fail: bool
    target_rules: list[str]
    run_id: str
    report_path: str
    case_status: CaseStatus
    case_error: str | None = None
    failing_rules: list[str] = Field(default_factory=list)
    target_rules_hit: list[str] = Field(default_factory=list)
    target_rules_missed: list[str] = Field(default_factory=list)
    other_failing_rules: list[str] = Field(default_factory=list)
    triggered: bool = False
    trigger_detail: str | None = None
    unexpected_trigger: bool = False
    replay_run_id: str = ""
    replay_path: str = ""
    replay_outcome: ReplayOutcome = "not_comparable"
    replay_status: CaseStatus
    replay_error: str | None = None
    replay_differences: int = 0
    matrix_ok: bool = False
    notes: list[str] = Field(default_factory=list)


class MetricResult(BenchmarkModel):
    """一项指标：分子、分母、目标与明细。

    分母来自固定清单而不是实际跑到的组合数，因此「没跑到」不会让分母变小。
    """

    metric_id: str
    description: str
    numerator: int
    denominator: int
    target: str
    passed: bool
    details: list[str] = Field(default_factory=list)

    @property
    def ratio(self) -> str:
        return f"{self.numerator}/{self.denominator}"


class NegativeValidation(BenchmarkModel):
    """负向验证：缺陷轨迹在 normal 上重放必须比对出差异。"""

    source_profile: str
    source_case_id: str
    report_path: str
    replay_path: str
    replay_outcome: ReplayOutcome
    execution_status: CaseStatus
    execution_error: str | None = None
    expected_outcome: str = "mismatch"
    actions_identical: bool = False
    differences: int = 0
    passed: bool = False
    note: str = ""


class BenchmarkSummary(BenchmarkModel):
    """评测总览：组合计数与最终结论。"""

    combinations_planned: int
    combinations_executed: int
    as_expected: int
    deviations: int
    # 原始运行的错误数，分母始终为清单中的 32 个组合。
    execution_errors: int
    # 额外的 32 次同 profile 重放和 1 次负向验证分别计数，不混入上述指标。
    replay_execution_errors: int
    negative_execution_errors: int
    metrics_passed: int
    metrics_total: int
    negative_validation_passed: bool
    status: MetricStatus
    exit_code: int


class BenchmarkReport(BenchmarkModel):
    """一次批量评测的完整摘要（不覆盖已有文件）。"""

    benchmark_version: str
    manifest_source: str
    suite: str
    mode: Literal["benchmark"] = "benchmark"
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    timeout_seconds: float
    base_url: str
    profiles: list[str]
    # profile 映射：每个 profile 对应的靶场实例说明（含植入的缺陷内容）。
    # 执行器报告里只有占位地址与场景结论，不含 profile，因此这条映射只存在于摘要中。
    profile_map: dict[str, str]
    target_rules: dict[str, list[str]]
    rules_version: str
    schema_version: str
    rules_source: str
    environment: RunEnvironment
    summary: BenchmarkSummary
    negative_validation: NegativeValidation | None = None
    combinations: list[CombinationEvidence] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)
