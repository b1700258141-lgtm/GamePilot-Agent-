"""Agent 报告的装配、退出码推导与排他写入。

退出码是对外契约，必须能从停止原因**唯一**推出，不能靠调用方各自解释：

- 0：覆盖目标已达到、所观察规则全部通过并正常收束 → `goal_met`；
- 1：独立规则证据确认游戏违规 → `rule_failure`；
- 2：模型 / 工具 / 输入 / 报告错误（优先于其他结论）→ `execution_error`、
  `model_error`、`model_format_error`、`input_error`；
- 3：正常请求但目标未完成（含预算耗尽与提前结束）→ `finish_requested`、
  `action_budget`、`model_call_budget`、`time_budget`。

"2 优先于其他结论" 在两处落地：停止原因本身是单值且先到先得；报告写入失败时
由 CLI 覆盖成 2——一份写不出去的报告不能被当成成功，也不能被当成发现了缺陷。

两份报告的写法刻意分开：`RunReport`（schema=1.1）是**游戏事实**，交给既有的
replay 重跑；`AgentRunReport` 是**任务结论**，记录模型决策、预算与停止原因。
后者只引用前者，不改写它的任何字段。
"""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from gamepilot.testing.models import CaseReport, RunReport
from gamepilot.testing.reporting import (
    ReportWriteError,
    current_environment,
    new_run_id,
    report_path,
    single_case_report,
    utc_now_iso,
    write_report,
)
from gamepilot.testing.rules import RULES_SOURCE, RULES_VERSION

from .models import (
    AGENT_SCHEMA_VERSION,
    AgentRunReport,
    AgentRunReportRef,
    AgentSummary,
    CostEstimate,
    GoalCoverage,
    ProviderInfo,
    TokenUsage,
)
from .prompts import PROMPT_VERSION

if TYPE_CHECKING:  # 只在类型检查时需要；运行期不导入 langgraph
    from .graph import AgentContext, AgentState

EXIT_OK = 0
EXIT_GAME_DEFECT = 1
EXIT_ERROR = 2
EXIT_INCOMPLETE = 3

# Agent 报告自己的目录，与脚本执行器的 artifacts/runs 分开，互不覆盖。
AGENT_OUTPUT_DIR = "artifacts/agent-runs"
AGENT_SUITE = "agent"

ERROR_STOP_REASONS = frozenset(
    {"execution_error", "model_error", "model_format_error", "input_error"}
)
DEFECT_STOP_REASONS = frozenset({"rule_failure"})
INCOMPLETE_STOP_REASONS = frozenset(
    {"finish_requested", "action_budget", "model_call_budget", "time_budget"}
)


def derive_exit_code(stop_reason: str) -> int:
    """停止原因 → 退出码。未登记的停止原因按错误处理，不静默放行。"""
    if stop_reason == "goal_met":
        return EXIT_OK
    if stop_reason in DEFECT_STOP_REASONS:
        return EXIT_GAME_DEFECT
    if stop_reason in ERROR_STOP_REASONS:
        return EXIT_ERROR
    if stop_reason in INCOMPLETE_STOP_REASONS:
        return EXIT_INCOMPLETE
    return EXIT_ERROR


def is_goal_met(coverage: GoalCoverage, rule_failures: list[str]) -> bool:
    """覆盖条件全部满足且没有任何规则失败——两条证据都来自程序而非模型。"""
    return coverage.met and not rule_failures


def estimate_cost(
    usage: TokenUsage,
    *,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    currency: str | None = None,
) -> CostEstimate:
    """按显式给出的单价估算费用。

    计价必须由调用方显式提供：这里**不内置任何价格表**，也不按「大概多少」
    猜一个数——报告是证据文件，凭空写出的金额比留空更糟。
    """
    unknown = CostEstimate(
        currency=currency,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        method="none",
        note="未提供计价或 Token 用量未知，不给出金额",
    )
    if input_price_per_million is None or output_price_per_million is None:
        return unknown
    if not usage.available:
        return unknown.model_copy(update={"method": "tokens-unknown"})
    assert usage.total_tokens is not None
    amount = (
        (usage.prompt_tokens or 0) * input_price_per_million
        + (usage.completion_tokens or 0) * output_price_per_million
    ) / 1_000_000
    return CostEstimate(
        currency=currency,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        amount=amount,
        method="tokens × 显式单价",
        note="按本次实际 Token 用量与显式提供的单价计算",
    )


def source_revision() -> tuple[str, bool | None]:
    """当前源码版本与是否有未提交改动。

    只读、只保留提交号与布尔值：不做网络访问，失败一律记 `unknown`，
    不让「拿不到版本」把一次已经跑完的运行打崩。
    """
    root = Path(__file__).resolve().parents[3]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return "unknown", None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        dirty = None if status.returncode != 0 else bool(status.stdout.strip())
        return head.stdout.strip(), dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", None


