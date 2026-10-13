"""网页入口在隔离靶场运行，证据、付费守卫与手动会话互不混淆。"""

import asyncio

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')

from gamepilot.main import create_app
from gamepilot.persistence.models import AgentTestRunRow
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.web_runs import AgentWebRunManager


@pytest.mark.anyio
async def test_offline_web_run_detects_fault_and_preserves_manual_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        manual = (await http.post("/api/v1/game-sessions", json={"seed": 42})).json()
        started = await http.post(
            "/api/v1/agent-test-runs",
            json={
                "goal_id": "victory",
                "seed": 42,
                "profile": "retaliate_after_death",
                "provider_mode": "offline",
            },
        )
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        await app.state.agent_run_manager._task
        result = (await http.get(f"/api/v1/agent-test-runs/{run_id}")).json()
        assert result["status"] == "completed"
        assert result["result"]["verdict"] == "defect_detected"
        assert result["result"]["replay_outcome"] == "match"
        assert result["result"]["target_rules_hit"] == ["R-NO-RETALIATE-ON-KILL"]
        assert result["result"]["provider_is_test_double"] is True
        assert any(item["kind"] == "model" for item in result["steps"])
        assert any(item["kind"] == "action" for item in result["steps"])
        reports = (await http.get(f"/api/v1/agent-test-runs/{run_id}/report")).json()
        assert reports["agent"]["summary"]["stop_reason"] == "rule_failure"
        assert reports["run"]["summary"]["failed"] == 1
        assert reports["replay"]["cases"][0]["outcome"] == "match"
        assert (await http.get(f"/api/v1/game-sessions/{manual['session_id']}")).json() == manual


@pytest.mark.anyio
async def test_real_guard_rejects_before_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GAMEPILOT_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        payload = {"goal_id": "victory", "seed": 42, "provider_mode": "real"}
        assert (await http.post("/api/v1/agent-test-runs", json=payload)).status_code == 400
        assert (
            await http.post("/api/v1/agent-test-runs", json={**payload, "paid_ack": True})
        ).status_code == 409
        assert (await http.get("/api/v1/agent-test-runs/config")).json()["real_available"] is False
        assert app.state.agent_run_manager._task is None


@pytest.mark.anyio
async def test_duplicate_real_start_is_rejected_without_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def hold_run(_manager: AgentWebRunManager, _record: dict) -> None:
        nonlocal calls
        calls += 1
        ready.set()
        await release.wait()

    monkeypatch.setattr("gamepilot.web_runs.real_key", lambda: ("test-key-env", "dummy"))
    monkeypatch.setattr(AgentWebRunManager, "_run", hold_run)
    app = create_app(InMemorySessionRepository())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            payload = {"goal_id": "victory", "seed": 42, "provider_mode": "real", "paid_ack": True}
            first = await http.post("/api/v1/agent-test-runs", json=payload)
            assert first.status_code == 202
            await asyncio.wait_for(ready.wait(), timeout=2)
            second = await http.post("/api/v1/agent-test-runs", json=payload)
            assert second.status_code == 409
            assert calls == 1
    finally:
        release.set()
        await app.state.agent_run_manager._task


@pytest.mark.anyio
async def test_offline_web_run_reports_normal_goal_and_invalid_profile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        payload = {"goal_id": "full-health", "seed": 42, "profile": "normal"}
        bad = await http.post("/api/v1/agent-test-runs", json={**payload, "profile": "unknown"})
        assert bad.status_code == 422
        started = await http.post("/api/v1/agent-test-runs", json=payload)
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        result = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert result["status"] == "completed"
        assert result["result"]["verdict"] == "pass"
        assert result["result"]["goal_met"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("seed_value", ["omitted", None])
async def test_offline_web_run_generates_and_records_seed_when_blank(
    seed_value: str | None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    monkeypatch.setattr("gamepilot.api.agent_test_routes.secrets.randbits", lambda bits: 123456789)
    app = create_app(InMemorySessionRepository())
    payload: dict[str, object] = {"goal_id": "full-health", "provider_mode": "offline"}
    if seed_value is None:
        payload["seed"] = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post("/api/v1/agent-test-runs", json=payload)
        assert started.status_code == 202
        assert started.json()["config"]["seed"] == 123456789
        await app.state.agent_run_manager._task
        result = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert result["status"] == "completed"
        assert result["config"]["seed"] == 123456789
        assert result["steps"][0]["snapshot"]["seed"] == 123456789
        assert result["result"]["replay_outcome"] == "match"


@pytest.mark.anyio
@pytest.mark.parametrize("profile", ["potion_overheal", "potion_not_consumed"])
async def test_offline_web_run_detects_healing_faults(
    profile: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post(
            "/api/v1/agent-test-runs",
            json={"goal_id": "healing", "seed": 42, "profile": profile},
        )
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        result = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert result["status"] == "completed"
        assert result["result"]["verdict"] == "defect_detected"
        assert result["result"]["triggered"] is True
        assert result["result"]["target_rules_hit"]
        assert result["result"]["replay_outcome"] == "match"


@pytest.mark.anyio
async def test_run_and_report_restore_from_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AgentTestRunRow.__table__.create(engine)
    try:
        factory = sessionmaker(engine, expire_on_commit=False)
        app = create_app(InMemorySessionRepository())
        app.state.agent_run_manager = AgentWebRunManager(factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            started = await http.post(
                "/api/v1/agent-test-runs", json={"goal_id": "full-health", "seed": 42}
            )
            assert started.status_code == 202
            await app.state.agent_run_manager._task
            run_id = started.json()["run_id"]
            app.state.agent_run_manager = AgentWebRunManager(factory)
            restored = (await http.get(f"/api/v1/agent-test-runs/{run_id}")).json()
            assert restored["status"] == "completed"
            assert restored["result"]["verdict"] == "pass"
            report = (await http.get(f"/api/v1/agent-test-runs/{run_id}/report")).json()
            assert report["agent"]["run_id"] == run_id
    finally:
        engine.dispose()
