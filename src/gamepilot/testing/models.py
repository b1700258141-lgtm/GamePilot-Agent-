"""测试执行器的数据模型：场景、HTTP 观测、规则检查与报告。

这些模型只描述数据。它们**不复用** `gamepilot.domain.models` 里的响应模型，
而是独立描述 HTTP 线上格式：判定器必须能看到「服务器实际返回了什么」，
而不是被同源的模型先清洗一遍。

字段类型刻意放宽（如 actor/kind/status 用 str 而非 Literal）：
规则不符应当被判定器报成 fail 并给出 rule_id，而不是在解析阶段
变成执行错误，否则游戏缺陷会被伪装成基础设施故障。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .rules import SCHEMA_VERSION

ActionName = Literal["attack", "use_potion"]
CheckStatus = Literal["pass", "fail", "error"]
CaseStatus = Literal["pass", "fail", "error"]

# HTTP 层错误分类：执行错误的具体原因，报告据此区分基础设施故障与规则缺陷。
ErrorKind = Literal[
    "connection",  # 连不上、连接被重置
    "timeout",  # 请求超时，结果未知
    "invalid_response",  # 响应不是可解析的 JSON 对象
    "server_error",  # 5xx
    "persistence_error",  # 存储冲突等执行故障
    "unexpected_error",  # 客户端适配器自身的异常
]


class EvidenceModel(BaseModel):
    """保持观测原类型，所有嵌套模型拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)


# --------------------------------------------------------------------- 场景


class ActionStep(EvidenceModel):
    """一个计划动作及其预期结果。"""

    action: ActionName
    expected_status: int = 200
    expected_code: str | None = None


class ControlSpec(EvidenceModel):
    """对照运行：同 seed 的第二个会话，用来验证随机结果或状态是否被影响。"""

    description: str
    actions: list[ActionName]


class Scenario(EvidenceModel):
    """一个固定场景：case_id、说明、seed、顺序动作与各步预期。"""

    case_id: str
    description: str
    seed: int
    steps: list[ActionStep] = Field(default_factory=list)
    expected_final_status: str | None = None
    control: ControlSpec | None = None


# ------------------------------------------------------------- HTTP 线上格式


class EventView(EvidenceModel):
    """一条战斗事件的线上表示。"""

    turn: int
    actor: str
    kind: str
    value: int
    player_hp: int
    slime_hp: int
    potions: int | None = None


class CombatantView(EvidenceModel):
    hp: int
    max_hp: int


class PlayerView(CombatantView):
    potions: int


class SnapshotView(EvidenceModel):
    """会话快照的线上表示。"""

    session_id: str
    seed: int
    status: str
    turn: int
    player: PlayerView
    slime: CombatantView
    events: list[EventView]


def normalize_snapshot(snapshot: SnapshotView) -> dict[str, object]:
    """比较用规范化：去掉会话 id 这类每次运行都不同的字段。

    只排除明确的非确定字段（session_id）；其余业务字段一律参与比较，
    不允许通过忽略字段来「获得一致」。
    """
    return snapshot.model_dump(exclude={"session_id"})


# ----------------------------------------------------------------- HTTP 观测


class HttpObservation(EvidenceModel):
    """一次 HTTP 请求的观测结果：保留状态码、响应体与错误分类。"""

    method: str
    path: str
    status_code: int | None = None
    body: dict[str, object] | None = None
    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    elapsed_ms: float = 0.0

    @property
    def is_error(self) -> bool:
        """是否属于执行错误（基础设施故障），而不是可判定的规则观测。"""
        return self.error_kind is not None

    @property
    def error_code(self) -> str | None:
        """响应体里的稳定错误码；成功响应或不可解析响应为 None。"""
        if not isinstance(self.body, dict):
            return None
        code = self.body.get("code")
        return code if isinstance(code, str) else None

    def snapshot(self) -> SnapshotView | None:
        """按快照模型解析响应体；不适用时返回 None（由调用方归类）。"""
        if self.body is None or "session_id" not in self.body:
            return None
        return SnapshotView.model_validate(self.body)


# ----------------------------------------------------------------- 规则检查


class RuleCheck(EvidenceModel):
    """一条规则判定结果：稳定 rule_id + 期望 + 实际 + 步骤位置。"""

    rule_id: str
    status: CheckStatus
    expected: str
    actual: str
    message: str = ""
    step: int | None = None


class ExecutedAction(EvidenceModel):
    """已执行的一次动作尝试（含被拒绝的动作）。"""

    index: int
    action: ActionName
    status_code: int | None
    error_code: str | None = None
    elapsed_ms: float = 0.0