def agent_report_path(output_dir: str | Path, run_id: str) -> Path:
    """Agent 报告路径；文件名只由 run_id 决定，外部文本永远不进路径。"""
    base = report_path(output_dir, run_id)
    return base.with_name(f"{base.stem}-agent{base.suffix}")


def write_agent_report(report: AgentRunReport, output_dir: str | Path) -> Path:
    """排他写出 Agent 报告；已存在的文件不会被覆盖。"""
    path = agent_report_path(output_dir, report.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入 Agent 报告 {path}：{type(exc).__name__}") from exc
    return path


def provider_info(provider: object) -> ProviderInfo:
    """从供应商对象取出可写进报告的信息；密钥本身从不经过这里。"""
    revision, dirty = source_revision()
    return ProviderInfo(
        provider_id=provider.provider_id,  # type: ignore[attr-defined]
        model=provider.model,  # type: ignore[attr-defined]
        base_url=provider.base_url,  # type: ignore[attr-defined]
        is_test_double=provider.is_test_double,  # type: ignore[attr-defined]
        sampling=dict(provider.sampling),  # type: ignore[attr-defined]
        api_key_env=provider.api_key_env,  # type: ignore[attr-defined]
        source_revision=revision,
        source_dirty=dirty,
    )


def build_agent_report(
    *,
    state: AgentState,
    context: AgentContext,
    client: object,
    run_id: str,
    started_at: str,
    duration_ms: float,
    run_report: AgentRunReportRef | None,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    currency: str | None = None,
) -> AgentRunReport:
    """把一次 Agent 运行的状态与上下文装配成报告。

    `completed` 只在**正常收束**时为真；覆盖条件恰好满足但被预算打断的运行
    `goal_met=True` 而 `completed=False`——这两件事在报告里必须分得开。
    """
    coverage = state.get("coverage") or GoalCoverage(
        goal_id=state["goal_id"], goal=state["goal"], met=False, conditions=[]
    )
    rule_failures = sorted(set(context.rule_failures))
    stop_reason = state.get("stop_reason") or "execution_error"
    exit_code = derive_exit_code(stop_reason)
    goal_met = is_goal_met(coverage, rule_failures)
    usage = state.get("usage") or TokenUsage.unknown()
    return AgentRunReport(
        schema_version=AGENT_SCHEMA_VERSION,
        run_id=run_id,
        goal_id=state["goal_id"],
        goal=state["goal"],
        seed=state["seed"],
        base_url=client.base_url,  # type: ignore[attr-defined]
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=duration_ms,
        provider=provider_info(context.provider),
        budget=context.budget.spec,
        prompts_version=PROMPT_VERSION,
        rules_version=RULES_VERSION,
        rules_source=RULES_SOURCE,
        session_id=context.execution.session_id,
        run_report=run_report,
        summary=AgentSummary(
            stop_reason=stop_reason,  # type: ignore[arg-type]
            stop_detail=state.get("stop_detail", ""),
            goal_met=goal_met,
            completed=goal_met and exit_code == EXIT_OK,
            exit_code=exit_code,
            actions_attempted=context.budget.actions,
            model_calls=context.budget.model_calls,
            format_retries=context.budget.format_retries,
            rule_failures=rule_failures,
            duration_ms=duration_ms,
            usage=usage,
            cost=estimate_cost(
                usage,
                input_price_per_million=input_price_per_million,
                output_price_per_million=output_price_per_million,
                currency=currency,
            ),
        ),
        coverage=coverage,
        decisions=state.get("decisions", []),
        model_calls=context.model_call_records,
    )


def write_run_report(
    case: CaseReport, client: object, run_id: str, output_dir: str | Path
) -> tuple[RunReport, Path]:
    """把 Agent 跑出来的游戏事实写成 schema=1.1 的 `RunReport`。

    Agent 生成的场景由模型逐步决定，但产物与脚本执行器完全同构，
    因此既有的 `replay` 可以原样重跑它。
    """
    report = single_case_report(case, client, run_id, AGENT_SUITE)  # type: ignore[arg-type]
    return report, write_report(report, output_dir)


__all__ = [
    "AGENT_OUTPUT_DIR",
    "AGENT_SUITE",
    "EXIT_ERROR",
    "EXIT_GAME_DEFECT",
    "EXIT_INCOMPLETE",
    "EXIT_OK",
    "agent_report_path",
    "build_agent_report",
    "current_environment",
    "derive_exit_code",
    "estimate_cost",
    "is_goal_met",
    "new_run_id",
    "provider_info",
    "report_path",
    "source_revision",
    "write_agent_report",
    "write_run_report",
]
