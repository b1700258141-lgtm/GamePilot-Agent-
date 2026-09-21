"""Agent 产出的 RunReport 必须能被**既有** replay 原样重跑。

Agent 的动作是模型逐步选出来的，但产物与脚本执行器完全同构：既有的严格
读取器能读、动作逐条重放、比对逐字段进行——重跑过程**完全不需要模型**。

四种收尾方式各自覆盖一条重跑路径：

1. 正常完成 → `match`；
2. 规则失败早停 → 同 profile 重跑仍 `match`（失败是可复现的事实）；
3. 预算打断的前缀 → 前缀逐字重放，仍 `match`；
4. 执行错误 → `not_comparable`（原错误不承诺复现，但也不降级成 mismatch）。

另外两条是「重跑确实在独立核对」的证据：缺陷轨迹换到 normal 上必须
`mismatch`（否则比对形同虚设），而重跑自身出错必须记 `not_comparable`
而不是 `mismatch`。
"""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

# `langgraph` 属于可选的 agent extra（见 pyproject 与 README「游戏测试 Agent」）：
# 没装时整组测试明确跳过，而不是让收集阶段直接报导入错误。
pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gamepilot.agent.budget import Budget
from gamepilot.agent.goals import GOAL_HEALING, resolve_goal
from gamepilot.agent.graph import run_agent
from gamepilot.agent.models import BudgetSpec, ChatMessage, ToolCallRequest
from gamepilot.agent.provider import FakeProvider, ModelReply
from gamepilot.agent.report import build_agent_report, write_run_report
from gamepilot.agent.tools import TOOL_PERFORM_ACTION
from gamepilot.lab import create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.replay import ReplayInputError, load_report, replay_report
from gamepilot.testing.reporting import new_run_id, utc_now_iso

BASE_URL = "http://agent-replay.invalid"


# -------------------------------------------------------------- 替身与客户端


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


class Responder:
    def __init__(self, policy) -> None:
        self._policy = policy
        self.seen: list[list[ChatMessage]] = []

    def __call__(self, messages: Sequence[ChatMessage]) -> ModelReply:
        self.seen.append(list(messages))
        return self._policy(messages, len(self.seen))


def healing_policy(messages: Sequence[ChatMessage], step: int) -> ModelReply:
    """满血攻击、受伤治疗；只用公开观测决策。"""
    latest = None
    for message in messages:
        if "玩家：生命 " in message.content:
            latest = message.content
    if latest is None:
        return ModelReply(text="")
    hp = int(latest.split("玩家：生命 ", 1)[1].split("/", 1)[0])
    max_hp = int(latest.split("/", 1)[1].split("，", 1)[0])
    if hp < max_hp:
        return action_call("use_potion", step)
    return action_call("attack", step)


def always_attack(messages: Sequence[ChatMessage], step: int) -> ModelReply:  # noqa: ARG001
    return action_call("attack", step)


def open_client(app: FastAPI, *, timeout: float = 5.0) -> tuple[GameClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=BASE_URL
    )
    return GameClient(http, base_url=BASE_URL, timeout=timeout), http


@pytest.fixture
async def normal() -> AsyncIterator[GameClient]:
    client, http = open_client(create_lab_app("normal"))
    try:
        yield client
    finally:
        await http.aclose()


# ------------------------------------------------------------------ 产出报告


async def produce_report(
    client: GameClient,
    profile: str,
    goal_id: str,
    policy,
    *,
    spec: BudgetSpec | None = None,
    tmp_path=None,
) -> tuple[object, object, object, object]:
    """跑一条 Agent 闭环，把游戏事实写成 RunReport 并落盘。

    返回 `(RunReport, 落盘路径, 运行上下文, Agent 报告)`。
    """
    run_id = new_run_id()
    state, context, _meta = await run_agent(
        client=client,
        provider=FakeProvider(Responder(policy)),
        budget=Budget(spec or BudgetSpec()),
        goal=resolve_goal(goal_id),
        seed=42,
        description=f"重跑测试 {profile} / {goal_id}",
    )
    report = build_agent_report(
        state=state,
        context=context,
        client=client,
        run_id=run_id,
        started_at=utc_now_iso(),
        duration_ms=0.0,
        run_report=None,
    )
    case = await context.execution.finalize()
    run_report, path = write_run_report(case, client, run_id, tmp_path)
    return run_report, path, context, report


