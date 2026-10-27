"""公开预览的标签页归属、关闭清理与丢失通知兜底。"""

import asyncio
from uuid import uuid4

import httpx
import pytest

from gamepilot.main import create_app
from gamepilot.preview_visitors import PreviewVisitorManager
from gamepilot.repositories.memory import InMemorySessionRepository


async def _open(http: httpx.AsyncClient, visitor_id: str, page_id: str) -> dict[str, str]:
    response = await http.post(
        "/api/v1/preview-visitor/open",
        json={"visitor_id": visitor_id, "page_id": page_id},
    )
    assert response.status_code == 200
    assert response.json() == {"preview_enabled": True}
    return {"X-GamePilot-Visitor": visitor_id, "X-GamePilot-Page": page_id}


@pytest.mark.anyio
async def test_preview_visitors_cannot_read_each_others_game_or_agent_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("GAMEPILOT_PUBLIC_PREVIEW", "true")
    monkeypatch.setattr("gamepilot.web_runs.WEB_REPORT_ROOT", tmp_path)
    app = create_app(InMemorySessionRepository())
    app.state.preview_visitors = PreviewVisitorManager(
        app.state.agent_run_manager, grace_seconds=0.01
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        assert (
            await http.post(
                "/api/v1/preview-visitor/open",
                json={"visitor_id": "invalid", "page_id": str(uuid4())},
            )
        ).status_code == 422
        assert (await http.post("/api/v1/game-sessions", json={"seed": 14})).status_code == 403
        first_id, second_id = str(uuid4()), str(uuid4())
        first = await _open(http, first_id, str(uuid4()))
        second = await _open(http, second_id, str(uuid4()))
        first_game = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=first)
        second_game = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=second)
        assert first_game.status_code == second_game.status_code == 201
        first_session_id = first_game.json()["session_id"]
        second_session_id = second_game.json()["session_id"]
        first_path = f"/api/v1/game-sessions/{first_session_id}"
        second_path = f"/api/v1/game-sessions/{second_session_id}"
        assert (await http.get(first_path, headers=first)).status_code == 200
        assert (await http.get(first_path, headers=second)).status_code == 404
        assert (await http.get(second_path, headers=first)).status_code == 404
        assert (
            await http.post(f"{first_path}/actions", json={"action": "attack"}, headers=second)
        ).status_code == 404

        started = await http.post(
            "/api/v1/agent-test-runs",
            json={"seed": 14, "profile": "potion_overheal"},
            headers=first,
        )
        assert started.status_code == 202
        await app.state.agent_run_manager._task
        run_id = started.json()["run_id"]
        run_path = f"/api/v1/agent-test-runs/{run_id}"
        assert (await http.get(run_path, headers=first)).status_code == 200
        assert (await http.get(f"{run_path}/report", headers=first)).status_code == 200
        assert (await http.get(run_path, headers=second)).status_code == 404
        assert (await http.get(f"{run_path}/report", headers=second)).status_code == 404
        assert (tmp_path / run_id).is_dir()

        closed = await http.post(
            "/api/v1/preview-visitor/close",
            json={"visitor_id": first_id, "page_id": first["X-GamePilot-Page"]},
        )
        assert closed.status_code == 204
        await app.state.preview_visitors._visitors[first_id].close_task
        assert (await http.get(first_path, headers=first)).status_code == 403
        assert (await http.get(second_path, headers=second)).status_code == 200
        assert (tmp_path / run_id).exists() is False
        assert run_id not in app.state.agent_run_manager._records


@pytest.mark.anyio
async def test_refresh_cancels_old_page_close_and_stale_close_cannot_remove_new_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAMEPILOT_PUBLIC_PREVIEW", "true")
    app = create_app(InMemorySessionRepository())
    app.state.preview_visitors = PreviewVisitorManager(
        app.state.agent_run_manager, grace_seconds=0.01
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        visitor_id = str(uuid4())
        old = await _open(http, visitor_id, str(uuid4()))
        created = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=old)
        path = f"/api/v1/game-sessions/{created.json()['session_id']}"
        await http.post(
            "/api/v1/preview-visitor/close",
            json={"visitor_id": visitor_id, "page_id": old["X-GamePilot-Page"]},
        )
        previous_close = app.state.preview_visitors._visitors[visitor_id].close_task
        new = await _open(http, visitor_id, str(uuid4()))
        await asyncio.gather(previous_close, return_exceptions=True)
        assert (await http.get(path, headers=new)).status_code == 200
        assert (await http.get(path, headers=old)).status_code == 403
        await http.post(
            "/api/v1/preview-visitor/close",
            json={"visitor_id": visitor_id, "page_id": old["X-GamePilot-Page"]},
        )
        assert (await http.get(path, headers=new)).status_code == 200


@pytest.mark.anyio
async def test_idle_sweep_cleans_only_visitor_without_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAMEPILOT_PUBLIC_PREVIEW", "true")
    app = create_app(InMemorySessionRepository())
    now = [0.0]
    app.state.preview_visitors = PreviewVisitorManager(
        app.state.agent_run_manager, idle_seconds=30, clock=lambda: now[0]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        first_id, second_id = str(uuid4()), str(uuid4())
        first = await _open(http, first_id, str(uuid4()))
        second = await _open(http, second_id, str(uuid4()))
        first_game = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=first)
        second_game = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=second)
        now[0] = 20.0
        assert (await http.post("/api/v1/preview-visitor/touch", headers=second)).status_code == 204
        now[0] = 31.0
        await app.state.preview_visitors.sweep_once()
        assert (
            await http.get(
                f"/api/v1/game-sessions/{first_game.json()['session_id']}", headers=first
            )
        ).status_code == 403
        assert (
            await http.get(
                f"/api/v1/game-sessions/{second_game.json()['session_id']}", headers=second
            )
        ).status_code == 200


@pytest.mark.anyio
async def test_preview_lifespan_runs_idle_cleanup_without_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAMEPILOT_PUBLIC_PREVIEW", "true")
    app = create_app(InMemorySessionRepository())
    manager = app.state.preview_visitors
    manager._idle_seconds = 0.01
    manager._sweep_seconds = 0.01
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            visitor_id = str(uuid4())
            headers = await _open(http, visitor_id, str(uuid4()))
            created = await http.post("/api/v1/game-sessions", json={"seed": 14}, headers=headers)
            assert created.status_code == 201
            await asyncio.sleep(0.1)
            assert visitor_id not in manager._visitors
    assert manager._sweep_task is None


@pytest.mark.anyio
async def test_closing_tab_cancels_its_running_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAMEPILOT_PUBLIC_PREVIEW", "true")
    started = asyncio.Event()

    async def hold(_manager, _record) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("gamepilot.web_runs.AgentWebRunManager._run", hold)
    app = create_app(InMemorySessionRepository())
    app.state.preview_visitors = PreviewVisitorManager(
        app.state.agent_run_manager, grace_seconds=0.01
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        visitor_id = str(uuid4())
        headers = await _open(http, visitor_id, str(uuid4()))
        response = await http.post("/api/v1/agent-test-runs", json={"seed": 14}, headers=headers)
        assert response.status_code == 202
        await asyncio.wait_for(started.wait(), timeout=2)
        await http.post(
            "/api/v1/preview-visitor/close",
            json={"visitor_id": visitor_id, "page_id": headers["X-GamePilot-Page"]},
        )
        await app.state.preview_visitors._visitors[visitor_id].close_task
        assert app.state.agent_run_manager._task.cancelled()
        assert response.json()["run_id"] not in app.state.agent_run_manager._records
