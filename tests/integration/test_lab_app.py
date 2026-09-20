"""缺陷靶场应用：真实 ASGI 路由、真实状态变异、隔离性与「没有 profile 开关」。

靶场与正常服务共用同一份路由与异常处理，因此这里的请求走的是完全相同的
HTTP 路径与线上格式；区别只在于新会话由哪个会话工厂创建。

隔离性要求是硬约束，逐条验证：不同 profile 的应用互不共享会话与触发记录；
正常服务的应用没有被改动、也没有多出任何靶场属性；游戏 API 上不存在
读取或切换 profile 的入口。
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from gamepilot.domain.combat import PLAYER_MAX_HP
from gamepilot.lab import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_NORMAL,
    PROFILES,
    TriggerRecorder,
    UnknownProfileError,
    create_lab_app,
    lab_profile_of,
)
from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.testing.client import GameClient
from gamepilot.testing.rules import R_NO_RETALIATE_ON_KILL, R_POTION_CAP, R_POTION_DECREMENTS
from gamepilot.testing.runner import run_case
from gamepilot.testing.scenarios import BASELINE_SCENARIOS, SUITE_BASELINE, get_suite

BASE_URL = "http://lab.testserver"
CREATE_PATH = "/api/v1/game-sessions"
CREATED = 201
DESIGNATED_CASE = "attack-then-potion"
FAULT_TARGET_RULES = {
    FAULT_POTION_OVERHEAL: R_POTION_CAP,
    FAULT_POTION_NOT_CONSUMED: R_POTION_DECREMENTS,
    FAULT_RETALIATE_AFTER_DEATH: R_NO_RETALIATE_ON_KILL,
}
FAULT_CASES = {
    FAULT_POTION_OVERHEAL: "attack-then-potion",
    FAULT_POTION_NOT_CONSUMED: "attack-then-potion",
    FAULT_RETALIATE_AFTER_DEATH: "win-then-rejected",
}


def _scenario(case_id: str):
    return next(item for item in get_suite(SUITE_BASELINE) if item.case_id == case_id)


class LabHarness:
    """一个靶场应用 + 驱动它的客户端；两者一一对应。"""

    def __init__(self, app: FastAPI, http: httpx.AsyncClient, recorder: TriggerRecorder) -> None:
        self.app = app
        self.http = http
        self.client = GameClient(http, base_url=BASE_URL)
        self.recorder = recorder


@pytest.fixture
async def lab_app() -> AsyncIterator[LabHarness]:
    """默认靶场实例（normal）：隔离性用例的参照物。"""
    recorder = TriggerRecorder()
    app = create_lab_app(PROFILE_NORMAL, recorder=recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        yield LabHarness(app, http, recorder)


def _lab_app(profile: str, recorder: TriggerRecorder | None = None) -> FastAPI:
    return create_lab_app(profile, recorder=recorder if recorder is not None else TriggerRecorder())


async def _create(http: httpx.AsyncClient, payload: dict[str, object]) -> httpx.Response:
    return await http.post(CREATE_PATH, json=payload)


# ------------------------------------------------------------------ 接口一致性


@pytest.mark.anyio
async def test_lab_app_serves_the_same_api_shape_as_the_normal_service() -> None:
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(InMemorySessionRepository())),
            base_url=BASE_URL,
        ) as normal_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_lab_app(PROFILE_NORMAL)), base_url=BASE_URL
        ) as lab_http,
    ):
        normal = await _create(normal_http, {"seed": 42})
        lab = await _create(lab_http, {"seed": 42})

        assert normal.status_code == lab.status_code == CREATED
        normal_body, lab_body = normal.json(), lab.json()
        # 除会话 id 外逐字段相同：靶场没有改写任何响应。
        assert set(normal_body) == set(lab_body)
        assert normal_body | {"session_id": lab_body["session_id"]} == lab_body


# ------------------------------------------------------------ normal profile


@pytest.mark.anyio
async def test_normal_lab_app_passes_the_baseline_suite_without_triggers(
    lab_app: LabHarness,
) -> None:
    for scenario in get_suite(SUITE_BASELINE):
        case = await run_case(lab_app.client, scenario)
        assert case.status == "pass", case.failure

    assert len(lab_app.recorder) == 0
    assert lab_profile_of(lab_app.app).fault_id is None


# ---------------------------------------------------------- 每个缺陷真的触发


@pytest.mark.anyio
@pytest.mark.parametrize("fault_id", sorted(FAULT_CASES))
async def test_each_fault_profile_really_triggers_its_own_fault(fault_id: str) -> None:
    recorder = TriggerRecorder()
    app = _lab_app(fault_id, recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        case = await run_case(GameClient(http, base_url=BASE_URL), _scenario(FAULT_CASES[fault_id]))

    assert case.status == "fail"
    failing = {check.rule_id for check in case.checks if check.status == "fail"}
    assert FAULT_TARGET_RULES[fault_id] in failing

    trigger = recorder.get(case.session_id)
    assert trigger is not None
    assert trigger.fault_id == fault_id
    # 触发记录只有一个，且属于本次会话。
    assert len(recorder) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("fault_id", sorted(FAULT_CASES))
async def test_trigger_detail_is_not_visible_over_http(fault_id: str) -> None:
    """判定器只看得到 HTTP 观测，看不到缺陷标签或触发说明。"""
    recorder = TriggerRecorder()
    app = _lab_app(fault_id, recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        case = await run_case(GameClient(http, base_url=BASE_URL), _scenario(FAULT_CASES[fault_id]))
        assert case.status == "fail"
        fetched = await http.get(f"{CREATE_PATH}/{case.session_id}")

    session_id = case.session_id
    trigger = recorder.get(session_id)
    assert trigger is not None
    payload = json.dumps(fetched.json(), ensure_ascii=False)
    assert trigger.detail not in payload
    assert fault_id not in payload
    assert "fault" not in payload


# ------------------------------------------------------------------ 隔离性


@pytest.mark.anyio
async def test_sessions_are_not_shared_between_lab_apps(lab_app: LabHarness) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_lab_app(FAULT_POTION_OVERHEAL)), base_url=BASE_URL
    ) as other_http:
        created = await _create(lab_app.http, {"seed": 42})
        session_id = created.json()["session_id"]

        missing = await other_http.get(f"{CREATE_PATH}/{session_id}")

    assert missing.status_code == 404
    assert missing.json()["code"] == "session_not_found"


@pytest.mark.anyio
async def test_all_profiles_run_in_one_process_without_crosstalk() -> None:
    """四个应用同进程并存：各自只记录自己的会话，normal 不记录任何触发。"""
    recorders = {profile: TriggerRecorder() for profile in PROFILES}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_lab_app(PROFILE_NORMAL, recorders[PROFILE_NORMAL])),
        base_url=BASE_URL,
    ) as http:
        for profile in PROFILES[1:]:
            recorder = recorders[profile]
            app = _lab_app(profile, recorder)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=BASE_URL
            ) as fault_http:
                case = await run_case(
                    GameClient(fault_http, base_url=BASE_URL), _scenario(FAULT_CASES[profile])
                )
            assert case.status == "fail"
            assert len(recorder) == 1
            # 其他 profile 的记录器看不到这个会话。
            for other, other_recorder in recorders.items():
                if other != profile:
                    assert other_recorder.get(case.session_id) is None

        # normal 上的同一个场景仍然通过，且不产生任何触发。
        normal_case = await run_case(
            GameClient(http, base_url=BASE_URL), _scenario(DESIGNATED_CASE)
        )
        assert normal_case.status == "pass"
        assert len(recorders[PROFILE_NORMAL]) == 0


# --------------------------------------------- 不读配置、不建 Engine、无开关


@pytest.mark.parametrize("name", ["", "normal-but-not-really", "all", "potion_overheal+overheal"])
def test_illegal_profile_is_rejected(name: str) -> None:
    """非法 profile 明确报错，绝不退回 normal。"""
    with pytest.raises(UnknownProfileError):
        create_lab_app(name)


def test_lab_app_never_creates_a_database_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使环境要求 postgres，靶场也只使用内存仓储。"""
    calls: list[str] = []

    def _explode(*args: object, **kwargs: object) -> None:
        calls.append("create_db_engine")
        raise AssertionError("靶场不允许创建数据库 Engine")

    monkeypatch.setenv("REPOSITORY_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://invalid@127.0.0.1:1/none")
    monkeypatch.setattr("gamepilot.persistence.database.create_db_engine", _explode)
    monkeypatch.setattr("gamepilot.main.create_db_engine", _explode)

    app = create_lab_app(FAULT_POTION_OVERHEAL)

    assert calls == []
    assert isinstance(app.state.repository, InMemorySessionRepository)
    assert app.state.session_factory is not None


