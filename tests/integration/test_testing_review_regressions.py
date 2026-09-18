"""TASK-003A-R1 的独立反例：在 HTTP 边界注入错误，不改变被测游戏。"""

import json
from pathlib import Path

import httpx
import pytest

from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import ActionStep, ControlSpec, Scenario
from gamepilot.testing.replay import ReplayInputError, compare_case, load_report, replay_report
from gamepilot.testing.runner import ReportWriteError, run_suite, write_report
from gamepilot.testing.scenarios import BASELINE_SCENARIOS

BASE = "http://testserver"
SCENARIOS = {case.case_id: case for case in BASELINE_SCENARIOS}


class MutatingTransport(httpx.AsyncBaseTransport):
    def __init__(self, mutate=None):
        self.inner = httpx.ASGITransport(app=create_app(InMemorySessionRepository()))
        self.mutate = mutate
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        response = await self.inner.handle_async_request(request)
        await response.aread()
        status, body = response.status_code, response.json()
        if self.mutate:
            status, body = self.mutate(len(self.requests), status, body)
        return httpx.Response(status, json=body, request=request)


async def record(scenario, mutate=None):
    transport = MutatingTransport(mutate)
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_suite(GameClient(http, base_url=BASE), "review", (scenario,))
    return report, transport


async def replay(report):
    transport = MutatingTransport()
    async with httpx.AsyncClient(transport=transport) as http:
        result = await replay_report(
            GameClient(http, base_url=BASE), report, source_report="review"
        )
    return result, transport


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "rule"),
    [
        ("potion_not_saved", "R-POTION-DECREMENTS"),
        ("enemy_regenerates", "R-RETALIATE-ONCE"),
        ("attack_uses_potion", "R-ATTACK-HP-APPLY"),
        ("potion_hurts_enemy", "R-POTION-HEAL"),
        ("event_out_of_bounds", "R-INVARIANTS"),
    ],
)
async def test_all_event_state_links_are_checked(mutation, rule):
    def mutate(n, status, body):
        if n == 3:
            if mutation == "enemy_regenerates":
                body["events"][-1]["slime_hp"] = body["slime"]["hp"] = 60
            elif mutation == "attack_uses_potion":
                body["player"]["potions"] = 1
            elif mutation == "event_out_of_bounds":
                body["events"][0]["player_hp"] = 101
        if n == 4:
            if mutation == "potion_not_saved":
                body["player"]["potions"] = 2
            elif mutation == "potion_hurts_enemy":
                body["slime"]["hp"] = 1
                for event in body["events"][-2:]:
                    event["slime_hp"] = 1
        return status, body

    report, _ = await record(SCENARIOS["attack-then-potion"], mutate)
    case = report.cases[0]
    assert case.status == "fail"
    assert rule in {c.rule_id for c in case.checks if c.status == "fail"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("request_number", "mutation", "rule"),
    [
        (2, "id", "R-CREATE-GET-IDENTICAL"),
        (4, "id", "R-REJECT-NO-STATE-CHANGE"),
        (2, "status", "R-GET-STATUS"),
        (4, "status", "R-GET-STATUS"),
        (1, "seed", "R-CREATE-SEED"),
    ],
)
async def test_same_session_http_contracts(request_number, mutation, rule, tmp_path):
    def mutate(n, status, body):
        if n == request_number:
            if mutation == "id":
                body["session_id"] = "f" * 32
            elif mutation == "status":
                status = 404
            elif mutation == "seed":
                body["seed"] = 999
        return status, body

    report, _ = await record(SCENARIOS["potion-at-full-hp-rejected"], mutate)
    case = report.cases[0]
    assert case.status == "fail"
    assert case.failure.rule_id == rule
    # 缺陷观测仍是合法证据，可以加载；不能把服务错误当成报告损坏。
    assert load_report(write_report(report, tmp_path)).cases[0].status == "fail"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("number", "failure"),
    [
        (2, "server"),
        (2, "schema"),
        (3, "schema"),
        (4, "server"),
        (4, "schema"),
    ],
)
async def test_errors_preserve_steps_and_get_observations(number, failure, tmp_path):
    def mutate(n, status, body):
        if n == number:
            return (503 if failure == "server" else 200), {"broken": True}
        return status, body

    report, _ = await record(SCENARIOS["potion-at-full-hp-rejected"], mutate)
    # 第三次请求原预期 409，构造 200 会成为接口 fail，因此另用正常攻击测试解析错误。
    if number == 3:
        report, _ = await record(SCENARIOS["attack-then-potion"], mutate)
    case = report.cases[0]
    assert case.status == "error"
    assert len(case.steps) == len(case.executed_actions) == (0 if number == 2 else 1)
    assert case.final_snapshot is not None
    if number == 2:
        assert case.initial_get_observation.body == {"broken": True}
    else:
        step = case.steps[0]
        observation = step.verification_observation if number == 4 else step.observation
        assert observation.body == {"broken": True}
        assert step.status == "error" and step.error
    loaded = load_report(write_report(report, tmp_path))
    result, _ = await replay(loaded)
    assert result.cases[0].outcome == "not_comparable"
    assert result.cases[0].execution is not None
    assert len(result.cases[0].replayed_actions) <= len(case.executed_actions)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["rule", "schema", "server"])
