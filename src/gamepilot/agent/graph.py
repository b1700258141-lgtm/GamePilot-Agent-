"""LangGraph 工作流：显式状态 + 条件边，不用隐藏内部循环的通用 Agent 封装。

节点与边的形状（与任务单第 4 节一致）：

```text
START → initialize → decide → validate → execute → check → route ─┬→ decide
                                                                  └→ finalize → END
```

每个节点的职责是单一的：

- `initialize`：创建会话、初始 GET 与初始校验（全部是确定性节点管理的）；
- `decide`：预算检查 → 组装输入 → 调用模型 → 记录用量与**实际发给模型的脱敏输入**；
- `validate`：校验工具选择（单个、已知、参数合法），非法请求不会产生游戏请求；
- `execute`：把动作追加到本次 `Scenario.steps` 后执行并做确定性校验；
- `check`：更新状态，按确定性覆盖条件求值；
- `route`：决定回到 `decide` 还是进入 `finalize`。

三条预算（动作、模型调用、时间）都在**调用之前**检查，计数器住在 `Budget` 对象里，
不属于工作流状态，因此重新进入某个节点不会把计数重置。LangGraph 的
`recursion_limit` 只作为兜底，不替代这些预算。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from gamepilot.testing.client import GameClient
from gamepilot.testing.execution import CaseExecution, CaseStop
from gamepilot.testing.models import ActionStep, CaseReport, RuleCheck, Scenario, SnapshotView
from gamepilot.testing.planning import derive_expectation

from . import prompts
from .budget import Budget, BudgetExceeded
from .goals import GoalSpec, evaluate_coverage
from .models import (
    ChatMessage,
    DecisionRecord,
    GoalCoverage,
    ModelCallRecord,
    TokenUsage,
    ToolCallRequest,
)
from .provider import ModelProvider
from .tools import TOOL_FINISH, TOOL_SCHEMAS, RejectedChoice, validate_choice


class AgentState(TypedDict, total=False):
    """工作流状态：只保留本轮任务的数据。"""

    goal_id: str
    goal: str
    seed: int
    session_id: str | None
    snapshot: SnapshotView | None
    actions: list[str]
    decisions: list[DecisionRecord]
    checks: list[RuleCheck]
    model_calls: int
    actions_attempted: int
    usage: TokenUsage
    stop_reason: str
    stop_detail: str
    coverage: GoalCoverage | None
    finish_summary: str | None
    repairing: bool
    # 一次回复里的**全部**工具调用，不是一个：只留第一个会把「多工具调用」
    # 静默截断成合法请求，而它本该被拒绝。
    pending: list[ToolCallRequest] | None
    accepted_action: str | None
    route_decision: str
    session_created: bool


@dataclass
class AgentContext:
    """节点之外的运行时依赖。

    这些（HTTP 客户端、增量执行器、预算、供应商）刻意不放进工作流状态：
    状态只描述「任务进展」，运行时对象由宿主持有，也不参与序列化。
    """

    client: GameClient
    execution: CaseExecution
    budget: Budget
    provider: ModelProvider
    goal: GoalSpec
    seed: int
    messages: list[ChatMessage] = field(default_factory=list)
    model_call_records: list[ModelCallRecord] = field(default_factory=list)
    rule_failures: list[str] = field(default_factory=list)
    case_report: CaseReport | None = None


def _stop(state: AgentState, reason: str, detail: str) -> AgentState:
    """收口到同一个停止原因上：先到的原因优先，不被后续节点覆盖。"""
    if state.get("stop_reason"):
        return {}
    return {"stop_reason": reason, "stop_detail": detail}


# --------------------------------------------------------------------- 节点


def _make_nodes(context: AgentContext) -> dict[str, Callable[[AgentState], Any]]:
    budget = context.budget
    spec = budget.spec

    async def initialize(state: AgentState) -> AgentState:
        try:
            snapshot = await context.execution.begin()
        except BudgetExceeded as exc:
            context.execution.interrupt(exc.detail)
            return {
                "session_created": context.execution.session_id is not None,
                "session_id": context.execution.session_id,
                **_stop(state, exc.reason, exc.detail),
            }
        except CaseStop as stop:
            return {
                "session_created": False,
                **_stop(state, "execution_error", stop.message),
            }
        context.messages.extend([prompts.rules_message(), prompts.protocol_message()])
        context.messages.append(prompts.initial_message(context.goal.goal, snapshot, spec))
        return {"session_created": True, "session_id": snapshot.session_id, "snapshot": snapshot}

    async def decide(state: AgentState) -> AgentState:
        repairing = bool(state.get("repairing"))
        try:
            budget.check_time()
            index = budget.start_model_call(repair=repairing)
        except BudgetExceeded as exc:
            return {**_stop(state, exc.reason, exc.detail)}
        timeout = budget.request_timeout(spec.model_timeout_seconds)

        # 记录**实际发出**的输入：报告里能事后核对模型看到了什么。
        sent = list(context.messages)
        started = time.perf_counter()
        reply = await context.provider.complete(
            sent,
            tools=TOOL_SCHEMAS,
            max_output_tokens=spec.max_output_tokens,
            timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if reply.failed:
            outcome = "provider_error"
        elif reply.tool_calls:
            outcome = "tool_call"
        else:
            outcome = "no_tool_call"
        context.model_call_records.append(
            ModelCallRecord(
                index=index,
                kind="format_repair" if repairing else "decision",
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                usage=reply.usage,
                messages=sent,
                error_kind=reply.error_kind,
                error_detail=reply.error_detail,
                tool_calls=list(reply.tool_calls),
            )
        )
        previous_usage = state.get("usage")
        usage = reply.usage if previous_usage is None else previous_usage.plus(reply.usage)
        if reply.failed:
            return {
                "model_calls": budget.model_calls,
                "usage": usage,
                "repairing": False,
                **_stop(
                    state,
                    "model_error",
                    f"模型调用失败：{reply.error_kind}（{reply.error_detail}）",
                ),
            }

        context.messages.append(
            ChatMessage(
                role="assistant", content=reply.text or "", tool_calls=list(reply.tool_calls)
            )
        )
        return {
            "model_calls": budget.model_calls,
            "usage": usage,
            "pending": list(reply.tool_calls) if reply.tool_calls else None,
            "repairing": False,
        }

    async def validate(state: AgentState) -> AgentState:
        pending = state.get("pending") or []
        # 整份列表一起校验：多工具调用、未知工具都在这里被拒。
        choice = validate_choice(pending)
        head = pending[0] if pending else None
        decisions = list(state.get("decisions", []))
        index = len(decisions) + 1
        if isinstance(choice, RejectedChoice):
            decisions.append(
                DecisionRecord(
                    index=index,
                    model_call_index=state.get("model_calls"),
                    tool=choice.tool,
                    accepted=False,
                    rejection=choice.reason,
                    detail=choice.detail,
                )
            )
            # 已经产生过 tool_use 时按协议回一条错误工具结果；否则补一条格式纠正提示。
            # 两条路径都只改本地对话：被拒绝的请求不会产生任何游戏请求。
            if head is not None:
                context.messages.append(
                    prompts.rejected_tool_results_message(pending, summary=choice.detail)
                )
            else:
                context.messages.append(prompts.format_repair_message(choice.detail))
            return {"decisions": decisions, "pending": None, "repairing": True}

        decisions.append(
            DecisionRecord(
                index=index,
                model_call_index=state.get("model_calls"),
                tool=choice.tool,
                arguments=dict(head.arguments) if head else {},
                action=choice.action,
                accepted=True,
                detail="已接受",
                summary=choice.summary,
            )
        )
        if choice.tool == TOOL_FINISH:
            if head is not None:
                context.messages.append(
                    prompts.tool_result_message(
                        head.tool_call_id,
                        action=TOOL_FINISH,
                        summary="结束申请已记录；任务是否完成由程序的确定性覆盖条件判定。",
                    )
                )
            return {
                "decisions": decisions,
                "pending": None,
                "finish_summary": choice.summary,
                "repairing": False,
                **_stop(state, "finish_requested", "模型主动申请结束（覆盖条件另行判定）"),
            }
        return {
            "decisions": decisions,
            "pending": None,
            "accepted_action": choice.action,
            "repairing": False,
        }

    async def execute(state: AgentState) -> AgentState:
        action = state.get("accepted_action")
        if action is None or state.get("stop_reason"):
            return {}
        try:
            budget.check_time()
            budget.start_action()
        except BudgetExceeded as exc:
            return {**_stop(state, exc.reason, exc.detail)}

        snapshot = state.get("snapshot")
        if snapshot is None:
            return {**_stop(state, "execution_error", "缺少已验证快照，无法确定动作预期")}
        # 预期由公开规则与已验证快照在**请求之前**确定，模型不能填写。
        plan = derive_expectation(snapshot, action)
        # 动态动作在发送之前追加到本次场景，最终按真实选择顺序保存在报告里。
        context.execution.scenario.steps.append(plan)
        try:
            report = await context.execution.execute(plan)
        except BudgetExceeded as exc:
            context.execution.interrupt(exc.detail, step=len(context.execution.steps) or None)
            _append_step_feedback(context, plan)
            return {
                "actions": [*state.get("actions", []), action],
                "actions_attempted": budget.actions,
                "checks": list(context.execution.checks),
                "snapshot": context.execution.current_snapshot or snapshot,
                **_stop(state, exc.reason, exc.detail),
            }
        except CaseStop as stop:
            context.rule_failures.extend(
                check.rule_id for check in context.execution.checks if check.status == "fail"
            )
            reason = "rule_failure" if stop.status == "fail" else "execution_error"
            _append_step_feedback(context, plan)
            return {
                "actions": [*state.get("actions", []), action],
                "actions_attempted": budget.actions,
                "checks": list(context.execution.checks),
                "snapshot": context.execution.current_snapshot or snapshot,
                **_stop(state, reason, stop.message),
            }
        _append_step_feedback(context, plan, report.index)
        return {
            "actions": [*state.get("actions", []), action],
            "actions_attempted": budget.actions,
            "checks": list(context.execution.checks),
            "snapshot": context.execution.current_snapshot or snapshot,
            "accepted_action": None,
        }

    async def check(state: AgentState) -> AgentState:
        """按已执行步骤求出确定性覆盖结论；不采信模型的任何自述。"""
        coverage = evaluate_coverage(context.goal.goal_id, context.execution.steps)
        updates: AgentState = {"coverage": coverage}
        if state.get("stop_reason") == "finish_requested":
            # 模型申请结束时覆盖条件已满足 → 正常完成；否则记「提前结束 = 未完成」。
            if coverage.met and not context.rule_failures and state.get("session_created"):
                updates["stop_reason"] = "goal_met"
                updates["stop_detail"] = "模型申请结束时覆盖条件已全部满足，所观察规则全部通过"
            else:
                missing = "、".join(coverage.unmet) or "无"
                updates["stop_detail"] = (
                    f"模型提前申请结束，覆盖条件尚未满足（未满足：{missing}）；未完成不等于通过"
                )
        return updates

    async def route(state: AgentState) -> AgentState:
        """收口判定：决定回到 `decide` 还是进入 `finalize`，并把决定写进状态。

        覆盖条件是**程序**算出来的完成信号——满足即正常收束，不依赖模型申请。
        预算在继续下一轮**之前**核对，超限后不会再发出任何模型或游戏请求：
        这也是「动作预算耗尽」被归到「目标未完成」的原因，而不是乐观地按前缀放行。
        """
        if state.get("stop_reason"):
            return {"route_decision": "finalize"}
        coverage = state.get("coverage")
        if coverage is not None and coverage.met and not context.rule_failures:
            return {
                "route_decision": "finalize",
                "stop_reason": "goal_met",
                "stop_detail": "覆盖条件已全部满足，所观察规则全部通过",
            }
        for exhausted, reason, detail in (
            (
                budget.model_calls >= spec.max_model_calls,
                "model_call_budget",
                f"模型调用预算 {spec.max_model_calls} 次已用尽",
            ),
            (
                budget.actions >= spec.max_action_attempts,
                "action_budget",
                f"动作尝试预算 {spec.max_action_attempts} 次已用尽",
            ),
        ):
            if exhausted:
                return {"route_decision": "finalize", "stop_reason": reason, "stop_detail": detail}
        return {"route_decision": "decide"}

    async def finalize(state: AgentState) -> AgentState:
        """收尾：把已经落下的证据封成可重跑的 `CaseReport`。

        这里不改变任何游戏事实：失败前缀的真实规则结果原样保留，
        任务级的完成状态由 `AgentSummary` 另行表达。
        """
        context.case_report = await context.execution.finalize()
        return {"accepted_action": None, "pending": None, "repairing": False}

    return {
        "initialize": initialize,
        "decide": decide,
        "validate": validate,
        "execute": execute,
        "check": check,
        "route": route,
        "finalize": finalize,
    }


def _append_step_feedback(
    context: AgentContext, plan: ActionStep, index: int | None = None
) -> None:
    """把这一步的观测与规则反馈按工具结果回传（保留 tool_call_id 关联）。"""
    pending = context.messages[-1] if context.messages else None
    tool_call_id = ""
    tool_name = plan.action
    if pending is not None and pending.tool_calls:
        call = pending.tool_calls[0]
        tool_call_id, tool_name = call.tool_call_id, call.name
    steps = context.execution.steps
    last = steps[-1] if steps else None
    checks = last.checks if last is not None else []
    text = prompts.observation_message(
        action=plan.action,
        snapshot=context.execution.current_snapshot,
        checks=checks,
        spec=context.budget.spec,
        actions_used=context.budget.actions,
        model_calls_used=context.budget.model_calls,
        note=(
            f"（第 {index} 步，预期 {plan.expected_status}"
            + (f" {plan.expected_code}" if plan.expected_code else "")
            + "）"
            if index is not None
            else ""
        ),
    )
    if tool_call_id:
        context.messages.append(
            prompts.tool_result_message(tool_call_id, action=tool_name, summary=text.content)
        )
    else:  # pragma: no cover - 正常路径上总有一个待回填的工具调用
        context.messages.append(text)


# ------------------------------------------------------------------- 图与运行


def _edge_after_initialize(state: AgentState) -> str:
    return "finalize" if state.get("stop_reason") else "decide"


def _edge_after_decide(state: AgentState) -> str:
    return "finalize" if state.get("stop_reason") else "validate"


def _edge_after_validate(state: AgentState) -> str:
    if state.get("accepted_action"):
        return "execute"
    # 申请结束要先过 `check`：覆盖条件是否满足由程序判定，
    # 「完成」与「提前结束（未完成）」的区别就在那一步落定。
    if state.get("stop_reason") == "finish_requested":
        return "check"
    if state.get("stop_reason"):
        return "finalize"
    return "decide"  # 格式纠正：重新请求模型


def _edge_after_route(state: AgentState) -> str:
    return "finalize" if state.get("stop_reason") else "decide"


def build_graph(context: AgentContext) -> Any:
    """按 AgentContext 组装并编译图。"""
    nodes = _make_nodes(context)
    graph = StateGraph(AgentState)
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "initialize")
    graph.add_conditional_edges(
        "initialize", _edge_after_initialize, {"decide": "decide", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "decide", _edge_after_decide, {"validate": "validate", "finalize": "finalize"}
    )
    graph.add_conditional_edges(
        "validate",
        _edge_after_validate,
        {"execute": "execute", "check": "check", "decide": "decide", "finalize": "finalize"},
    )
    graph.add_edge("execute", "check")
    graph.add_edge("check", "route")
    graph.add_conditional_edges(
        "route", _edge_after_route, {"decide": "decide", "finalize": "finalize"}
    )
    graph.add_edge("finalize", END)
    return graph.compile()


def recursion_limit_for(spec_budget: Any) -> int:
    """兜底步数上限：每个动作最多 5 个节点，另留格式纠正与收尾的余量。

    它只是防止图失控，不替代动作/模型/时间预算。
    """
    return 5 * (spec_budget.max_model_calls + spec_budget.max_action_attempts) + 20


def build_scenario(goal_id: str, seed: int, description: str) -> Scenario:
    """Agent 场景：动作由模型逐步决定，因此初始步骤为空。

    `expected_final_status` 刻意不设：目标是「验证某个行为」，
    不是「必须打到某个终态」，强加终态要求就等于伪造游戏事实。
    """
    return Scenario(case_id=f"agent-{goal_id}", description=description, seed=seed, steps=[])


async def run_agent(
    *,
    client: GameClient,
    provider: ModelProvider,
    budget: Budget,
    goal: GoalSpec,
    seed: int,
    description: str,
    timeout_provider: Callable[[], float] | None = None,
) -> tuple[AgentState, AgentContext, dict[str, Any]]:
    """跑完一条 Agent 闭环，返回最终状态、运行上下文与图执行元数据。"""
    scenario = build_scenario(goal.goal_id, seed, description)
    execution = CaseExecution(client, scenario, timeout_provider=timeout_provider)
    context = AgentContext(
        client=client,
        execution=execution,
        budget=budget,
        provider=provider,
        goal=goal,
        seed=seed,
    )
    graph = build_graph(context)
    initial: AgentState = {
        "goal_id": goal.goal_id,
        "goal": goal.goal,
        "seed": seed,
        "session_created": False,
        "decisions": [],
        "actions": [],
        "checks": [],
        "model_calls": 0,
        "actions_attempted": 0,
    }
    final = await graph.ainvoke(
        initial,
        config={"recursion_limit": recursion_limit_for(budget.spec)},
    )
    meta = {
        "messages": context.messages,
        "model_call_records": context.model_call_records,
        "rule_failures": context.rule_failures,
        "scenario": scenario,
        "execution": execution,
    }
    return final, context, meta


__all__ = [
    "AgentContext",
    "AgentState",
    "build_graph",
    "build_scenario",
    "recursion_limit_for",
    "run_agent",
]
