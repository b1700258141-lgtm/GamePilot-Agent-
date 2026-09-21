"""Agent 闭环：真实 ASGI 靶场 + 测试替身走完图，并逐个验证终止路径。

这些用例**不是**在测辅助函数：每一步都真的经过 `create_session`、
`perform_action`、核对 GET 与规则判定，只是把 HTTP 换成了进程内的 ASGI
（真实 localhost TCP 的版本见 `test_agent_cli.py`）。

三条贯穿全篇的检查：

1. **未授权请求为零**。每条拒绝路径上都核对动作请求计数没有增加——
   「本地拒绝」不是靠读代码相信的，而是靠请求计数证明的。
2. **输入确实更新**。模型后续输入里带着上一步的真实观测，且与报告里
   记录的步骤逐字段一致——不是靠预先录好的动作序列。
3. **前缀 pass 不等于任务通过**。`goal_met` / `completed` 与游戏前缀的规则
   结论分开表达：预算耗尽时前缀可能是 pass，任务仍是未完成。
"""

import re
from collections.abc import AsyncIterator, Callable, Sequence
from types import SimpleNamespace

import httpx
import pytest

# `langgraph` 属于可选的 agent extra（见 pyproject 与 README「游戏测试 Agent」）：
# 没装时整组测试明确跳过，而不是让收集阶段直接报导入错误。
pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from gamepilot.agent.budget import Budget
from gamepilot.agent.goals import GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY, resolve_goal
from gamepilot.agent.graph import run_agent
from gamepilot.agent.models import (
    AgentRunReport,
    BudgetSpec,
    ChatMessage,
    TokenUsage,
    ToolCallRequest,
)
from gamepilot.agent.provider import AnthropicCompatibleProvider, FakeProvider, ModelReply
from gamepilot.agent.report import (
    EXIT_ERROR,
    EXIT_GAME_DEFECT,
    EXIT_INCOMPLETE,
    EXIT_OK,
    build_agent_report,
    write_run_report,
)
from gamepilot.agent.tools import (
    REJECT_INVALID_ARGUMENTS,
    REJECT_MULTIPLE_TOOL_CALLS,
    REJECT_NO_TOOL_CALL,
    REJECT_UNKNOWN_TOOL,
    TOOL_FINISH,
    TOOL_PERFORM_ACTION,
)
from gamepilot.lab import TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.reporting import new_run_id, utc_now_iso
from gamepilot.testing.rules import R_NO_RETALIATE_ON_KILL, R_POTION_CAP, R_POTION_DECREMENTS

BASE_URL = "http://agent-graph.invalid"

STATUS_PATTERN = re.compile(r"状态：(\S+)")
PLAYER_PATTERN = re.compile(r"玩家：生命 (\d+)/(\d+)，药水 (\d+)")


# --------------------------------------------------------------- 观测与替身


def observations_in(messages: Sequence[ChatMessage]) -> list[tuple[str, int, int, int]]:
    """按出现顺序取出这段输入里所有可解析的观测：状态、玩家 HP、上限、药水。"""
    found: list[tuple[str, int, int, int]] = []
    for message in messages:
        status = STATUS_PATTERN.search(message.content)
        player = PLAYER_PATTERN.search(message.content)
        if status is not None and player is not None:
            found.append(
                (
                    status.group(1),
                    int(player.group(1)),
                    int(player.group(2)),
                    int(player.group(3)),
                )
            )
    return found


def latest_observation(messages: Sequence[ChatMessage]) -> tuple[str, int, int, int] | None:
    """取最新一份观测：第一步之后，最新的观测在工具结果里而不是初始消息里。"""
    found = observations_in(messages)
    return found[-1] if found else None


def count_actions(messages: Sequence[ChatMessage], action: str) -> int:
    return sum(
        1
        for message in messages
        for call in message.tool_calls
        if call.name == TOOL_PERFORM_ACTION and call.arguments.get("action") == action
    )


def action_call(action: str, step: int) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCallRequest(
                tool_call_id=f"call-{step}-{action}",
                name=TOOL_PERFORM_ACTION,
                arguments={"action": action},
            )
        ]
    )


def finish_call(step: int, summary: str = "结束") -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCallRequest(
                tool_call_id=f"call-{step}-finish", name=TOOL_FINISH, arguments={"summary": summary}
            )
        ]
    )


