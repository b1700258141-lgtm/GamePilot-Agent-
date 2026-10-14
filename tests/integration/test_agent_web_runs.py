"""网页入口在隔离靶场运行，证据、付费守卫与手动会话互不混淆。"""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')

from gamepilot.agent.provider import FakeProvider, scripted_responder
from gamepilot.agent.skills import DEFAULT_SKILL_ID, SkillStore
from gamepilot.lab.selection import activation_opportunity, select_faults
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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("profile", "expected_verdict", "expected_rule"),
    [
        ("normal", "pass", None),
        ("potion_overheal", "defect_detected", "R-POTION-CAP"),
        ("potion_not_consumed", "defect_detected", "R-POTION-DECREMENTS"),
        ("retaliate_after_death", "defect_detected", "R-NO-RETALIATE-ON-KILL"),
    ],
)
async def test_default_skill_runs_without_selected_goal_and_checks_each_profile(
    profile: str, expected_verdict: str, expected_rule: str | None, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post("/api/v1/agent-test-runs", json={"seed": 42, "profile": profile})
        assert started.status_code == 202
        assert started.json()["config"]["goal_id"] == "battle-end"
        assert started.json()["config"]["skill_id"] == DEFAULT_SKILL_ID
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert run["status"] == "completed"
        assert run["result"]["verdict"] == expected_verdict
        assert run["result"]["replay_outcome"] == "match"
        assert run["model_calls"] <= 20
        assert run["actions_attempted"] <= 20
        if expected_rule:
            assert expected_rule in run["result"]["target_rules_hit"]
        else:
            assert run["result"]["goal_met"] is True
            actions = [item["action"] for item in run["steps"] if item["kind"] == "action"]
            assert "use_potion" in actions
            assert actions[-1] == "attack"
            assert run["steps"][-1]["met"] is True


@pytest.mark.anyio
async def test_uploaded_skill_is_listed_and_cannot_use_offline_double(tmp_path) -> None:
    app = create_app(InMemorySessionRepository())
    app.state.agent_run_manager.skills = SkillStore(tmp_path / "skills")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        uploaded = await http.post(
            "/api/v1/agent-test-runs/skills",
            json={"name": "先攻击", "instructions": "# 先攻击\n每轮攻击直到战斗结束。"},
        )
        assert uploaded.status_code == 201
        skill_id = uploaded.json()["skill_id"]
        assert SkillStore(tmp_path / "skills").get(skill_id).name == "先攻击"
        skills = (await http.get("/api/v1/agent-test-runs/skills")).json()
        assert [item["skill_id"] for item in skills] == [DEFAULT_SKILL_ID, skill_id]
        rejected = await http.post(
            "/api/v1/agent-test-runs",
            json={"seed": 42, "skill_id": skill_id, "provider_mode": "offline"},
        )
        assert rejected.status_code == 409
        assert app.state.agent_run_manager._task is None
        invalid = await http.post(
            "/api/v1/agent-test-runs/skills", json={"name": "x", "instructions": " "}
        )
        assert invalid.status_code == 422


@pytest.mark.anyio
async def test_uploaded_skill_reaches_graph_without_paid_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path / "reports")
    monkeypatch.setattr("gamepilot.web_runs.real_key", lambda: ("test-key-env", "dummy"))
    monkeypatch.setattr(
        "gamepilot.agent.provider.AnthropicCompatibleProvider",
        lambda **_kwargs: FakeProvider(scripted_responder(["finish"])),
    )
    app = create_app(InMemorySessionRepository())
    app.state.agent_run_manager.skills = SkillStore(tmp_path / "skills")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        uploaded = await http.post(
            "/api/v1/agent-test-runs/skills",
            json={"name": "测试自定义", "instructions": "先攻击一次，再结束。"},
        )
        assert uploaded.status_code == 201
        started = await http.post(
            "/api/v1/agent-test-runs",
            json={
                "seed": 42,
                "profile": "potion_overheal",
                "skill_id": uploaded.json()["skill_id"],
                "provider_mode": "real",
                "paid_ack": True,
            },
        )
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        run_id = started.json()["run_id"]
        run = (await http.get(f"/api/v1/agent-test-runs/{run_id}")).json()
        report = (await http.get(f"/api/v1/agent-test-runs/{run_id}/report")).json()
        assert run["status"] == "completed"
        assert run["result"]["verdict"] == "not_triggered"
        assert run["result"]["provider_is_test_double"] is True
        assert report["skill"]["instructions"] == "先攻击一次，再结束。"
        assert "先攻击一次，再结束。" in report["agent"]["model_calls"][0]["messages"][2]["content"]


