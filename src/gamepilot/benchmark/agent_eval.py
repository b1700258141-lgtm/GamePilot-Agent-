"""Agent 配对试验：4 profile × 3 目标的 12 格对照评测。

评测层是本仓库里**唯一**被允许同时组装 `agent` / `lab` / `benchmark` / `scenarios`
的地方——这正是「Agent 不得沿导入链读到答案」的边界所在：Agent 读不到靶场、
清单和脚本，而评测层读得到，并在模型上下文之外决定用哪个靶场。

每一格的流程刻意与脚本评测同构：

```text
Agent 运行 → 落盘 RunReport → 读回 → 同 profile 无模型重跑 → 落盘
           → 同 seed 同 profile 跑脚本对照 → 落盘
```

三条要点：

1. **两组同参**：相同的 seed、profile、新会话、动作上限与 oracle；
   对照组的动作数与耗时单独记录，不假称脚本也消耗了模型预算。
2. **检出要两条证据**：靶场 recorder 确认真实触发 + 独立判定器命中目标规则。
   只凭 profile 认定检出是不允许的。
3. **工程门槛与模型效果分开**：测试替身的成绩只证明工程链路，
   报告里显式标注，不能被当成 Agent 能力成绩。
"""

import re
import time
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from gamepilot.agent.budget import Budget
from gamepilot.agent.goals import GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY, resolve_goal
from gamepilot.agent.graph import run_agent
from gamepilot.agent.models import (
    AgentRunReportRef,
    BudgetSpec,
    ChatMessage,
    CostEstimate,
    TokenUsage,
    ToolCallRequest,
)
from gamepilot.agent.provider import FakeProvider, ModelProvider, ModelReply
from gamepilot.agent.report import build_agent_report, estimate_cost, write_agent_report
from gamepilot.agent.tools import TOOL_FINISH, TOOL_PERFORM_ACTION
from gamepilot.lab import PROFILE_CATALOG, PROFILES, TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import CaseReport, ReplayReport, RunReport
from gamepilot.testing.replay import ReplayInputError, load_report, replay_report
from gamepilot.testing.reporting import (
    ReportWriteError,
    current_environment,
    new_run_id,
    utc_now_iso,
    write_report,
)
from gamepilot.testing.rules import RULES_SOURCE, RULES_VERSION, SCHEMA_VERSION
from gamepilot.testing.runner import run_case, single_case_report
from gamepilot.testing.scenarios import BASELINE_SCENARIOS

from .agent_manifest import (
    AGENT_BENCHMARK_VERSION,
    AGENT_COMBINATIONS,
    AGENT_EVAL_BUDGET,
    AGENT_GOAL_IDS,
    AGENT_MANIFEST_SOURCE,
    AGENT_SEED,
    DESIGNATED_OPPORTUNITIES,
    AgentCombination,
)
from .agent_models import (
    AgentCellEvidence,
    AgentEvalReport,
    AgentEvalSummary,
    AgentMetricResult,
    AgentPricing,
    AgentUsageCell,
)

# 与脚本评测一致的进程内占位地址：这些报告不是真实网络的产物。
AGENT_EVAL_BASE_URL = "http://agent-benchmark.invalid"

EXIT_OK = 0
EXIT_DEVIATION = 1
EXIT_EXECUTION_ERROR = 2

CONTROL_SUITE = "agent-control"


# ------------------------------------------------------------- 离线测试替身


STATUS_PATTERN = re.compile(r"状态：(\S+)")
PLAYER_PATTERN = re.compile(r"玩家：生命 (\d+)/(\d+)，药水 (\d+)")
SLIME_PATTERN = re.compile(r"史莱姆：生命 (\d+)/(\d+)")


class Observation:
    """从**渲染后的观测文本**里读出的公开快照。"""

    __slots__ = ("status", "hp", "max_hp", "potions")

    def __init__(self, status: str, hp: int, max_hp: int, potions: int) -> None:
        self.status = status
        self.hp = hp
        self.max_hp = max_hp
        self.potions = potions


def read_observation(content: str) -> Observation | None:
    """从观测文本里读出公开快照，读不出来时返回 None。

    测试替身读的是**模型看到的同一段文字**，而不是内部对象：这样它既证明
    「渲染出来的观测足以支撑决策」，也保证替身的选择确实随观测变化。
    """
    status = STATUS_PATTERN.search(content)
    player = PLAYER_PATTERN.search(content)
    slime = SLIME_PATTERN.search(content)
    if status is None or player is None or slime is None:
        return None
    return Observation(
        status=status.group(1),
        hp=int(player.group(1)),
        max_hp=int(player.group(2)),
        potions=int(player.group(3)),
    )