class Responder:
    """按**当前观测**决策的替身，并记下每一轮实际收到的输入。

    策略是一个纯函数：只有观测不同，选择才会不同。因此「选择随观测变化」
    不是靠预先录好的动作序列实现的。
    """

    def __init__(self, policy: Callable[[Sequence[ChatMessage], int], ModelReply]) -> None:
        self._policy = policy
        self.seen: list[list[ChatMessage]] = []

    def __call__(self, messages: Sequence[ChatMessage]) -> ModelReply:
        self.seen.append(list(messages))
        return self._policy(messages, len(self.seen))

    @property
    def calls(self) -> int:
        return len(self.seen)


def full_health_policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
    """满血喝一次药，之后申请结束（覆盖条件此时已经满足）。"""
    observed = latest_observation(messages)
    if observed is None:
        return ModelReply(text="")
    _, hp, max_hp, potions = observed
    if hp >= max_hp and potions > 0 and count_actions(messages, "use_potion") == 0:
        return action_call("use_potion", step)
    return finish_call(step, "已确认满血喝药被拒绝且状态未变。")


def healing_policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
    """满血时攻击，受伤后立刻治疗——选哪一个完全由观测到的血量决定。"""
    observed = latest_observation(messages)
    if observed is None:
        return ModelReply(text="")
    _, hp, max_hp, potions = observed
    if hp < max_hp and potions > 0:
        return action_call("use_potion", step)
    return action_call("attack", step)


def victory_policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
    """一直攻击；战斗结束后再试一次，观察拒绝与状态未变。"""
    observed = latest_observation(messages)
    if observed is None:
        return ModelReply(text="")
    status = observed[0]
    if status != "active" and count_actions(messages, "attack") >= 12:
        return finish_call(step, "已确认战斗结束后的操作限制。")
    return action_call("attack", step)


def always_attack(messages: Sequence[ChatMessage], step: int) -> ModelReply:  # noqa: ARG001
    return action_call("attack", step)