@pytest.mark.parametrize("rejected_first", [False, True])
async def test_replay_sends_only_recorded_prefix(failure, rejected_first, tmp_path):
    steps = [ActionStep(action="attack"), ActionStep(action="attack")]
    if rejected_first:
        steps.insert(
            0, ActionStep(action="use_potion", expected_status=409, expected_code="player_full_hp")
        )
    scenario = Scenario(
        case_id="prefix",
        description="prefix",
        seed=42,
        steps=steps,
        control=ControlSpec(description="must not run", actions=["attack"]),
    )

    def mutate(n, status, body):
        if n == (5 if rejected_first else 3):
            if failure == "rule":
                body["player"]["hp"] = 1000
            else:
                return (503 if failure == "server" else 200), {"broken": True}
        return status, body

    report, _ = await record(scenario, mutate)
    loaded = load_report(write_report(report, tmp_path))
    result, transport = await replay(loaded)
    actions = [r for r in transport.requests if r.url.path.endswith("/actions")]
    assert len(actions) == (2 if rejected_first else 1)
    assert len(result.cases[0].replayed_actions) == len(report.cases[0].executed_actions)
    assert result.cases[0].execution.control is None
    assert result.cases[0].execution.planned_actions == report.cases[0].planned_actions
    assert result.cases[0].outcome == ("mismatch" if failure == "rule" else "not_comparable")
    assert all(
        diff.field not in {"scenario", "planned_actions"} for diff in result.cases[0].differences
    )


@pytest.mark.anyio
async def test_same_rule_failure_is_reproduced_without_tail(tmp_path):
    scenario = Scenario(
        case_id="prefix",
        description="same defect",
        seed=42,
        steps=[ActionStep(action="attack"), ActionStep(action="attack")],
    )

    def mutate(n, status, body):
        if n == 3:
            body["player"]["hp"] = 1000
        return status, body

    recorded, _ = await record(scenario, mutate)
    async with httpx.AsyncClient(transport=MutatingTransport(mutate)) as http:
        result = await replay_report(
            GameClient(http, base_url=BASE),
            load_report(write_report(recorded, tmp_path)),
            source_report="review",
        )
    assert result.cases[0].outcome == "match"
    assert result.cases[0].execution.status == "fail"
    assert len(result.cases[0].replayed_actions) == 1