def latest_observation(messages: Sequence[ChatMessage]) -> Observation | None:
    """取对话里**最新**一份可解析的观测。

    必须倒着找而不是只看最后一条普通用户消息：第一步之后，最新的观测在
    工具结果里，最后一条普通用户消息始终是那份初始观测。只看它就会一直拿
    初始状态做判断——替身看起来在随观测决策，实际是照念第一眼。
    """
    for message in reversed(messages):
        found = read_observation(message.content)
        if found is not None:
            return found
    return None


def observation_reacting_double(goal_id: str) -> ModelProvider:
    """离线测试替身：只按公开规则与**最新观测**决定下一步。

    它是工程自检工具，不是 Agent 能力基线——报告里会标明 `is_test_double`。
    策略完全由渲染出来的快照驱动：换一个 seed、换一个 profile 或只让血量
    变一点，下一步选择都会随之改变，而不是照念一份预生成的脚本。

    替身允许记住「这一步的试探已经做过」，但不预置动作序列：
    目标本身就要求走到边界（满血喝药、受伤后治疗、获胜后再操作），
    而这些边界只能由观测到的真实状态判断出来。
    """
    probed = False

    def respond(messages: Sequence[ChatMessage]) -> ModelReply:
        nonlocal probed
        step = sum(1 for item in messages if item.role == "assistant")
        observation = latest_observation(messages)
        if observation is None:
            # 观测读不出来就不猜：返回空输出，由本地校验按格式错误处理，
            # 不会产生任何游戏请求。
            return ModelReply(text="（读不到可用观测）")

        if goal_id == GOAL_VICTORY and observation.status == "won" and not probed:
            # 获胜后再试一次，验证「战斗结束后不允许操作」；只试探一次。
            probed = True
            return _action("attack", step)
        if observation.status != "active":
            return _finish(step, f"战斗状态为 {observation.status}，观测到此为止。")
        if goal_id == GOAL_FULL_HEALTH:
            if observation.hp >= observation.max_hp and not probed:
                probed = True
                return _action("use_potion", step)
            return _finish(step, "已观察到满血喝药的处理与状态一致性。")
        if goal_id == GOAL_HEALING:
            # 受伤即治疗：这是治疗量最可能越过上限的时刻，也就是要验证的边界。
            if observation.hp < observation.max_hp and observation.potions > 0:
                return _action("use_potion", step)
            return _action("attack", step)  # 先受伤，才能验证治疗边界
        if goal_id == GOAL_VICTORY:
            return _action("attack", step)
        return _finish(step, "没有与该目标匹配的策略。")

    return FakeProvider(respond, model=f"offline-double::{goal_id}")


def _action(name: str, step: int) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCallRequest(
                tool_call_id=f"double-{step}-{name}",
                name=TOOL_PERFORM_ACTION,
                arguments={"action": name},
            )
        ]
    )


def _finish(step: int, summary: str) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCallRequest(
                tool_call_id=f"double-{step}-finish",
                name=TOOL_FINISH,
                arguments={"summary": summary},
            )
        ]
    )


def offline_provider_factory(goal_id: str) -> ModelProvider:
    """离线配对试验的默认供应商工厂（测试替身）。"""
    return observation_reacting_double(goal_id)


# ----------------------------------------------------------------- 指标助手


def _failing_rules(case: CaseReport | None) -> list[str]:
    if case is None:
        return []
    return sorted({check.rule_id for check in case.checks if check.status == "fail"})


def _metric(
    metric_id: str,
    description: str,
    numerator: int,
    denominator: int,
    target: str,
    *,
    expected: str,
    details: Sequence[str] = (),
) -> AgentMetricResult:
    """`expected` 有三种，必须显式给出，不允许悄悄放行：

    - `"zero"`：分子必须为 0（误报、执行错误、重跑差异）；
    - `"all"`：分子必须等于分母（预定机会、缺陷覆盖）；
    - `"report"`：信息项，只如实列出，不参与门槛。
    """
    if denominator == 0:
        # 分母为 0 时不声称通过也不声称未通过；门槛指标的分母来自清单，正常不会为 0。
        status = "not_applicable"
    elif expected == "zero":
        status = "ok" if numerator == 0 else "deviation"
    elif expected == "all":
        status = "ok" if numerator == denominator else "deviation"
    elif expected == "report":
        status = "ok"
    else:  # pragma: no cover - 防止新增期望类型时静默放行
        raise ValueError(f"未知的指标期望类型：{expected}")
    return AgentMetricResult(
        metric_id=metric_id,
        description=description,
        numerator=numerator,
        denominator=denominator,
        target=target,
        status=status,
        required=expected != "report",
        details=list(details),
    )