# ------------------------------------------------------------------ 请求计数


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录所有经手的请求；用来证明「被拒绝的请求没有发出去」。"""

    def __init__(self, app: FastAPI) -> None:
        self._inner = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.requests: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

    @property
    def action_requests(self) -> int:
        return sum(1 for _method, path in self.requests if path.endswith("/actions"))


class Server:
    """一个进程内服务 + 客户端 + 请求记录；负责关闭底层 HTTP 客户端。"""

    def __init__(self, app: FastAPI, *, timeout: float = 5.0) -> None:
        self.transport = RecordingTransport(app)
        self.http = httpx.AsyncClient(transport=self.transport, base_url=BASE_URL)
        self.client = GameClient(self.http, base_url=BASE_URL, timeout=timeout)

    @property
    def action_requests(self) -> int:
        return self.transport.action_requests

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def normal() -> AsyncIterator[Server]:
    """normal 靶场；每条用例拿到独立的应用实例与请求记录。"""
    server = Server(create_lab_app("normal"))
    try:
        yield server
    finally:
        await server.aclose()


# ------------------------------------------------------------------- 运行外壳


class Outcome:
    """一次闭环的全部产物：报告、工作流最终状态与运行时上下文。"""

    def __init__(self, report: AgentRunReport, state: dict, context: object) -> None:
        self.report = report
        self.state = state
        self.context = context


async def run_agent_once(
    client: GameClient,
    responder: Responder,
    goal_id: str,
    *,
    spec: BudgetSpec | None = None,
    seed: int = 42,
    budget: Budget | None = None,
    timeout_provider: Callable[[], float] | None = None,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
) -> Outcome:
    """跑一次闭环并装配出 Agent 报告。"""
    spec = spec or BudgetSpec()
    state, context, _meta = await run_agent(
        client=client,
        provider=FakeProvider(responder),
        budget=budget or Budget(spec),
        goal=resolve_goal(goal_id),
        seed=seed,
        description=f"集成测试 {goal_id}",
        timeout_provider=timeout_provider,
    )
    report = build_agent_report(
        state=state,
        context=context,
        client=client,
        run_id=new_run_id(),
        started_at=utc_now_iso(),
        duration_ms=0.0,
        run_report=None,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
    )
    return Outcome(report, state, context)


# ------------------------------------------------ 1. 观测驱动与输入更新


@pytest.mark.anyio
async def test_initial_input_carries_the_real_snapshot_and_the_action_is_executed(
    normal: Server,
) -> None:
    """第一轮输入里是**真实**初始观测，动作真的被执行并核对过。

    满血目标在一次成功观察后即满足：模型只调用了 1 次，游戏侧恰好 1 次动作请求。
    """
    server = normal
    responder = Responder(full_health_policy)
    outcome = await run_agent_once(server.client, responder, GOAL_FULL_HEALTH)
    report, context = outcome.report, outcome.context

    assert responder.calls == report.summary.model_calls == 1
    first = observations_in(responder.seen[0])
    assert first and first[-1][1:] == (100, 100, 2), "第一轮输入必须带上真实初始快照"

    assert report.summary.stop_reason == "goal_met"
    assert report.summary.exit_code == EXIT_OK
    assert report.summary.goal_met is True
    assert report.summary.completed is True
    assert report.coverage.met is True and report.coverage.unmet == []

    accepted = [record for record in report.decisions if record.accepted]
    assert [(record.tool, record.action) for record in accepted] == [
        (TOOL_PERFORM_ACTION, "use_potion")
    ]
    # 游戏侧证据：真的一次 409，且由 GET 证明状态未变。
    steps = context.execution.steps
    assert [(step.action, step.observed_status, step.observed_code) for step in steps] == [
        ("use_potion", 409, "player_full_hp")
    ]
    assert server.action_requests == 1


@pytest.mark.anyio
async def test_next_choice_follows_the_observed_hp_and_input_is_updated(
    normal: Server,
) -> None:
    """同一个决策函数在不同观测下给出不同选择：观察到的血量决定攻击还是治疗。

    如果后续输入里的观测是陈旧的，策略会一直攻击——所以这条用例同时守住了
    「输入必须更新」与「选择必须由观测驱动」。
    """
    server = normal
    responder = Responder(healing_policy)
    outcome = await run_agent_once(server.client, responder, GOAL_HEALING, seed=7)
    report, context = outcome.report, outcome.context

    assert report.summary.stop_reason == "goal_met"
    assert report.summary.exit_code == EXIT_OK
    accepted = [record.action for record in report.decisions if record.accepted]
    assert accepted == ["attack", "use_potion"], "满血先攻击，受伤后立刻治疗"

    # 每一轮输入里最新的观测，与上一步真实落下的快照逐字段一致。
    steps = context.execution.steps
    assert len(steps) == len(accepted) == responder.calls
    for index, seen in enumerate(responder.seen):
        observed = latest_observation(seen)
        assert observed is not None
        if index == 0:
            assert observed[1:] == (100, 100, 2)
        else:
            after = steps[index - 1].after
            assert after is not None
            assert observed == (
                after.status,
                after.player.hp,
                after.player.max_hp,
                after.player.potions,
            ), "后续输入里的观测必须来自上一步的真实快照"


@pytest.mark.anyio
async def test_victory_is_confirmed_by_observation_not_by_self_report(
    normal: Server,
) -> None:
    """获胜由观测确认：状态变为 won 后再试一次，得到 409 且状态未变。"""
    server = normal
    outcome = await run_agent_once(server.client, Responder(victory_policy), GOAL_VICTORY)
    report, context = outcome.report, outcome.context

    assert report.summary.stop_reason == "goal_met"
    assert report.summary.exit_code == EXIT_OK
    steps = context.execution.steps
    statuses = [step.after.status for step in steps if step.after is not None]
    assert statuses[-1] == "won"
    assert any(
        step.observed_status == 409 and step.observed_code == "battle_not_active" for step in steps
    ), "获胜之后必须观察到一次 battle_not_active 拒绝"
    assert report.coverage.unmet == []


# ------------------------------------------------ 2. 三个缺陷与 normal


@pytest.mark.parametrize(
    ("profile", "goal_id", "policy", "expected_rule"),
    [
        ("potion_overheal", GOAL_HEALING, healing_policy, R_POTION_CAP),
        ("potion_not_consumed", GOAL_HEALING, healing_policy, R_POTION_DECREMENTS),
        ("retaliate_after_death", GOAL_VICTORY, victory_policy, R_NO_RETALIATE_ON_KILL),
    ],
)
@pytest.mark.anyio
async def test_each_defect_is_detected_with_independent_rule_evidence(
    profile: str, goal_id: str, policy: Callable, expected_rule: str
) -> None:
    """三个缺陷都能被受控动作触发，并产出**独立判定器**的规则证据。"""
    recorder = TriggerRecorder()
    server = Server(create_lab_app(profile, recorder=recorder))
    try:
        outcome = await run_agent_once(server.client, Responder(policy), goal_id)
    finally:
        await server.aclose()

    report = outcome.report
    assert report.summary.stop_reason == "rule_failure"
    assert report.summary.exit_code == EXIT_GAME_DEFECT
    assert expected_rule in report.summary.rule_failures
    assert report.summary.goal_met is False, "两条证据必须分开：发现缺陷 ≠ 目标达成"
    assert report.summary.completed is False
    # 靶场在宿主侧确认这次会话真的走到了缺陷分支（判定器看不到这条记录）。
    trigger = recorder.get(outcome.context.execution.session_id)
    assert trigger is not None and trigger.fault_id == profile


@pytest.mark.parametrize(
    ("goal_id", "policy"),
    [
        (GOAL_FULL_HEALTH, full_health_policy),
        (GOAL_HEALING, healing_policy),
        (GOAL_VICTORY, victory_policy),
    ],
)
@pytest.mark.anyio
async def test_normal_profile_produces_no_rule_failure(
    goal_id: str, policy: Callable, normal: Server
) -> None:
    """normal 上三个目标都不产生任何规则失败——判定器没有误报。"""
    server = normal
    outcome = await run_agent_once(server.client, Responder(policy), goal_id)
    assert outcome.report.summary.rule_failures == [], f"{goal_id} 在 normal 上出现了规则失败"
    assert outcome.report.summary.exit_code != EXIT_GAME_DEFECT


# ------------------------------------------------ 3. 拒绝路径不产生游戏请求


def _reject_once(reply: ModelReply) -> Callable[[Sequence[ChatMessage], int], ModelReply]:
    """第一轮返回不合规输出，之后正常申请结束。"""

    def policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:  # noqa: ARG001
        return reply if step == 1 else finish_call(step, "纠正后结束")

    return policy


@pytest.mark.parametrize(
    ("reply", "expected_reason", "expected_outcome"),
    [
        (ModelReply(text="我只说一句话，不调用工具。"), REJECT_NO_TOOL_CALL, "no_tool_call"),
        (
            ModelReply(
                tool_calls=[
                    ToolCallRequest(
                        tool_call_id="a", name=TOOL_PERFORM_ACTION, arguments={"action": "attack"}
                    ),
                    ToolCallRequest(tool_call_id="b", name=TOOL_FINISH, arguments={"summary": "x"}),
                ]
            ),
            REJECT_MULTIPLE_TOOL_CALLS,
            "tool_call",
        ),
        (
            ModelReply(
                tool_calls=[ToolCallRequest(tool_call_id="a", name="admin_tool", arguments={})]
            ),
            REJECT_UNKNOWN_TOOL,
            "tool_call",
        ),
        (
            ModelReply(
                tool_calls=[
                    ToolCallRequest(
                        tool_call_id="a", name=TOOL_PERFORM_ACTION, arguments={"action": "teleport"}
                    )
                ]
            ),
            REJECT_INVALID_ARGUMENTS,
            "tool_call",
        ),
    ],
)
@pytest.mark.anyio
async def test_rejected_output_produces_no_game_request(
    reply: ModelReply,
    expected_reason: str,
    expected_outcome: str,
    normal: Server,
) -> None:
    """未知工具、多工具调用、非法动作、空输出都不产生未授权的游戏请求。"""
    server = normal
    responder = Responder(_reject_once(reply))
    outcome = await run_agent_once(server.client, responder, GOAL_FULL_HEALTH)

    # 被拒绝的那一刻：只有创建会话与初始 GET，没有任何动作请求。
    assert server.action_requests == 0
    rejected = [record for record in outcome.state.get("decisions", []) if not record.accepted]
    assert [record.rejection for record in rejected] == [expected_reason]
    assert outcome.context.model_call_records[0].outcome == expected_outcome
    # 被拒绝的请求既不算「发现了缺陷」，也不算任务完成。
    assert outcome.report.summary.exit_code == EXIT_INCOMPLETE
    assert outcome.report.summary.rule_failures == []
    assert outcome.report.summary.goal_met is False


@pytest.mark.anyio
async def test_anthropic_retry_returns_results_for_every_rejected_tool_use(
    normal: Server,
) -> None:
    """多 tool_use 被拒绝后，真实适配器的下一轮请求必须闭合全部 ID。"""

    class Messages:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        async def create(self, **request: object) -> object:
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            id="multi-a",
                            name=TOOL_PERFORM_ACTION,
                            input={"action": "attack"},
                        ),
                        SimpleNamespace(
                            type="tool_use",
                            id="multi-b",
                            name=TOOL_FINISH,
                            input={"summary": "不应被接受"},
                        ),
                    ],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                    stop_reason="tool_use",
                )
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id="finish",
                        name=TOOL_FINISH,
                        input={"summary": "纠正后结束"},
                    )
                ],
                usage=SimpleNamespace(input_tokens=8, output_tokens=3),
                stop_reason="tool_use",
            )

    messages = Messages()
    provider = AnthropicCompatibleProvider(
        api_key="test-only",
        client=SimpleNamespace(messages=messages),
    )
    state, context, _meta = await run_agent(
        client=normal.client,
        provider=provider,
        budget=Budget(BudgetSpec()),
        goal=resolve_goal(GOAL_FULL_HEALTH),
        seed=42,
        description="多工具协议测试",
    )

    assert state["stop_reason"] == "finish_requested"
    assert normal.action_requests == 0
    second_conversation = messages.requests[1]["messages"]
    assert isinstance(second_conversation, list)
    for request in messages.requests:
        assert request["tool_choice"] == {
            "type": "any",
            "disable_parallel_tool_use": True,
        }
    result_message = second_conversation[-1]
    assert result_message["role"] == "user"
    blocks = result_message["content"]
    assert [block["tool_use_id"] for block in blocks] == ["multi-a", "multi-b"]
    assert all(block["type"] == "tool_result" and block["is_error"] for block in blocks)
    rejected_results = [item for item in context.messages if item.tool_results]
    assert len(rejected_results) == 1
    assert [item.tool_call_id for item in rejected_results[0].tool_results] == [
        "multi-a",
        "multi-b",
    ]
    assert [record.provider_stop_reason for record in context.model_call_records] == [
        "tool_use",
        "tool_use",
    ]
    assert all(record.response_text is None for record in context.model_call_records)


@pytest.mark.anyio
async def test_model_call_record_preserves_text_and_provider_stop_reason(
    normal: Server,
) -> None:
    """纯文本或截断响应必须留下诊断证据，不能只压成 no_tool_call。"""
    responder = Responder(
        lambda messages, step: ModelReply(  # noqa: ARG005
            text="正在分析，尚未形成工具调用",
            stop_reason="max_tokens",
        )
    )
    outcome = await run_agent_once(
        normal.client,
        responder,
        GOAL_FULL_HEALTH,
        spec=BudgetSpec(max_format_retries=0),
    )

    record = outcome.context.model_call_records[0]
    assert record.outcome == "no_tool_call"
    assert record.response_text == "正在分析，尚未形成工具调用"
    assert record.provider_stop_reason == "max_tokens"
    assert outcome.report.model_calls[0] == record
    assert outcome.report.summary.stop_reason == "model_format_error"


@pytest.mark.anyio
async def test_unknown_tool_repeated_never_reaches_the_game(
    normal: Server,
) -> None:
    """模型一直调用未知工具：纠正次数耗尽后报错，且游戏侧一次动作请求都没有。"""
    server = normal
    responder = Responder(
        lambda messages, step: ModelReply(  # noqa: ARG005
            tool_calls=[
                ToolCallRequest(tool_call_id="x", name="admin_tool", arguments={"action": "attack"})
            ]
        )
    )
    outcome = await run_agent_once(server.client, responder, GOAL_FULL_HEALTH)

    assert server.action_requests == 0
    assert outcome.report.summary.stop_reason == "model_format_error"
    assert outcome.report.summary.exit_code == EXIT_ERROR


# ------------------------------------------------ 4. 预算与终止路径


class JumpingClock:
    """可控时钟：只有显式推进才走时间。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.anyio