@pytest.mark.anyio
async def test_partial_control_trace_is_preserved_and_replayed(tmp_path):
    scenario = Scenario(
        case_id="control",
        description="control",
        seed=42,
        steps=[ActionStep(action="attack")],
        control=ControlSpec(description="control", actions=["attack", "attack"]),
    )

    def mutate(n, status, body):
        return (503, {"code": "internal_error"}) if n == 5 else (status, body)

    recorded, _ = await record(scenario, mutate)
    control = recorded.cases[0].control
    assert len(control.executed_actions) == len(control.observations) == 1
    assert not control.completed
    loaded = load_report(write_report(recorded, tmp_path))
    result, transport = await replay(loaded)
    assert len([r for r in transport.requests if r.url.path.endswith("/actions")]) == 2
    assert len(result.cases[0].execution.control.executed_actions) == 1
    assert result.cases[0].outcome == "not_comparable"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("version", "schema_version"),
        ("events", "必需字段"),
        ("extra", "未知字段"),
        ("string_seed", "类型"),
        ("bool_seed", "类型"),
        ("wrong_seed", "seed"),
        ("action", "前缀"),
        ("index", "索引"),
        ("expected", "预期"),
        ("wrong_path", "路径"),
        ("control_limit", "对照动作超过上限"),
    ],
)
async def test_report_validation_rejects_each_corruption(mutation, reason, tmp_path):
    report, _ = await record(SCENARIOS["attack-then-potion"])
    raw = report.model_dump(mode="json")
    case = raw["cases"][0]
    if mutation == "version":
        raw.pop("schema_version")
    elif mutation == "events":
        case["initial_snapshot"].pop("events")
    elif mutation == "extra":
        case["initial_snapshot"]["hidden_field"] = 99
    elif mutation == "string_seed":
        case["initial_snapshot"]["seed"] = "42"
    elif mutation == "bool_seed":
        case["initial_snapshot"]["seed"] = True
    elif mutation == "wrong_seed":
        case["scenario"]["seed"] = 999
    elif mutation == "action":
        case["executed_actions"][0]["action"] = "use_potion"
    elif mutation == "index":
        case["executed_actions"][0]["index"] = 9
    elif mutation == "expected":
        case["steps"][0]["expected_status"] = 409
    elif mutation == "wrong_path":
        case["steps"][0]["observation"]["path"] = "/api/v1/game-sessions/" + "f" * 32 + "/actions"
    elif mutation == "control_limit":
        case["scenario"]["control"] = {"description": "oversized", "actions": ["attack"] * 21}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ReplayInputError, match=reason):
        load_report(path)


@pytest.mark.anyio
async def test_comparison_does_not_hide_foreign_response_id():
    report, _ = await record(SCENARIOS["attack-then-potion"])
    original = report.cases[0]
    tampered = original.model_copy(deep=True)
    tampered.steps[0].observation.body["session_id"] = "f" * 32
    assert any(diff.field.endswith("body") for diff in compare_case(original, tampered))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (502, b"<html>bad gateway</html>", "server_error"),
        (409, b'{"code":"persistence_conflict"}', "persistence_error"),
    ],
)
async def test_execution_error_precedence(status, body, kind):
    transport = httpx.MockTransport(lambda r: httpx.Response(status, content=body, request=r))
    async with httpx.AsyncClient(transport=transport) as http:
        report = await run_suite(
            GameClient(http, base_url=BASE), "review", (SCENARIOS["initial-state"],)
        )
    assert report.cases[0].status == "error"
    assert report.cases[0].create_observation.error_kind == kind


@pytest.mark.anyio
async def test_write_permission_failure_becomes_report_error(tmp_path, monkeypatch):
    report, _ = await record(SCENARIOS["initial-state"])

    def deny(*args, **kwargs):
        raise PermissionError("controlled failure")

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(ReportWriteError, match="PermissionError"):
        write_report(report, tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["create", "get", "completed", "summary", "control_create"])
async def test_null_or_inconsistent_success_evidence_is_rejected(mutation, tmp_path):
    report, _ = await record(SCENARIOS["reproducible-duplicate-run"])
    raw = report.model_dump(mode="json")
    case = raw["cases"][0]
    if mutation == "create":
        case["create_observation"] = None
    elif mutation == "get":
        case["initial_get_observation"] = None
    elif mutation == "completed":
        case["actions_completed"] = False
    elif mutation == "summary":
        raw["summary"]["passed"] = 20
    elif mutation == "control_create":
        case["control"]["create_observation"] = None
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ReplayInputError):
        load_report(path)