def _ratio(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator}"


def _cell_error_stages(row: AgentCellEvidence) -> list[str]:
    """返回一格发生执行错误的阶段；一格可以同时命中多个阶段。"""
    stages: list[str] = []
    if (
        row.exit_code == EXIT_EXECUTION_ERROR
        or row.case_status == "error"
        or row.run_report_error is not None
        or row.agent_report_error is not None
    ):
        stages.append("agent")
    if row.replay_status == "error" or row.replay_error is not None:
        stages.append("replay")
    if (
        row.control_case_status == "error"
        or row.control_case_error is not None
        or row.control_report_error is not None
    ):
        stages.append("control")
    return stages


def _cell_error_detail(row: AgentCellEvidence) -> str:
    return next(
        (
            detail
            for detail in (
                row.case_error,
                row.replay_error,
                row.control_case_error,
                row.run_report_error,
                row.agent_report_error,
                row.control_report_error,
            )
            if detail
        ),
        row.stop_reason,
    )


def build_agent_metrics(cells: Sequence[AgentCellEvidence]) -> list[AgentMetricResult]:
    """按清单分母构造指标；未执行与执行错误都不缩小分母。"""
    designated = [row for row in cells if row.is_designated]
    detected = [row for row in designated if row.detected]
    distinct = sorted({row.fault_id for row in detected if row.fault_id})

    normal = [row for row in cells if row.fault_id is None]
    normal_fp = [row for row in normal if row.case_status == "fail"]

    variants = [row for row in cells if row.fault_id is not None]
    untriggered = [row for row in variants if not row.triggered]
    untriggered_fp = [row for row in untriggered if row.case_status == "fail"]

    beyond = [row for row in cells if row.beyond_designated]

    errors = [row for row in cells if _cell_error_stages(row)]
    met = [row for row in cells if row.goal_met]
    incomplete = [row for row in cells if not row.goal_met]
    matches = [row for row in cells if row.replay_outcome == "match"]
    mismatches = [row for row in cells if row.replay_outcome == "mismatch"]
    comparable = [row for row in cells if row.replay_outcome in ("match", "mismatch")]

    designated_defects = {fault_id for fault_id, _ in DESIGNATED_OPPORTUNITIES}
    return [
        _metric(
            "designated_opportunities",
            "预定的三个缺陷机会被真实触发并命中目标规则",
            len(detected),
            len(DESIGNATED_OPPORTUNITIES),
            f"{len(DESIGNATED_OPPORTUNITIES)}/{len(DESIGNATED_OPPORTUNITIES)}",
            expected="all",
            details=[
                f"{row.profile} × {row.goal_id}：{'检出' if row.detected else '未检出'}"
                + (
                    ""
                    if row.detected
                    else f"（触发={row.triggered}，命中规则={row.target_rules_hit or '无'}"
                    f"，停止原因={row.stop_reason}）"
                )
                for row in designated
            ],
        ),
        _metric(
            "distinct_defect_coverage",
            "去重后的缺陷覆盖（按缺陷编号去重，命中多条规则不重复计数）",
            len(distinct),
            len(designated_defects),
            f"{len(designated_defects)}/{len(designated_defects)}",
            expected="all",
            details=[f"已检出：{', '.join(distinct) or '无'}"],
        ),
        _metric(
            "normal_false_positive",
            "normal 上的误报（正确服务被判为违规）",
            len(normal_fp),
            len(normal),
            "0（越小越好）",
            expected="zero",
            details=[f"{row.profile} × {row.goal_id}：{row.case_status}" for row in normal_fp]
            or ["normal 列没有任何 fail 结论"],
        ),
        _metric(
            "variant_untriggered_false_positive",
            "变体未触发时的误报（缺陷没被走到却判为违规）",
            len(untriggered_fp),
            len(untriggered),
            "0（分母为 0 时标 N/A）",
            expected="zero",
            details=[f"{row.profile} × {row.goal_id}：{row.case_status}" for row in untriggered_fp]
            or [f"未触发的变体格共 {len(untriggered)} 个"],
        ),
        _metric(
            "beyond_designated",
            "清单外的额外检出（真实触发且有规则失败，信息项：不算异常，也不是误报）",
            len(beyond),
            len(cells),
            "如实列出",
            expected="report",
            details=[
                f"{row.profile} × {row.goal_id}：{', '.join(row.failing_rules)}" for row in beyond
            ]
            or ["没有清单外的检出"],
        ),
        _metric(
            "goal_coverage",
            "目标覆盖成立（信息项：如实列出，不作门槛）",
            len(met),
            len(cells),
            "如实列出",
            expected="report",
            details=[f"{row.profile} × {row.goal_id}：{row.stop_reason}" for row in incomplete],
        ),
        _metric(
            "goal_incomplete",
            "目标未完成（信息项：覆盖条件未满足，含提前结束与预算耗尽）",
            len(incomplete),
            len(cells),
            "如实列出",
            expected="report",
            details=[
                f"{row.profile} × {row.goal_id}：覆盖未满足、停止原因 {row.stop_reason}"
                for row in incomplete
            ],
        ),
        _metric(
            "execution_errors",
            "执行错误（连不上、超时、5xx、结构不符、报告写入失败）",
            len(errors),
            len(cells),
            "0/12",
            expected="zero",
            details=[
                f"{row.profile} × {row.goal_id}：阶段 {', '.join(_cell_error_stages(row))}；"
                f"{_cell_error_detail(row)}"
                for row in errors
            ]
            or ["没有执行错误"],
        ),
        _metric(
            "replay_mismatch",
            "同 profile 无模型重跑的差异数（不得出现 mismatch）",
            len(mismatches),
            len(cells),
            "0/12",
            expected="zero",
            details=[
                f"match {_ratio(len(matches), len(cells))}，"
                f"可比 {_ratio(len(comparable), len(cells))}，"
                f"mismatch {len(mismatches)}，"
                f"未执行/不可比 {len(cells) - len(matches) - len(mismatches)}"
            ],
        ),
    ]