@pytest.mark.anyio
async def test_random_fault_selection_is_saved_and_replays_same_seed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    clean_seed = next(seed for seed in range(100) if not select_faults(seed).fault_ids)
    fault_seed = next(seed for seed in range(100) if select_faults(seed).fault_ids)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        for seed in (clean_seed, fault_seed, clean_seed):
            started = await http.post("/api/v1/agent-test-runs", json={"seed": seed})
            assert started.status_code == 202
            config = started.json()["config"]
            assert config["fault_selection_mode"] == "random"
            assert config["selected_faults"] == list(select_faults(seed).fault_ids)
            assert config["fault_schedule"] == {
                fault: activation_opportunity(seed, 1, fault) for fault in config["selected_faults"]
            }
            await app.state.agent_run_manager._task
            run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
            assert run["status"] == "completed"
            assert run["config"]["selected_faults"] == config["selected_faults"]
            assert run["result"]["replay_outcome"] == "match"


@pytest.mark.anyio
async def test_forced_faults_trigger_and_are_independently_detected(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    faults = ["potion_not_consumed", "potion_overheal"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        invalid = await http.post(
            "/api/v1/agent-test-runs", json={"seed": 14, "forced_faults": faults * 2}
        )
        assert invalid.status_code == 422
        conflict = await http.post(
            "/api/v1/agent-test-runs",
            json={"seed": 14, "profile": "normal", "forced_faults": faults},
        )
        assert conflict.status_code == 422
        started = await http.post(
            "/api/v1/agent-test-runs", json={"seed": 14, "forced_faults": faults}
        )
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        result = run["result"]
        assert run["status"] == "completed"
        assert run["config"]["fault_selection_mode"] == "forced"
        assert set(run["config"]["selected_faults"]) == set(faults)
        assert set(result["detected_faults"]) == set(faults)
        assert {item["fault_id"] for item in result["trigger_details"]} == set(faults)
        assert result["verdict"] == "defect_detected"
        assert result["replay_outcome"] == "match"
        report = (
            await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}/report")
        ).json()
        model_messages = json.dumps(report["agent"]["model_calls"][0]["messages"])
        assert all(fault not in model_messages for fault in faults)


@pytest.mark.anyio
async def test_forced_fault_is_delayed_until_second_battle_and_replays(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post(
            "/api/v1/agent-test-runs",
            json={"seed": 5, "battle_count": 2, "forced_faults": ["loot_anomaly"]},
        )
        assert started.status_code == 202
        assert started.json()["config"]["fault_schedule"] == {"loot_anomaly": 2}
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert run["status"] == "completed"
        assert run["result"]["verdict"] == "defect_detected"
        assert run["result"]["replay_outcome"] == "match"
        assert run["result"]["trigger_details"][0]["battle_index"] == 2
        assert run["result"]["trigger_details"][0]["opportunity"] == 2
        report = (
            await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}/report")
        ).json()
        assert report["lab"]["fault_schedule"] == {"loot_anomaly": 2}
        assert report["lab"]["trigger_details"] == run["result"]["trigger_details"]
        first_battle_checks = [
            step
            for step in run["steps"]
            if step["kind"] == "action" and step.get("after") and step["after"]["battle_index"] == 1
        ]
        assert all(
            check["status"] != "fail" for step in first_battle_checks for check in step["checks"]
        )


@pytest.mark.anyio
async def test_detected_fault_is_not_hidden_by_another_fault_scheduled_later(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post(
            "/api/v1/agent-test-runs",
            json={
                "seed": 42,
                "battle_count": 2,
                "forced_faults": ["potion_overheal", "potion_not_consumed"],
            },
        )
        assert started.status_code == 202
        assert started.json()["config"]["fault_schedule"] == {
            "potion_overheal": 1,
            "potion_not_consumed": 2,
        }
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert run["result"]["verdict"] == "defect_detected"
        assert run["result"]["detected_faults"] == ["potion_overheal"]
        assert run["result"]["untriggered_faults"] == ["potion_not_consumed"]
        assert run["result"]["replay_outcome"] == "match"


@pytest.mark.anyio
async def test_clean_campaign_ending_in_loss_still_meets_battle_end_goal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post("/api/v1/agent-test-runs", json={"seed": 8, "battle_count": 6})
        assert started.status_code == 202
        assert started.json()["config"]["selected_faults"] == []
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert run["status"] == "completed"
        assert run["result"]["verdict"] == "pass"
        assert run["result"]["goal_met"] is True
