"""API 集成测试：通过 httpx 的 ASGITransport 驱动完整路由与异常处理。

httpx 0.28 起 ASGITransport 移除了同步 handle_request，只支持 AsyncClient，
因此测试基于 anyio 的 pytest 插件在 asyncio 后端运行（pytest.mark.anyio）。

每个测试使用独立的应用实例与内存仓储，测试之间互不影响；
不使用真实网络，断言不依赖随机结果的绝对数值。
"""

from collections.abc import AsyncIterator

import httpx
import pytest

from gamepilot.domain.combat import PLAYER_DAMAGE_RANGE, SLIME_DAMAGE_RANGE
from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(InMemorySessionRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as test_client:
        yield test_client


async def create_session(client: httpx.AsyncClient, seed: int = 42) -> dict:
    response = await client.post("/api/v1/game-sessions", json={"seed": seed})
    assert response.status_code == 201
    return response.json()


async def perform_action(client: httpx.AsyncClient, session_id: str, action: str) -> dict:
    response = await client.post(
        f"/api/v1/game-sessions/{session_id}/actions", json={"action": action}
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_health_check(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_create_session_returns_201_with_full_snapshot(
    client: httpx.AsyncClient,
) -> None:
    body = await create_session(client, seed=42)
    assert set(body) == {
        "session_id",
        "seed",
        "status",
        "turn",
        "player",
        "slime",
        "events",
    }
    assert body["seed"] == 42
    assert body["status"] == "active"
    assert body["turn"] == 0
    assert body["player"] == {"hp": 100, "max_hp": 100, "potions": 2}
    assert body["slime"] == {"hp": 60, "max_hp": 60}
    assert body["events"] == []


@pytest.mark.anyio
async def test_create_session_without_seed_gets_replayable_seed(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/v1/game-sessions", json={})
    assert response.status_code == 201
    body = response.json()
    assert isinstance(body["seed"], int)
    assert body["status"] == "active"


@pytest.mark.anyio
async def test_get_existing_session_returns_same_snapshot(
    client: httpx.AsyncClient,
) -> None:
    created = await create_session(client)
    response = await client.get(f"/api/v1/game-sessions/{created['session_id']}")
    assert response.status_code == 200
    assert response.json() == created


@pytest.mark.anyio
async def test_get_missing_session_returns_404_with_stable_error(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/v1/game-sessions/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "code": "session_not_found",
        "message": "game session not found: does-not-exist",
    }


@pytest.mark.anyio
async def test_action_on_missing_session_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/game-sessions/does-not-exist/actions", json={"action": "attack"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "session_not_found"


@pytest.mark.anyio
async def test_attack_advances_turn_and_appends_structured_events(
    client: httpx.AsyncClient,
) -> None:
    session_id = (await create_session(client))["session_id"]
    body = await perform_action(client, session_id, "attack")
    assert body["turn"] == 1
    assert body["status"] == "active"
    assert len(body["events"]) == 2  # 玩家攻击 + 史莱姆反击
    player_event, slime_event = body["events"]
    assert player_event["actor"] == "player"
    assert player_event["kind"] == "attack"
    assert PLAYER_DAMAGE_RANGE[0] <= player_event["value"] <= PLAYER_DAMAGE_RANGE[1]
    assert slime_event["actor"] == "slime"
    assert slime_event["kind"] == "retaliate"
    assert SLIME_DAMAGE_RANGE[0] <= slime_event["value"] <= SLIME_DAMAGE_RANGE[1]
    assert body["player"]["hp"] == slime_event["player_hp"]
    assert body["slime"]["hp"] == player_event["slime_hp"]


@pytest.mark.anyio
async def test_use_potion_updates_hp_and_potions(client: httpx.AsyncClient) -> None:
    session_id = (await create_session(client))["session_id"]
    await perform_action(client, session_id, "attack")
    body = await perform_action(client, session_id, "use_potion")
    assert body["turn"] == 2
    # 注意：药水事件记录的是反击前的治疗结果，之后史莱姆仍会反击
    potion_event = body["events"][2]
    retaliate_event = body["events"][3]
    assert potion_event["kind"] == "potion"
    assert potion_event["player_hp"] == 100
    assert body["player"]["hp"] == 100 - retaliate_event["value"]
    assert body["player"]["potions"] == 1


@pytest.mark.anyio
async def test_invalid_action_value_returns_422(client: httpx.AsyncClient) -> None:
    session_id = (await create_session(client))["session_id"]
    response = await client.post(
        f"/api/v1/game-sessions/{session_id}/actions", json={"action": "fireball"}
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_potion_at_full_hp_returns_409_without_state_change(
    client: httpx.AsyncClient,
) -> None:
    session_id = (await create_session(client))["session_id"]
    response = await client.post(
        f"/api/v1/game-sessions/{session_id}/actions", json={"action": "use_potion"}
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "player_full_hp",
        "message": "player is already at full HP",
    }
    after = (await client.get(f"/api/v1/game-sessions/{session_id}")).json()
    assert after["turn"] == 0
    assert after["events"] == []
    assert after["player"] == {"hp": 100, "max_hp": 100, "potions": 2}


@pytest.mark.anyio
async def test_action_after_battle_over_returns_409(client: httpx.AsyncClient) -> None:
    session_id = (await create_session(client))["session_id"]
    status = "active"
    for _ in range(20):  # 玩家至多 4 回合获胜，设上限防死循环
        if status != "active":
            break
        status = (await perform_action(client, session_id, "attack"))["status"]
    assert status == "won"
    response = await client.post(
        f"/api/v1/game-sessions/{session_id}/actions", json={"action": "attack"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "battle_not_active"


@pytest.mark.anyio
async def test_event_structure_is_stable(client: httpx.AsyncClient) -> None:
    session_id = (await create_session(client))["session_id"]
    body = await perform_action(client, session_id, "attack")
    for event in body["events"]:
        assert set(event) == {
            "turn",
            "actor",
            "kind",
            "value",
            "player_hp",
            "slime_hp",
            "potions",
        }
        assert event["turn"] == body["turn"]


@pytest.mark.anyio
async def test_openapi_documents_domain_error_responses(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert "ErrorResponse" in spec["components"]["schemas"]

    session_path = spec["paths"]["/api/v1/game-sessions/{session_id}"]
    get_error_schema = session_path["get"]["responses"]["404"]["content"]["application/json"][
        "schema"
    ]
    assert get_error_schema["$ref"] == "#/components/schemas/ErrorResponse"

    action_responses = spec["paths"]["/api/v1/game-sessions/{session_id}/actions"]["post"][
        "responses"
    ]
    for status_code in ("404", "409"):
        error_schema = action_responses[status_code]["content"]["application/json"]["schema"]
        assert error_schema["$ref"] == "#/components/schemas/ErrorResponse"