def _status(metrics: Sequence[AgentMetricResult], cells: Sequence[AgentCellEvidence]) -> str:
    if any(_cell_error_stages(row) for row in cells):
        return "execution_error"
    if any(metric.required and metric.status == "deviation" for metric in metrics):
        return "deviation"
    return "ok"


def _exit_code(status: str) -> int:
    if status == "execution_error":
        return EXIT_EXECUTION_ERROR
    if status == "deviation":
        return EXIT_DEVIATION
    return EXIT_OK


def acceptance_note(provider: ModelProvider) -> str:
    if provider.is_test_double:
        return (
            "本次供应商是测试替身：以下数字只证明工程链路（图、预算、判定、重跑）正确，"
            "不是 Agent 能力成绩；真实模型验收待完成。"
        )
    return "本次使用真实模型调用，结论可作为首轮 12 格小样本冒烟结果。"


def summarize_spend(
    spend: Sequence[AgentUsageCell],
    *,
    cells_planned: int,
    pricing: AgentPricing | None,
) -> tuple[int, int, TokenUsage, CostEstimate]:
    """汇总整批用量；任一计划格未知时，不输出伪造的部分总量。"""
    known = sum(1 for item in spend if item.usage.available)
    unknown = max(cells_planned - known, 0)
    if len(spend) == cells_planned and unknown == 0:
        prompt = sum(item.usage.prompt_tokens or 0 for item in spend)
        completion = sum(item.usage.completion_tokens or 0 for item in spend)
        total = sum(item.usage.total_tokens or 0 for item in spend)
        usage = TokenUsage.of(prompt, completion, total)
    else:
        usage = TokenUsage.unknown()

    cost = estimate_cost(
        usage,
        input_price_per_million=(pricing.input_price_per_million if pricing else None),
        output_price_per_million=(pricing.output_price_per_million if pricing else None),
        currency=pricing.currency if pricing else None,
    )
    return known, unknown, usage, cost


# ------------------------------------------------------------------- 单格