async def test_time_budget_trips_on_a_controlled_clock_without_sleeping(
    normal: Server,
) -> None:
    """总时限耗尽由可控时钟验证：不 sleep，也不真的等 120 秒。

    时钟在第二轮决策时推过总时限；此时第一步的真实证据已经落下。
    """
    server = normal
    clock = JumpingClock()
    spec = BudgetSpec(total_timeout_seconds=120.0)

    def policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:  # noqa: ARG001
        if step == 2:
            clock.advance(1000.0)  # 第一步跑完之后把时间推过期
        return action_call("attack", step)

    outcome = await run_agent_once(
        server.client,
        Responder(policy),
        GOAL_HEALING,
        spec=spec,
        budget=Budget(spec, clock=clock),
    )
    report = outcome.report

    assert report.summary.stop_reason == "time_budget"
    assert report.summary.exit_code == EXIT_INCOMPLETE
    assert report.summary.goal_met is False
    # 到期后不再发出任何游戏请求：计数停在超时那一刻。
    assert report.summary.actions_attempted == 1
    assert server.action_requests == 1
    assert outcome.context.execution.steps, "超时前必须留下部分真实证据"
    assert report.budget.total_timeout_seconds == 120.0


@pytest.mark.anyio
async def test_time_budget_is_rechecked_between_create_and_initial_get() -> None:
    """创建会话耗尽总时限后，不得再发初始 GET。"""
    clock = JumpingClock()
    app = create_lab_app("normal")

    class AdvanceAfterCreate(RecordingTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await super().handle_async_request(request)
            if request.method == "POST" and request.url.path == "/api/v1/game-sessions":
                clock.advance(2.0)
            return response

    transport = AdvanceAfterCreate(app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = GameClient(http, base_url=BASE_URL, timeout=5.0)
        spec = BudgetSpec(total_timeout_seconds=1.0)
        budget = Budget(spec, clock=clock)
        outcome = await run_agent_once(
            client,
            Responder(full_health_policy),
            GOAL_FULL_HEALTH,
            spec=spec,
            budget=budget,
            timeout_provider=lambda: budget.request_timeout(spec.http_timeout_seconds),
        )

    assert outcome.report.summary.stop_reason == "time_budget"
    assert outcome.report.summary.exit_code == EXIT_INCOMPLETE
    assert transport.requests == [("POST", "/api/v1/game-sessions")]
    assert outcome.context.execution.session_id is not None
    assert outcome.context.execution.stop is not None


@pytest.mark.anyio
async def test_time_budget_is_rechecked_before_rejected_action_verification_get() -> None:
    """409 动作耗尽总时限后，不得再发用于证明状态未变的 GET。"""
    clock = JumpingClock()
    snapshot = {
        "session_id": "budget-verify",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 100, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }
    app = FastAPI()

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return {**snapshot, "session_id": session_id}

    @app.post("/api/v1/game-sessions/{session_id}/actions")
    async def act(session_id: str, payload: dict):  # noqa: ARG001
        return JSONResponse({"code": "player_full_hp"}, status_code=409)

    class AdvanceAfterAction(RecordingTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await super().handle_async_request(request)
            if request.url.path.endswith("/actions"):
                clock.advance(2.0)
            return response

    transport = AdvanceAfterAction(app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = GameClient(http, base_url=BASE_URL, timeout=5.0)
        spec = BudgetSpec(total_timeout_seconds=1.0)
        budget = Budget(spec, clock=clock)
        outcome = await run_agent_once(
            client,
            Responder(full_health_policy),
            GOAL_FULL_HEALTH,
            spec=spec,
            budget=budget,
            timeout_provider=lambda: budget.request_timeout(spec.http_timeout_seconds),
        )

    assert outcome.report.summary.stop_reason == "time_budget"
    assert [method for method, _path in transport.requests] == ["POST", "GET", "POST"]
    assert sum(1 for method, _path in transport.requests if method == "GET") == 1
    step = outcome.context.execution.steps[0]
    assert step.observed_status == 409
    assert step.verification_observation is None
    assert step.error and "总时限" in step.error


@pytest.mark.anyio
async def test_known_token_usage_is_summed_into_the_agent_report(normal: Server) -> None:
    """供应商给出已知用量时，首轮不能被初始 unknown 污染，后续调用应正常相加。"""

    def policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
        choice = healing_policy(messages, step)
        return ModelReply(
            tool_calls=choice.tool_calls,
            text=choice.text,
            usage=TokenUsage.of(prompt=100 * step, completion=10 * step),
        )

    responder = Responder(policy)
    outcome = await run_agent_once(
        normal.client,
        responder,
        GOAL_HEALING,
        input_price_per_million=2.0,
        output_price_per_million=4.0,
    )
    usage = outcome.report.summary.usage

    assert responder.calls == 2
    assert usage.available is True
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (300, 30, 330)
    assert outcome.report.summary.cost.amount == pytest.approx(0.00072)
    assert [record.usage.available for record in outcome.context.model_call_records] == [
        True,
        True,
    ]


@pytest.mark.anyio
async def test_one_unknown_model_call_downgrades_the_report_total(normal: Server) -> None:
    """只有实际发生的调用里混入 unknown 时，总用量才降级为 unknown。"""

    def policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
        choice = healing_policy(messages, step)
        usage = TokenUsage.of(100, 10) if step == 1 else TokenUsage.unknown()
        return ModelReply(tool_calls=choice.tool_calls, text=choice.text, usage=usage)

    outcome = await run_agent_once(
        normal.client,
        Responder(policy),
        GOAL_HEALING,
        input_price_per_million=2.0,
        output_price_per_million=4.0,
    )

    assert outcome.report.summary.usage == TokenUsage.unknown()
    assert outcome.report.summary.cost.amount is None
    assert outcome.report.summary.cost.method == "tokens-unknown"


@pytest.mark.anyio
async def test_failed_model_reply_keeps_reliable_usage(normal: Server) -> None:
    """供应商失败响应若带可靠 usage，报告仍计入该次已发生调用。"""
    responder = Responder(
        lambda _messages, _step: ModelReply(
            error_kind="server_error",
            error_detail="测试注入",
            usage=TokenUsage.of(70, 7),
        )
    )
    outcome = await run_agent_once(normal.client, responder, GOAL_FULL_HEALTH)

    assert outcome.report.summary.stop_reason == "model_error"
    assert outcome.report.summary.usage == TokenUsage.of(70, 7)


@pytest.mark.anyio
async def test_format_retry_exhaustion_is_a_model_format_error(
    normal: Server,
) -> None:
    """模型连续返回空输出：纠正次数耗尽后以退出码 2 结束，且没有游戏请求。"""
    server = normal
    spec = BudgetSpec(max_format_retries=2, max_model_calls=12)
    responder = Responder(lambda messages, step: ModelReply(text="我拒绝输出工具调用。"))  # noqa: ARG005
    outcome = await run_agent_once(
        server.client, responder, GOAL_FULL_HEALTH, spec=spec, budget=Budget(spec)
    )

    assert outcome.report.summary.stop_reason == "model_format_error"
    assert outcome.report.summary.exit_code == EXIT_ERROR
    assert outcome.report.summary.format_retries == spec.max_format_retries
    # 计数没有被节点重入重置：每一轮都真的占用了一次模型额度。
    assert (
        outcome.report.summary.model_calls
        == outcome.context.budget.model_calls
        == responder.calls
        == 3
    )
    assert server.action_requests == 0
    assert outcome.report.summary.rule_failures == []


@pytest.mark.anyio
async def test_finish_too_early_is_incomplete_even_though_the_prefix_passed(
    normal: Server,
) -> None:
    """提前 finish：游戏前缀全部通过，任务仍未完成。

    这是「不允许用前缀 pass 冒充任务通过」的直接证据：RunReport 里那条用例的
    结论是 pass，而 Agent 报告里 goal_met=False、completed=False、退出码 3。
    """
    server = normal
    outcome = await run_agent_once(
        server.client, Responder(lambda m, s: finish_call(s)), GOAL_VICTORY
    )
    case = await outcome.context.execution.finalize()
    report = outcome.report

    assert case.status == "pass", "零动作的前缀本身没有任何违规"
    assert case.failure is None and case.error is None
    assert report.summary.stop_reason == "finish_requested"
    assert report.summary.exit_code == EXIT_INCOMPLETE
    assert report.summary.goal_met is False
    assert report.summary.completed is False
    assert report.coverage.unmet, "覆盖条件必须逐条说明还没满足什么"


@pytest.mark.anyio
async def test_action_budget_stops_a_repeated_loop(
    normal: Server,
) -> None:
    """循环攻击：动作预算耗尽后明确终止，不再发出请求。"""
    server = normal
    spec = BudgetSpec(max_action_attempts=5, max_model_calls=12)
    outcome = await run_agent_once(
        server.client,
        Responder(always_attack),
        GOAL_HEALING,
        spec=spec,
        budget=Budget(spec),
    )
    report = outcome.report

    assert report.summary.stop_reason == "action_budget"
    assert report.summary.exit_code == EXIT_INCOMPLETE
    assert report.summary.actions_attempted == spec.max_action_attempts
    assert server.action_requests == spec.max_action_attempts, "超限后不得再发动作请求"
    assert report.summary.model_calls <= spec.max_model_calls


@pytest.mark.anyio
async def test_model_call_budget_stops_the_loop(
    normal: Server,
) -> None:
    """模型调用预算耗尽：同样明确终止，且不再调用模型。"""
    server = normal
    spec = BudgetSpec(max_model_calls=2, max_action_attempts=10)
    responder = Responder(always_attack)
    outcome = await run_agent_once(
        server.client, responder, GOAL_HEALING, spec=spec, budget=Budget(spec)
    )
    report = outcome.report

    assert report.summary.model_calls == spec.max_model_calls == responder.calls
    assert report.summary.stop_reason == "model_call_budget"
    assert report.summary.exit_code == EXIT_INCOMPLETE
    assert report.summary.goal_met is False


# ------------------------------------------------ 5. 模型侧与游戏侧错误分类


@pytest.mark.parametrize(
    "error_kind", ["timeout", "rate_limit", "server_error", "refusal", "connection"]
)
@pytest.mark.anyio
async def test_model_failures_are_classified_and_exit_2(error_kind: str, normal: Server) -> None:
    """模型超时、429/5xx、拒绝响应都归类为模型错误，退出码 2，且不发游戏请求。"""
    server = normal

    def policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:  # noqa: ARG001
        return ModelReply(error_kind=error_kind, error_detail="构造的失败")

    outcome = await run_agent_once(server.client, Responder(policy), GOAL_FULL_HEALTH)
    report = outcome.report

    assert report.summary.stop_reason == "model_error"
    assert report.summary.exit_code == EXIT_ERROR
    assert server.action_requests == 0, "模型失败后不得再发游戏请求"
    # 错误分类被保留，且不把「调用失败」记成「模型选择了不动作」。
    assert outcome.context.model_call_records[0].error_kind == error_kind
    assert outcome.context.model_call_records[0].outcome == "provider_error"
    assert report.summary.exit_code != EXIT_GAME_DEFECT, "没跑出结论就不能声称发现缺陷"


def _broken_app(mode: str) -> FastAPI:
    """构造一个「能建会话、但动作必然失败」的服务，用于验证错误归类。"""
    snapshot = {
        "session_id": "broken",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 100, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }
    app = FastAPI()

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return {**snapshot, "session_id": session_id}

    @app.post("/api/v1/game-sessions/{session_id}/actions")
    async def act(session_id: str, payload: dict) -> dict:  # noqa: ARG001
        if mode == "server_error":
            return JSONResponse({"error": "boom"}, status_code=503)
        return PlainTextResponse("这不是 JSON 对象")

    return app


# 单次超时**不在这里测**：httpx 的 ASGITransport 会忽略 timeout 参数，
# 进程内的「慢响应」根本不会触发超时。真实超时由 test_agent_cli.py
# 在真实 localhost TCP 上验证。
@pytest.mark.parametrize("mode", ["server_error", "invalid_body"])
@pytest.mark.anyio
async def test_game_side_failures_are_execution_errors_with_partial_evidence(
    mode: str, tmp_path
) -> None:
    """游戏侧 5xx / 结构不符都归为执行错误：退出码 2，且部分证据落盘。"""
    server = Server(_broken_app(mode), timeout=0.05)
    try:
        outcome = await run_agent_once(server.client, Responder(always_attack), GOAL_FULL_HEALTH)
        case = await outcome.context.execution.finalize()
        run_report, path = write_run_report(case, server.client, outcome.report.run_id, tmp_path)
    finally:
        await server.aclose()
    report = outcome.report

    assert report.summary.stop_reason == "execution_error"
    assert report.summary.exit_code == EXIT_ERROR
    assert outcome.context.rule_failures == [], "基础设施故障不能被记成规则缺陷"
    assert report.summary.rule_failures == []
    # 部分证据仍然落盘：失败的那次动作与错误原因都留在了报告里。
    assert path.exists()
    assert run_report.cases[0].status == "error", f"{mode} 的前缀不该被判成 pass 或 fail"
    assert run_report.cases[0].error
    assert run_report.cases[0].executed_actions


@pytest.mark.anyio
async def test_409_followed_by_a_failing_verification_get_is_an_execution_error() -> None:
    """预期 409 之后的核对 GET 失败：按执行错误处理，不推断「状态未变」。"""
    snapshot = {
        "session_id": "verify",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 100, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }
    app = FastAPI()
    seen = {"gets": 0}

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str):
        seen["gets"] += 1
        if seen["gets"] >= 2:  # 第一次是初始 GET，之后是核对 GET
            return JSONResponse({"error": "boom"}, status_code=503)
        return {**snapshot, "session_id": session_id}

    @app.post("/api/v1/game-sessions/{session_id}/actions")
    async def act(session_id: str, payload: dict):  # noqa: ARG001
        return JSONResponse({"code": "player_full_hp"}, status_code=409)

    server = Server(app)
    try:
        outcome = await run_agent_once(
            server.client, Responder(full_health_policy), GOAL_FULL_HEALTH
        )
    finally:
        await server.aclose()

    assert outcome.report.summary.stop_reason == "execution_error"
    assert outcome.report.summary.exit_code == EXIT_ERROR
    assert outcome.report.summary.exit_code != EXIT_OK, "核对失败时不得推断状态未变"
    steps = outcome.context.execution.steps
    assert [step.observed_status for step in steps] == [409]
    assert steps[0].verification_observation is not None
    assert steps[0].checks, "核对失败的原因必须落在这一步的检查里"