# ------------------------------------------------- 1. 四种收尾都能重跑


@pytest.mark.anyio
async def test_normal_completion_replays_to_match(normal: GameClient, tmp_path) -> None:
    """正常完成：读回落盘的报告并在**新会话**里重跑，逐字段一致。"""
    run_report, path, _context, agent_report = await produce_report(
        normal, "normal", GOAL_HEALING, healing_policy, tmp_path=tmp_path
    )
    assert agent_report.summary.exit_code == 0
    assert run_report.cases[0].status == "pass"

    reloaded = load_report(path)  # 走既有的严格读取器，不绕过校验
    replay = await replay_report(normal, reloaded, source_report=str(path))

    assert replay.summary.mismatched == 0
    assert replay.summary.not_comparable == 0
    assert replay.summary.matched == 1
    assert replay.cases[0].outcome == "match"
    # 实际动作逐条一致：重跑的是模型当初选的那几个动作，不是别的。
    recorded = reloaded.cases[0].executed_actions
    replayed = replay.cases[0].replayed_actions
    assert [item.action for item in replayed] == [item.action for item in recorded]
    assert len(replayed) == len(recorded)


@pytest.mark.anyio
async def test_rule_failure_early_stop_replays_to_match(tmp_path) -> None:
    """规则失败早停：失败本身是可复现的事实，重跑必须仍然一致。"""
    client, http = open_client(create_lab_app("potion_overheal"))
    try:
        run_report, path, _context, agent_report = await produce_report(
            client, "potion_overheal", GOAL_HEALING, healing_policy, tmp_path=tmp_path
        )
        assert agent_report.summary.exit_code == 1
        assert run_report.cases[0].status == "fail"

        reloaded = load_report(path)
        replay = await replay_report(client, reloaded, source_report=str(path))
    finally:
        await http.aclose()

    assert replay.cases[0].outcome == "match"
    assert replay.summary.matched == 1
    assert reloaded.cases[0].failure is not None


@pytest.mark.anyio
async def test_budget_terminated_prefix_replays_to_match(normal: GameClient, tmp_path) -> None:
    """被动作预算打断的前缀：已发生的动作逐字重跑，仍一致。"""
    spec = BudgetSpec(max_action_attempts=5, max_model_calls=12)
    run_report, path, context, agent_report = await produce_report(
        normal, "normal", GOAL_HEALING, always_attack, spec=spec, tmp_path=tmp_path
    )
    assert agent_report.summary.stop_reason == "action_budget"
    assert agent_report.summary.actions_attempted == spec.max_action_attempts
    # 任务未完成，但前缀本身没有任何违规：报告里的游戏事实如实保留。
    assert run_report.cases[0].status == "pass"
    assert len(run_report.cases[0].steps) == spec.max_action_attempts
    assert context.rule_failures == []

    replay = await replay_report(normal, load_report(path), source_report=str(path))
    assert replay.cases[0].outcome == "match"
    assert replay.summary.mismatched == 0