async def _replay_cell(
    client: GameClient, report_path: Path, cell_dir: Path
) -> tuple[ReplayReport | None, Path | None, str | None]:
    """读回刚写出的报告再重跑：证据链必须能被读回来。"""
    try:
        reloaded = load_report(report_path)
    except ReplayInputError as exc:  # pragma: no cover - 只在写入或校验实现出错时走到
        return None, None, f"刚写出的报告无法重新读取：{exc}"
    replay = await replay_report(client, reloaded, source_report=str(report_path))
    try:
        replay_path = write_report(replay, cell_dir / "replay")
    except ReportWriteError as exc:
        return replay, None, str(exc)
    return replay, replay_path, None


async def _run_cell(
    combination: AgentCombination,
    *,
    provider: ModelProvider,
    client: GameClient,
    recorder: TriggerRecorder,
    root: Path,
    budget_spec: BudgetSpec,
    pricing: AgentPricing | None,
) -> AgentCellEvidence:
    cell_dir = root / "cells" / combination.profile / combination.goal_id
    goal = resolve_goal(combination.goal_id)
    budget = Budget(budget_spec)
    run_id = new_run_id()
    started_at = utc_now_iso()
    started = time.perf_counter()

    state, context, _meta = await run_agent(
        client=client,
        provider=provider,
        budget=budget,
        goal=goal,
        seed=AGENT_SEED,
        description=f"Agent 评测 {combination.goal_id}（{combination.profile}，seed={AGENT_SEED}）",
        timeout_provider=lambda: budget.request_timeout(budget_spec.http_timeout_seconds),
    )
    duration_ms = (time.perf_counter() - started) * 1000

    case = context.case_report
    run_report: RunReport | None = None
    run_report_path: Path | None = None
    run_report_error: str | None = None
    if case is not None and context.execution.session_id is not None:
        run_report = single_case_report(case, client, run_id, "agent")
        try:
            run_report_path = write_report(run_report, cell_dir / "run")
        except ReportWriteError as exc:
            run_report_error = str(exc)
            state = {
                **state,
                "stop_reason": "input_error",
                "stop_detail": run_report_error,
            }

    report = build_agent_report(
        state=state,
        context=context,
        client=client,
        run_id=run_id,
        started_at=started_at,
        duration_ms=duration_ms,
        run_report=(
            AgentRunReportRef(run_id=run_id, path=str(run_report_path))
            if run_report_path is not None
            else None
        ),
        input_price_per_million=(pricing.input_price_per_million if pricing else None),
        output_price_per_million=(pricing.output_price_per_million if pricing else None),
        currency=pricing.currency if pricing else None,
    )
    agent_report_path: Path | None = None
    agent_report_error: str | None = None
    try:
        agent_report_path = write_agent_report(report, cell_dir)
    except ReportWriteError as exc:
        agent_report_error = str(exc)
        state = {
            **state,
            "stop_reason": "input_error",
            "stop_detail": agent_report_error,
        }
        report = build_agent_report(
            state=state,
            context=context,
            client=client,
            run_id=run_id,
            started_at=started_at,
            duration_ms=duration_ms,
            run_report=(
                AgentRunReportRef(run_id=run_id, path=str(run_report_path))
                if run_report_path is not None
                else None
            ),
            input_price_per_million=(pricing.input_price_per_million if pricing else None),
            output_price_per_million=(pricing.output_price_per_million if pricing else None),
            currency=pricing.currency if pricing else None,
        )

    # 同 profile 无模型重跑：只重放实际动作，不再请求模型。
    replay: ReplayReport | None = None
    replay_path: Path | None = None
    replay_outcome = "not_executed"
    replay_error: str | None = None
    replay_status = None
    replay_differences = 0
    if run_report_path is not None:
        replay, replay_path, replay_error = await _replay_cell(client, run_report_path, cell_dir)
        if replay is not None and replay.cases:
            result = replay.cases[0]
            replay_outcome = result.outcome
            replay_differences = len(result.differences)
            replay_status = result.execution.status
            if result.execution.status == "error" and replay_error is None:
                replay_error = result.execution.error or "重放执行失败，未提供原因"

    # 对照组：原脚本场景，同 seed 同 profile 的新会话，不消耗模型预算。
    scenario = next(
        item for item in BASELINE_SCENARIOS if item.case_id == combination.script_case_id
    )
    control_case = await run_case(client, scenario)
    control_report = single_case_report(control_case, client, new_run_id(), CONTROL_SUITE)
    control_path: Path | None = None
    control_report_error: str | None = None
    try:
        control_path = write_report(control_report, cell_dir / "control")
    except ReportWriteError as exc:
        control_report_error = str(exc)

    trigger = recorder.get(case.session_id) if case is not None and case.session_id else None
    failing = _failing_rules(case)
    return AgentCellEvidence(
        profile=combination.profile,
        goal_id=combination.goal_id,
        goal=combination.goal,
        fault_id=combination.fault_id,
        is_designated=combination.is_designated,
        target_rules=list(combination.target_rules),
        run_id=run_id,
        agent_report_path=(
            str(agent_report_path.relative_to(root)) if agent_report_path is not None else ""
        ),
        agent_report_error=agent_report_error,
        run_report_path=(
            str(run_report_path.relative_to(root)) if run_report_path is not None else None
        ),
        run_report_error=run_report_error,
        stop_reason=report.summary.stop_reason,
        exit_code=report.summary.exit_code,
        goal_met=report.summary.goal_met,
        completed=report.summary.completed,
        case_status=case.status if case is not None else None,
        case_error=case.error if case is not None else None,
        accepted_actions=list(state.get("actions", [])),
        spend=AgentUsageCell(
            actions_attempted=report.summary.actions_attempted,
            model_calls=report.summary.model_calls,
            format_retries=report.summary.format_retries,
            duration_ms=duration_ms,
            usage=report.summary.usage,
            cost=report.summary.cost,
        ),
        failing_rules=failing,
        target_rules_hit=sorted(set(failing) & set(combination.target_rules)),
        target_rules_missed=sorted(set(combination.target_rules) - set(failing)),
        other_failing_rules=sorted(set(failing) - set(combination.target_rules)),
        triggered=trigger is not None,
        trigger_detail=trigger.detail if trigger else None,
        # 额外检出：真实触发且有规则确实失败，只是不在预定机会清单里。
        beyond_designated=(trigger is not None and bool(failing) and not combination.is_designated),
        replay_run_id=replay.run_id if replay else "",
        replay_path=str(replay_path.relative_to(root)) if replay_path is not None else "",
        replay_outcome=replay_outcome,  # type: ignore[arg-type]
        replay_status=replay_status,
        replay_error=replay_error,
        replay_differences=replay_differences,
        control_case_id=scenario.case_id,
        control_run_id=control_report.run_id,
        control_report_path=(
            str(control_path.relative_to(root)) if control_path is not None else ""
        ),
        control_case_status=control_case.status,
        control_case_error=control_case.error,
        control_report_error=control_report_error,
        control_action_count=len(control_case.executed_actions),
        control_duration_ms=control_case.duration_ms,
        control_failing_rules=_failing_rules(control_case),
    )