@pytest.mark.anyio
async def test_lab_app_is_usable_with_a_broken_postgres_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPOSITORY_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "not-a-database-url")

    app = _lab_app(FAULT_POTION_OVERHEAL)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        created = await _create(http, {"seed": 42})

    assert created.status_code == CREATED


def test_normal_service_app_has_no_lab_state() -> None:
    app = create_app(InMemorySessionRepository())

    assert not hasattr(app.state, "lab_profile")
    assert not hasattr(app.state, "trigger_recorder")
    with pytest.raises(AttributeError):
        lab_profile_of(app)


@pytest.mark.anyio
async def test_game_api_exposes_no_profile_switch(lab_app: LabHarness) -> None:
    """游戏 API 上没有读取或切换 profile 的入口，参数与响应里都没有它。"""
    schema = lab_app.app.openapi()
    operations = json.dumps(schema, ensure_ascii=False)

    assert "/api/v1/game-sessions" in schema["paths"]
    assert "profile" not in operations
    assert "fault" not in operations
    assert list(schema["paths"][CREATE_PATH]) == ["post"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lab_app.app), base_url=BASE_URL
    ) as http:
        document = await http.get("/openapi.json")

    assert document.status_code == 200
    assert "profile" not in json.dumps(document.json(), ensure_ascii=False)


@pytest.mark.anyio
async def test_profile_cannot_be_selected_through_the_request_body() -> None:
    """客户端在请求体里塞 profile 字段既不能切换也不能叠加缺陷。"""
    recorder = TriggerRecorder()
    app = _lab_app(FAULT_POTION_OVERHEAL, recorder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        created = await _create(http, {"seed": 42, "profile": PROFILE_NORMAL, "fault_id": "none"})
        assert created.status_code == CREATED
        session_id = created.json()["session_id"]
        await http.post(f"{CREATE_PATH}/{session_id}/actions", json={"action": "attack"})
        acted = await http.post(
            f"{CREATE_PATH}/{session_id}/actions", json={"action": "use_potion"}
        )

    # 缺陷仍然生效：缺失 20 点却治疗 25 点，生命越过上限。
    potion_event = [event for event in acted.json()["events"] if event["kind"] == "potion"][-1]
    assert potion_event["value"] == 25
    assert potion_event["player_hp"] == PLAYER_MAX_HP + 5
    assert recorder.get(session_id) is not None


def test_baseline_scenarios_are_unchanged_by_the_lab() -> None:
    """靶场不新增、不修改场景：评测用的仍然只有既有基线套件。"""
    assert len(BASELINE_SCENARIOS) == 8
    assert len(get_suite(SUITE_BASELINE)) == 8
