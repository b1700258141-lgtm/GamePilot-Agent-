"""执行器、判定与重跑的集成测试：通过 ASGITransport 驱动真实应用。

测试用的客户端是被测应用的真实路由（同一份 ASGI 应用、同一份适配器），
只是把网络层换成进程内传输：不占端口、不受本机网络影响，行为与真实 HTTP 一致。
故障注入则由一个可编程的 transport 完成（连接失败/超时/5xx/非法 JSON），
因此「基础设施故障必须记成 error 而不是 fail」可以被精确验证。

构造的缺陷只存在于测试数据里（预料/坏观测），不会把真实游戏改成缺陷模式。
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import (
    ActionStep,
    HttpObservation,
    RuleCheck,
    Scenario,
    normalize_snapshot,
)
from gamepilot.testing.replay import ReplayInputError, load_report, replay_report
from gamepilot.testing.rules import MAX_ACTION_ATTEMPTS, RULES_VERSION, SCHEMA_VERSION
from gamepilot.testing.runner import ReportWriteError, run_case, run_suite, write_report
from gamepilot.testing.scenarios import BASELINE_SCENARIOS, SUITE_BASELINE, get_suite

BASE_URL = "http://testserver"


def _app():
    return create_app(InMemorySessionRepository())


@pytest.fixture
async def game_client() -> AsyncIterator[GameClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url=BASE_URL
    ) as http:
        yield GameClient(http, base_url=BASE_URL)


def _scenario(case_id: str) -> Scenario:
    return next(scenario for scenario in get_suite(SUITE_BASELINE) if scenario.case_id == case_id)


def _rule_ids(checks: list[RuleCheck]) -> set[str]:
    return {check.rule_id for check in checks}


# ------------------------------------------------------------------- 基线套件


@pytest.mark.anyio
async def test_baseline_suite_passes_on_in_memory_app(game_client: GameClient) -> None:
    report = await run_suite(game_client, SUITE_BASELINE)

    assert report.summary.errored == 0, [case.error for case in report.cases]
    assert report.summary.failed == 0, [case.failure for case in report.cases]
    assert report.summary.passed == len(BASELINE_SCENARIOS) == report.summary.total
    assert report.rules_version == RULES_VERSION
    assert report.schema_version == SCHEMA_VERSION
    # 每个场景都在自己的新会话里执行。
    session_ids = [case.session_id for case in report.cases]
    assert all(session_ids)
    assert len(set(session_ids)) == len(session_ids)


@pytest.mark.anyio
async def test_rejected_action_is_verified_by_get_without_state_change(
    game_client: GameClient,
) -> None:
    case = await run_case(game_client, _scenario("potion-at-full-hp-rejected"))

    assert case.status == "pass"
    step = case.steps[0]
    assert step.observed_status == 409
    assert step.observed_code == "player_full_hp"
    # 409 之后必须用 GET 证明状态与完整事件未变，而不是推断。
    assert step.after is not None
    assert "R-REJECT-NO-STATE-CHANGE" in _rule_ids(step.checks)
    assert normalize_snapshot(step.after) == normalize_snapshot(step.before)
    assert step.before.events == step.after.events


@pytest.mark.anyio
async def test_potion_heal_is_measured_from_potion_event_then_retaliation(
    game_client: GameClient,
) -> None:
    """治疗后反击：治疗量看药水事件，最终 HP 已经过反击。"""
    case = await run_case(game_client, _scenario("potion-exhaustion"))

    assert case.status == "pass"
    potion_event = case.steps[1].new_events[0]
    retaliate_event = case.steps[1].new_events[1]
    assert potion_event.kind == "potion"
    assert potion_event.value == min(25, 100 - case.steps[1].before.player.hp)
    assert potion_event.player_hp == case.steps[1].before.player.hp + potion_event.value
    assert retaliate_event.actor == "slime"
    assert case.steps[1].after.player.hp == potion_event.player_hp - retaliate_event.value
    # 第三次喝药被拒绝：药水已用完，且状态未变。
    assert case.steps[3].observed_code == "no_potions"
    assert case.steps[3].observation.body is not None


@pytest.mark.anyio
async def test_rejected_action_does_not_change_later_randomness(game_client: GameClient) -> None:
    """对照运行：插入被拒绝的动作后，后续被接受动作的结果不变。"""
    case = await run_case(game_client, _scenario("rejected-action-keeps-rng"))

    assert case.status == "pass"
    assert case.control is not None
    assert case.control.status_codes == [200, 200, 200]
    assert normalize_snapshot(case.final_snapshot) == normalize_snapshot(
        case.control.final_snapshot
    )
    assert case.executed_actions[0].status_code == 409


@pytest.mark.anyio
async def test_same_seed_and_actions_reproduce_normalized_state(game_client: GameClient) -> None:
    scenario = _scenario("reproducible-duplicate-run")

    first = await run_case(game_client, scenario)
    second = await run_case(game_client, scenario)

    assert first.session_id != second.session_id
    assert normalize_snapshot(first.final_snapshot) == normalize_snapshot(second.final_snapshot)
    assert first.final_snapshot.events == second.final_snapshot.events


# --------------------------------------------------------------- 失败与错误


@pytest.mark.anyio
async def test_wrong_expectation_fails_at_first_step_and_stops(game_client: GameClient) -> None:
    """预期与实际不符属于 fail（而不是 error），并停在首个失败处。"""
    scenario = Scenario(
        case_id="wrong-expectation",
        description="构造的错预期：期望攻击返回 409",
        seed=42,
        steps=[
            ActionStep(action="attack", expected_status=409, expected_code="battle_not_active"),
            ActionStep(action="attack"),
        ],
    )

    case = await run_case(game_client, scenario)

    assert case.status == "fail"
    assert case.failure is not None
    assert case.failure.rule_id == "R-EXPECTED-STATUS"
    assert case.failure.step == 1
    # 第 2 步没有执行；第 1 步的观测证据完整保留。
    assert len(case.executed_actions) == 1
    assert case.steps[0].observed_status == 200
    assert case.steps[0].observation.body is not None


@pytest.mark.anyio
async def test_wrong_final_status_expectation_hits_final_status_rule(
    game_client: GameClient,
) -> None:
    scenario = Scenario(
        case_id="wrong-final-status",
        description="构造的错预期：只攻击一次却声明胜利",
        seed=12345,
        steps=[ActionStep(action="attack")],
        expected_final_status="won",
    )

    case = await run_case(game_client, scenario)

    assert case.status == "fail"
    assert case.failure is not None
    assert case.failure.rule_id == "R-FINAL-STATUS"
    assert "R-FINAL-STATUS" in _rule_ids(case.checks)


@pytest.mark.anyio
async def test_action_attempt_limit_counts_rejected_actions_and_stops(
    game_client: GameClient,
) -> None:
    """上限之内以「胜利 + 被拒绝动作」填满，第 21 次尝试必须不再发出。"""
    steps = [ActionStep(action="attack") for _ in range(3)]
    steps += [
        ActionStep(action="attack", expected_status=409, expected_code="battle_not_active")
        for _ in range(MAX_ACTION_ATTEMPTS - 2)
    ]
    scenario = Scenario(
        case_id="step-limit",
        description=f"计划动作数为 {MAX_ACTION_ATTEMPTS + 1}，触发上限",
        seed=42,
        steps=steps,
    )

    case = await run_case(game_client, scenario)

    assert case.status == "error"
    assert case.failure is None
    assert case.error is not None and "上限" in case.error
    # 前 20 次尝试都真实发出（含被拒绝的 17 次），第 21 次没有发出。
    assert len(case.executed_actions) == MAX_ACTION_ATTEMPTS
    assert len(case.steps) == MAX_ACTION_ATTEMPTS
    assert case.executed_actions[-1].status_code == 409
    checks = [check for check in case.checks if check.rule_id == "R-STEP-LIMIT"]
    assert [check.status for check in checks] == ["error"]


class _FaultyTransport(httpx.AsyncBaseTransport):
    """前 `fail_at - 1` 个请求转发给真实应用，之后按模式注入故障。"""

    def __init__(self, mode: str, fail_at: int = 1) -> None:
        self._mode = mode
        self._fail_at = fail_at
        self._inner = httpx.ASGITransport(app=_app())
        self._count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._count += 1
        if self._count < self._fail_at:
            return await self._inner.handle_async_request(request)
        if self._mode == "connection":
            raise httpx.ConnectError("注入的连接失败", request=request)
        if self._mode == "timeout":
            raise httpx.ReadTimeout("注入的超时", request=request)
        if self._mode == "server_error":
            return httpx.Response(500, json={"detail": "注入的服务端故障"}, request=request)
        if self._mode == "invalid_json":
            return httpx.Response(200, content=b"<html>not json</html>", request=request)
        raise AssertionError(f"未知注入模式：{self._mode}")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "expected_kind"),
    [
        ("connection", "connection"),
        ("timeout", "timeout"),
        ("server_error", "server_error"),
        ("invalid_json", "invalid_response"),
    ],
)
async def test_infrastructure_failure_on_create_is_error_not_failure(
    mode: str, expected_kind: str
) -> None:
    async with httpx.AsyncClient(transport=_FaultyTransport(mode), base_url=BASE_URL) as http:
        client = GameClient(http, base_url=BASE_URL)
        case = await run_case(client, _scenario("initial-state"))

    assert case.status == "error"
    assert case.failure is None
    assert case.error is not None and expected_kind in case.error
    # 基础设施故障不会被伪装成游戏规则缺陷，但状态码与响应体必须留证。
    assert case.create_observation is not None
    assert case.create_observation.error_kind == expected_kind
    if mode == "server_error":
        assert case.create_observation.status_code == 500
        assert case.create_observation.body == {"detail": "注入的服务端故障"}
    if mode == "invalid_json":
        assert case.create_observation.status_code == 200


@pytest.mark.anyio
async def test_infrastructure_failure_on_action_preserves_step_evidence() -> None:
    """创建与查询正常，第 3 个请求（第一次动作）失败：步骤证据仍在。"""
    async with httpx.AsyncClient(
        transport=_FaultyTransport("server_error", fail_at=3), base_url=BASE_URL
    ) as http:
        client = GameClient(http, base_url=BASE_URL)
        case = await run_case(client, _scenario("win-then-rejected"))

    assert case.status == "error"
    assert case.failure is None
    assert len(case.executed_actions) == 1
    step = case.steps[0]
    assert step.status == "error"
    assert step.observation.status_code == 500
    assert step.observation.body == {"detail": "注入的服务端故障"}
    assert step.before is not None


# --------------------------------------------------------------------- 报告


@pytest.mark.anyio
async def test_report_is_written_under_generated_run_id(
    game_client: GameClient, tmp_path: Path
) -> None:
    report = await run_suite(game_client, SUITE_BASELINE)

    path = write_report(report, tmp_path)

    assert path.name == f"{report.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["rules_version"] == RULES_VERSION
    assert payload["cases"][0]["scenario"]["case_id"] == BASELINE_SCENARIOS[0].case_id
    # 报告只记录脱敏后的地址，且不含环境变量与凭据。
    assert payload["base_url"] == BASE_URL
    assert "DATABASE_URL" not in path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_report_is_never_overwritten(game_client: GameClient, tmp_path: Path) -> None:
    report = await run_suite(game_client, SUITE_BASELINE)
    path = write_report(report, tmp_path)

    with pytest.raises(ReportWriteError):
        write_report(report, tmp_path)
    assert path.read_text(encoding="utf-8").startswith("{")


@pytest.mark.anyio
async def test_reported_base_url_hides_credentials() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url=BASE_URL
    ) as http:
        client = GameClient(http, base_url="http://user:secret@127.0.0.1:8000/?token=abc")

    assert client.base_url == "http://127.0.0.1:8000"
    assert "secret" not in client.base_url


# --------------------------------------------------------------------- 重跑


@pytest.fixture
async def recorded_report(game_client: GameClient, tmp_path: Path):  # noqa: ANN201
    """先跑一遍基线套件并落盘，供重跑用例读取。"""
    report = await run_suite(game_client, SUITE_BASELINE)
    write_report(report, tmp_path)
    return report, tmp_path


@pytest.mark.anyio
async def test_replay_matches_recorded_run(game_client: GameClient, recorded_report) -> None:  # noqa: ANN001
    recorded, tmp_path = recorded_report

    loaded = load_report(tmp_path / f"{recorded.run_id}.json")
    replayed = await replay_report(
        game_client, loaded, source_report=str(tmp_path / f"{recorded.run_id}.json")
    )

    assert replayed.summary.mismatched == 0, [case.differences for case in replayed.cases]
    assert replayed.summary.not_comparable == 0
    assert replayed.summary.matched == len(recorded.cases)
    assert replayed.source_run_id == recorded.run_id
    replay_path = write_report(replayed, tmp_path)
    assert replay_path.name == f"{replayed.run_id}.json"
    assert replay_path != tmp_path / f"{recorded.run_id}.json"


@pytest.mark.anyio
async def test_replay_target_address_comes_from_command_not_report(
    game_client: GameClient, recorded_report
) -> None:  # noqa: ANN001
    """报告里的地址是坏地址：重跑仍应打到本次命令提供的地址上。"""
    recorded, tmp_path = recorded_report
    path = tmp_path / f"{recorded.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["base_url"] = "http://127.0.0.1:1"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = load_report(path)
    assert loaded.base_url == "http://127.0.0.1:1"
    replayed = await replay_report(game_client, loaded, source_report=str(path))

    assert replayed.base_url == BASE_URL
    assert replayed.summary.mismatched == 0


@pytest.mark.anyio
async def test_replay_detects_changed_initial_state(
    game_client: GameClient, recorded_report
) -> None:  # noqa: ANN001
    recorded, tmp_path = recorded_report
    path = tmp_path / f"{recorded.run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["initial_snapshot"]["player"]["hp"] = 77
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    replayed = await replay_report(game_client, load_report(path), source_report=str(path))

    mismatched = [case for case in replayed.cases if case.outcome == "mismatch"]
    assert [case.case_id for case in mismatched] == [payload["cases"][0]["case_id"]]
    fields = {difference.field for difference in mismatched[0].differences}
    assert "initial_snapshot" in fields
    assert replayed.summary.matched == len(payload["cases"]) - 1


@pytest.mark.anyio
async def test_replay_marks_recorded_error_run_not_comparable(
    game_client: GameClient, tmp_path: Path
) -> None:
    async with httpx.AsyncClient(
        transport=_FaultyTransport("connection"), base_url=BASE_URL
    ) as http:
        broken = GameClient(http, base_url=BASE_URL)
        report = await run_suite(broken, SUITE_BASELINE)
        write_report(report, tmp_path)

    path = tmp_path / f"{report.run_id}.json"
    replayed = await replay_report(game_client, load_report(path), source_report=str(path))

    assert replayed.summary.not_comparable == len(report.cases)
    assert all(case.outcome == "not_comparable" for case in replayed.cases)
    assert "不承诺复现" in replayed.cases[0].note


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing_file", "不存在"),
        ("bad_json", "JSON"),
        ("schema", "schema_version"),
        ("rules", "规则版本"),
        ("mode", "mode"),
        ("extra", "未知字段"),
        ("missing_field", "必需字段"),
        ("empty", "不包含任何场景"),
        ("limit", "超过当前上限"),
        ("steps", "步骤证据数"),
    ],
)
async def test_replay_refuses_unsupported_or_inconsistent_reports(
    recorded_report,
    mutation: str,
    reason: str,
) -> None:
    recorded, tmp_path = recorded_report
    payload = recorded.model_dump(mode="json")
    path = tmp_path / f"{mutation}.json"
    if mutation == "bad_json":
        path.write_text("{ invalid", encoding="utf-8")
    elif mutation != "missing_file":
        if mutation == "schema":
            payload["schema_version"] = "9.9"
        elif mutation == "rules":
            payload["rules_version"] = "0.0.1"
        elif mutation == "mode":
            payload["mode"] = "replay"
        elif mutation == "extra":
            payload["unknown_field"] = True
        elif mutation == "missing_field":
            del payload["rules_source"]
        elif mutation == "empty":
            payload["cases"] = []
        elif mutation == "limit":
            payload["cases"][0]["scenario"]["steps"] = [
                ActionStep(action="attack").model_dump() for _ in range(MAX_ACTION_ATTEMPTS + 1)
            ]
        elif mutation == "steps":
            case = max(payload["cases"], key=lambda item: len(item["steps"]))
            case["steps"].pop()
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReplayInputError, match=reason):
        load_report(path)


@pytest.mark.anyio
async def test_http_observation_error_classification() -> None:
    """适配器把异常归类成稳定标签，且不记录异常文本。"""
    async with httpx.AsyncClient(transport=_FaultyTransport("timeout"), base_url=BASE_URL) as http:
        client = GameClient(http, base_url=BASE_URL)
        observation = await client.get_session("whatever")

    assert isinstance(observation, HttpObservation)
    assert observation.status_code is None
    assert observation.error_kind == "timeout"
    assert observation.error_detail == "ReadTimeout"
    assert observation.is_error


def test_get_suite_unknown_name_raises() -> None:
    with pytest.raises(KeyError):
        get_suite("no-such-suite")