# ------------------------------------------------------------------ 入口


def _write_summary(report: AgentEvalReport, root: Path) -> Path:
    path = root / "agent-benchmark.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入 Agent 评测摘要 {path}：{type(exc).__name__}") from exc
    return path


async def run_agent_eval(
    output_dir: str | Path,
    *,
    provider_factory: Callable[[str], ModelProvider] | None = None,
    budget: BudgetSpec = AGENT_EVAL_BUDGET,
    pricing: AgentPricing | None = None,
) -> tuple[AgentEvalReport, Path]:
    """跑完 12 格配对试验，返回摘要与摘要文件路径。

    `provider_factory` 按目标编号给出供应商：默认是离线测试替身。
    真实模型由调用方注入，**评测层自己不会隐式发起付费调用**。
    """
    factory = provider_factory or offline_provider_factory
    started_at = utc_now_iso()
    started = time.perf_counter()
    run_id = new_run_id()
    root = Path(output_dir) / run_id

    cells: list[AgentCellEvidence] = []
    created: list[ModelProvider] = []
    async with AsyncExitStack() as stack:
        clients: dict[str, GameClient] = {}
        recorders: dict[str, TriggerRecorder] = {}
        for profile in PROFILES:
            recorder = TriggerRecorder()
            app = create_lab_app(profile, recorder=recorder)
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=AGENT_EVAL_BASE_URL
                )
            )
            clients[profile] = GameClient(
                http, base_url=AGENT_EVAL_BASE_URL, timeout=budget.http_timeout_seconds
            )
            recorders[profile] = recorder

        for combination in AGENT_COMBINATIONS:
            provider = factory(combination.goal_id)
            created.append(provider)
            cells.append(
                await _run_cell(
                    combination,
                    provider=provider,
                    client=clients[combination.profile],
                    recorder=recorders[combination.profile],
                    root=root,
                    budget_spec=budget,
                    pricing=pricing,
                )
            )

    metrics = build_agent_metrics(cells)
    status = _status(metrics, cells)
    # 12 格必须来自同一个供应商配置：混用会让「同一 profile 的输入一致」失去意义。
    probe = created[0]
    distinct_models = sorted({item.provider_id for item in created})
    detected = [row for row in cells if row.is_designated and row.detected]
    agent_errors = sum(1 for row in cells if "agent" in _cell_error_stages(row))
    replay_errors = sum(1 for row in cells if "replay" in _cell_error_stages(row))
    control_errors = sum(1 for row in cells if "control" in _cell_error_stages(row))
    usage_known, usage_unknown, total_usage, total_cost = summarize_spend(
        [row.spend for row in cells],
        cells_planned=len(AGENT_COMBINATIONS),
        pricing=pricing,
    )
    summary = AgentEvalSummary(
        cells_planned=len(AGENT_COMBINATIONS),
        cells_executed=len(cells),
        goals_met=sum(1 for row in cells if row.goal_met),
        goals_incomplete=sum(1 for row in cells if not row.goal_met),
        agent_execution_errors=agent_errors,
        replay_execution_errors=replay_errors,
        control_execution_errors=control_errors,
        execution_errors=sum(1 for row in cells if _cell_error_stages(row)),
        designated_opportunities=len(DESIGNATED_OPPORTUNITIES),
        designated_detected=len(detected),
        distinct_defects_detected=len({row.fault_id for row in detected if row.fault_id}),
        normal_false_positives=sum(
            1 for row in cells if row.fault_id is None and row.case_status == "fail"
        ),
        replay_matches=sum(1 for row in cells if row.replay_outcome == "match"),
        replay_comparable=sum(1 for row in cells if row.replay_outcome in ("match", "mismatch")),
        replay_mismatches=sum(1 for row in cells if row.replay_outcome == "mismatch"),
        replay_not_comparable=sum(1 for row in cells if row.replay_outcome == "not_comparable"),
        replay_not_executed=sum(1 for row in cells if row.replay_outcome == "not_executed"),
        provider_is_test_double=probe.is_test_double,
        metrics_passed=sum(1 for metric in metrics if metric.required and metric.passed),
        metrics_total=sum(1 for metric in metrics if metric.required),
        status=status,  # type: ignore[arg-type]
        exit_code=_exit_code(status),
        usage_known_cells=usage_known,
        usage_unknown_cells=usage_unknown,
        usage=total_usage,
        cost=total_cost,
        acceptance_note=acceptance_note(probe),
    )

    report = AgentEvalReport(
        agent_benchmark_version=AGENT_BENCHMARK_VERSION,
        manifest_source=AGENT_MANIFEST_SOURCE,
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=(time.perf_counter() - started) * 1000,
        seed=AGENT_SEED,
        base_url=AGENT_EVAL_BASE_URL,
        profiles=list(PROFILES),
        profile_map={profile: PROFILE_CATALOG[profile].summary for profile in PROFILES},
        goal_ids=list(AGENT_GOAL_IDS),
        goal_texts={goal_id: resolve_goal(goal_id).goal for goal_id in AGENT_GOAL_IDS},
        budget_seconds=budget.total_timeout_seconds,
        budget=budget.model_dump(),
        pricing=pricing,
        provider={
            "provider_id": probe.provider_id,
            "model": probe.model,
            "base_url": probe.base_url,
            "is_test_double": probe.is_test_double,
            "api_key_env": probe.api_key_env,
            "sampling": dict(probe.sampling),
            "providers_seen": distinct_models,
        },
        rules_version=RULES_VERSION,
        schema_version=SCHEMA_VERSION,
        rules_source=RULES_SOURCE,
        environment=current_environment(),
        summary=summary,
        cells=cells,
        metrics=metrics,
    )
    return report, _write_summary(report, root)


__all__ = [
    "AGENT_EVAL_BASE_URL",
    "EXIT_DEVIATION",
    "EXIT_EXECUTION_ERROR",
    "EXIT_OK",
    "Observation",
    "build_agent_metrics",
    "observation_reacting_double",
    "offline_provider_factory",
    "read_observation",
    "run_agent_eval",
    "summarize_spend",
]