@pytest.mark.anyio
async def test_execution_error_replays_as_not_comparable(tmp_path) -> None:
    """执行错误：原错误不承诺复现，因此记 `not_comparable`——不是 match，也不是 mismatch。"""
    app = FastAPI()
    snapshot = {
        "session_id": "broken",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 100, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return {**snapshot, "session_id": session_id}

    @app.post("/api/v1/game-sessions/{session_id}/actions")
    async def act(session_id: str, payload: dict) -> dict:  # noqa: ARG001
        return JSONResponse({"error": "boom"}, status_code=503)

    broken, broken_http = open_client(app)
    try:
        run_report, path, _context, _agent = await produce_report(
            broken, "broken", GOAL_HEALING, always_attack, tmp_path=tmp_path
        )
        assert run_report.cases[0].status == "error"

        # 重跑目标是一个**正常**服务：原错误不复现，所以结论只能是「不可比」。
        replayer, replayer_http = open_client(create_lab_app("normal"))
        try:
            replay = await replay_report(replayer, load_report(path), source_report=str(path))
        finally:
            await replayer_http.aclose()
    finally:
        await broken_http.aclose()

    assert replay.cases[0].outcome == "not_comparable"
    assert replay.summary.not_comparable == 1
    assert replay.summary.mismatched == 0
    assert "执行错误" in replay.cases[0].note


# ----------------------------------------------------- 2. 重跑真的在核对


@pytest.mark.anyio
async def test_defect_trace_replayed_on_normal_is_a_mismatch(tmp_path) -> None:
    """缺陷轨迹换到 normal 上必须 mismatch——否则「比对通过」毫无意义。"""
    client, http = open_client(create_lab_app("potion_overheal"))
    try:
        run_report, path, _context, agent_report = await produce_report(
            client, "potion_overheal", GOAL_HEALING, healing_policy, tmp_path=tmp_path
        )
        assert agent_report.summary.exit_code == 1
        reloaded = load_report(path)
        # 报告里确实带着失败证据，不是只会「通过」。
        failing = [check.rule_id for check in reloaded.cases[0].checks if check.status == "fail"]
        assert failing
    finally:
        await http.aclose()

    normal_client, normal_http = open_client(create_lab_app("normal"))
    try:
        replay = await replay_report(normal_client, reloaded, source_report=str(path))
    finally:
        await normal_http.aclose()

    assert replay.cases[0].outcome == "mismatch"
    assert replay.summary.mismatched == 1
    # 差异指向真正的分歧：原运行 fail、重跑 pass。
    fields = {difference.field for difference in replay.cases[0].differences}
    assert "status" in fields
    assert any(field.startswith("checks") or field.startswith("steps") for field in fields)


@pytest.mark.anyio
async def test_replay_execution_error_does_not_degrade_to_mismatch(
    normal: GameClient, tmp_path
) -> None:
    """重跑自身出错时记 `not_comparable`：不能把「跑不出来」写成「不一致」。"""
    _run_report, path, _context, _agent = await produce_report(
        normal, "normal", GOAL_HEALING, healing_policy, tmp_path=tmp_path
    )

    app = FastAPI()
    snapshot = {
        "session_id": "broken",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 100, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return {**snapshot, "session_id": session_id}

    @app.post("/api/v1/game-sessions/{session_id}/actions")
    async def act(session_id: str, payload: dict) -> dict:  # noqa: ARG001
        return JSONResponse({"error": "boom"}, status_code=503)

    replayer, http = open_client(app)
    try:
        replay = await replay_report(replayer, load_report(path), source_report=str(path))
    finally:
        await http.aclose()

    assert replay.cases[0].outcome == "not_comparable"
    assert replay.summary.mismatched == 0
    assert "执行错误" in replay.cases[0].note


# ------------------------------------------------------------- 3. 严格读取


@pytest.mark.anyio
async def test_a_tampered_report_is_rejected_before_any_request(
    normal: GameClient, tmp_path
) -> None:
    """篡改动作边界的报告在读入阶段就被拒绝，不会先发一轮请求再报错。"""
    import json

    _run_report, path, _context, _agent = await produce_report(
        normal, "normal", GOAL_HEALING, healing_policy, tmp_path=tmp_path
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 把计划动作加到超过既有上限：报告不该被「尽力而为」地接受。
    case = payload["cases"][0]
    case["scenario"]["steps"] = case["scenario"]["steps"] * 10
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReplayInputError):
        load_report(path)
