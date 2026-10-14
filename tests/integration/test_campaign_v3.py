"""v3 手动和 Agent 连战共用 API、判定器与重放链路。"""

import httpx
import pytest

from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository


@pytest.mark.anyio
async def test_manual_session_advances_and_preserves_player() -> None:
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post("/api/v1/game-sessions", json={"seed": 4, "battle_count": 2})
        assert response.status_code == 201
        created = response.json()
        session_id = created["session_id"]
        assert (created["ruleset"], created["battle_count"], created["battle_index"]) == (
            "v3",
            2,
            1,
        )
        current = created
        while current["status"] == "active":
            response = await http.post(
                f"/api/v1/game-sessions/{session_id}/actions", json={"action": "attack"}
            )
            current = response.json()
        assert current["player"]["exp"] == 10
        won = current
        response = await http.post(
            f"/api/v1/game-sessions/{session_id}/actions", json={"action": "next_battle"}
        )
        assert response.status_code == 200
        current = response.json()
        assert (current["status"], current["battle_index"], current["enemy_type"]) == (
            "active",
            2,
            "goblin",
        )
        assert current["player"] == won["player"]
        assert (await http.get(f"/api/v1/game-sessions/{session_id}")).json() == current


@pytest.mark.anyio
async def test_manual_session_rejects_out_of_range_campaign_length() -> None:
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        for battle_count in (0, 7):
            response = await http.post(
                "/api/v1/game-sessions", json={"seed": 4, "battle_count": battle_count}
            )
            assert response.status_code == 422
            response = await http.post(
                "/api/v1/agent-test-runs", json={"seed": 4, "battle_count": battle_count}
            )
            assert response.status_code == 422


@pytest.mark.anyio
async def test_six_battle_offline_agent_beats_leveled_troll_and_replays(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post("/api/v1/agent-test-runs", json={"seed": 9, "battle_count": 6})
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        run_id = started.json()["run_id"]
        run = (await http.get(f"/api/v1/agent-test-runs/{run_id}")).json()
        assert run["result"]["verdict"] == "pass"
        assert run["result"]["replay_outcome"] == "match"
        assert run["model_calls"] <= 50
        assert run["actions_attempted"] <= 50
        actions = [item for item in run["steps"] if item["kind"] == "action"]
        assert sum(item["action"] == "next_battle" for item in actions) == 5
        assert any(
            item["before"]["enemy_type"] == "troll" and item["before"]["player"]["level"] > 1
            for item in actions
        )
        assert actions[-1]["after"]["battle_index"] == 6


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("profile", "seed", "rule"),
    [
        ("loot_anomaly", 3, "R-EXP-REWARD"),
        ("loot_anomaly", 4, "R-EXP-REWARD"),
        ("loot_anomaly", 5, "R-EXP-REWARD"),
        ("level_anomaly", 9, "R-LEVEL-UP"),
        ("level_anomaly", 12, "R-LEVEL-UP"),
    ],
)
async def test_new_faults_are_detected_and_replay(
    profile: str, seed: int, rule: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        started = await http.post(
            "/api/v1/agent-test-runs", json={"seed": seed, "profile": profile}
        )
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        run = (await http.get(f"/api/v1/agent-test-runs/{started.json()['run_id']}")).json()
        assert run["result"]["verdict"] == "defect_detected"
        assert rule in run["result"]["target_rules_hit"]
        assert run["result"]["replay_outcome"] == "match"