class StepReport(EvidenceModel):
    """一个步骤的完整证据：预期、观测、前后快照与新增事件。"""

    index: int
    action: ActionName
    expected_status: int
    expected_code: str | None
    observed_status: int | None
    observed_code: str | None
    status: CheckStatus
    before: SnapshotView
    after: SnapshotView | None = None
    new_events: list[EventView] = Field(default_factory=list)
    observation: HttpObservation
    verification_observation: HttpObservation | None = None
    error: str | None = None
    checks: list[RuleCheck] = Field(default_factory=list)


class ControlReport(EvidenceModel):
    """对照会话的执行结果。"""

    description: str
    actions: list[ActionName]
    status_codes: list[int | None]
    create_observation: HttpObservation | None = None
    observations: list[HttpObservation] = Field(default_factory=list)
    executed_actions: list[ExecutedAction] = Field(default_factory=list)
    completed: bool = False
    session_id: str | None = None
    final_snapshot: SnapshotView | None = None
    checks: list[RuleCheck] = Field(default_factory=list)


class CaseFailure(EvidenceModel):
    """首个失败/错误的定位信息。"""

    kind: CaseStatus
    rule_id: str | None = None
    step: int | None = None
    message: str = ""


class CaseReport(EvidenceModel):
    """一个场景的完整报告。

    内嵌 `scenario`（场景定义）是重跑的前提：重跑只依赖报告中的
    seed、动作序列与各步预期，不去猜原运行执行了什么。
    未知字段一律拒绝（extra="forbid"），避免「忽略字段后看起来一致」。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    description: str
    seed: int
    status: CaseStatus
    scenario: Scenario
    session_id: str | None = None
    create_observation: HttpObservation | None = None
    initial_get_observation: HttpObservation | None = None
    actions_completed: bool = False
    started_at: str
    finished_at: str
    duration_ms: float
    planned_actions: list[ActionName] = Field(default_factory=list)
    executed_actions: list[ExecutedAction] = Field(default_factory=list)
    initial_snapshot: SnapshotView | None = None
    final_snapshot: SnapshotView | None = None
    steps: list[StepReport] = Field(default_factory=list)
    checks: list[RuleCheck] = Field(default_factory=list)
    control: ControlReport | None = None
    failure: CaseFailure | None = None
    error: str | None = None


class RunSummary(EvidenceModel):
    passed: int = 0
    failed: int = 0
    errored: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errored


class RunEnvironment(EvidenceModel):
    """运行环境快照：不含环境变量、凭据或请求头。"""

    python_version: str
    platform: str
    gamepilot_version: str


class RunReport(EvidenceModel):
    """一次运行的完整证据。未知字段同样拒绝，重跑时不会静默忽略业务字段。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    rules_version: str
    rules_source: str
    mode: Literal["run", "replay"] = "run"
    run_id: str
    suite: str
    base_url: str
    started_at: str
    finished_at: str
    duration_ms: float
    timeout_seconds: float
    environment: RunEnvironment
    summary: RunSummary
    cases: list[CaseReport] = Field(default_factory=list)


# --------------------------------------------------------------- 重跑（replay）


class ReplayDifference(EvidenceModel):
    """重跑与记录值的一处差异。"""

    step: int | None = None
    field: str
    recorded: str
    replayed: str


class ReplayCaseReport(EvidenceModel):
    """一个场景的重跑结果。"""

    case_id: str
    outcome: Literal["match", "mismatch", "not_comparable"]
    note: str = ""
    session_id: str | None = None
    replayed_actions: list[ExecutedAction] = Field(default_factory=list)
    execution: CaseReport
    differences: list[ReplayDifference] = Field(default_factory=list)


class ReplaySummary(EvidenceModel):
    matched: int = 0
    mismatched: int = 0
    not_comparable: int = 0

    @property
    def total(self) -> int:
        return self.matched + self.mismatched + self.not_comparable


class ReplayReport(EvidenceModel):
    """重跑结果报告：独立文件，不覆盖原报告。"""

    schema_version: str = SCHEMA_VERSION
    rules_version: str
    mode: Literal["replay"] = "replay"
    run_id: str
    source_run_id: str
    source_report: str
    base_url: str
    started_at: str
    finished_at: str
    duration_ms: float
    timeout_seconds: float
    environment: RunEnvironment
    summary: ReplaySummary
    cases: list[ReplayCaseReport] = Field(default_factory=list)
