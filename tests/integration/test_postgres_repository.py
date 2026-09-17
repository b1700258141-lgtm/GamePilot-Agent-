"""PostgreSQL 仓储集成测试：真实数据库上的持久化、确定性恢复与事务行为。

全部用例标记为 `postgres`，只在显式提供 TEST_DATABASE_URL 时运行，
并且只使用专用测试库（见 tests/integration/conftest.py）。

覆盖重点：
- 保存/读取往返，缺失会话的 404 语义；
- 极端种子（负数、超 BIGINT）无损往返；
- 应用重启（重建 Engine 与仓储）后状态与随机数进度一致；
- 重复保存不产生重复记录、时间戳语义正确；
- 过期/分叉副本不能静默覆盖已保存历史；
- 状态行已 flush、事件未提交时注入失败，状态与事件一起回滚；
- 篡改存储后显式报告数据不一致，且不自动修复；
- 通过 HTTP 的完整链路、错误映射与凭据不外泄；
- 应用自建 Engine 在关闭时释放，注入对象的资源归调用方。
"""

import uuid
from collections.abc import Callable, Sequence

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from gamepilot.domain.combat import CombatSession, create_combat_session
from gamepilot.domain.errors import InvalidCombatAction, SessionNotFoundError
from gamepilot.domain.models import BattleStatus, GameSnapshot
from gamepilot.main import create_app
from gamepilot.persistence.database import create_db_engine, create_session_factory
from gamepilot.persistence.errors import (
    PersistenceConflictError,
    PersistenceInconsistentError,
)
from gamepilot.persistence.models import GameSessionRow
from gamepilot.repositories.postgres import PostgresSessionRepository

pytestmark = pytest.mark.postgres

# 领域层已验证的确定性终局（见 tests/unit/test_combat.py）：三次攻击后分别获胜与阵亡。
WON_SEED = 42
LOST_SEED = 1252
# 极端种子：超 BIGINT 的整数本来就是 API 与领域支持的取值。
EXTREME_SEEDS = (0, -1, -123456789, 2**63 - 1, 2**63, 2**80, -(2**80) - 7, 10**40)
# 必然经过「攻击 → 反击 → 喝药 → 反击」且不会结束战斗的动作序列：
# 首次反击后玩家必然掉血，因此 use_potion 合法；两次反击最多 70 点伤害。
SAFE_PLAN = ("attack", "use_potion")


# --------------------------------------------------------------- 数据库读取工具


def _state_values(engine: Engine, session_id: str) -> dict[str, object] | None:
    """读取状态行的领域列（不含时间戳）。"""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT session_id, seed, status, turn, player_hp, player_max_hp, potions, "
                    "slime_hp, slime_max_hp FROM game_sessions WHERE session_id = :session_id"
                ),
                {"session_id": session_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _timestamps(engine: Engine, session_id: str) -> tuple[object, object] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT created_at, updated_at FROM game_sessions WHERE session_id = :session_id"),
            {"session_id": session_id},
        ).one_or_none()
    return (row[0], row[1]) if row is not None else None


def _events(engine: Engine, session_id: str) -> list[tuple[object, ...]]:
    """按会话内序号读取事件行；不涉及数据库自增 id 与时间戳。"""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT sequence, turn, actor, kind, value, player_hp, slime_hp, potions "
                "FROM combat_events WHERE session_id = :session_id ORDER BY sequence"
            ),
            {"session_id": session_id},
        ).all()
    return [tuple(row) for row in rows]


def _execute(engine: Engine, statement: str, session_id: str) -> None:
    """直接改写存储内容，用于模拟数据被破坏。"""
    with engine.begin() as connection:
        connection.execute(text(statement), {"session_id": session_id})


def _connection_count(engine: Engine) -> int:
    """当前测试库上由其他连接占用的后端数量（排除本次查询自己的连接）。"""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        ).scalar_one()


# ------------------------------------------------------------------- 领域工具


def _apply(session: CombatSession, action: str) -> None:
    if action == "attack":
        session.attack()
    else:
        session.use_potion()


def _reference_session(seed: int, actions: Sequence[str]) -> CombatSession:
    """用领域层重放同一串动作，作为「未被打断的基线」。"""
    session = CombatSession(session_id="baseline", seed=seed)
    for action in actions:
        _apply(session, action)
    return session


def _without_session_id(snapshot: GameSnapshot) -> dict[str, object]:
    """比较时排除会话 id：基线对象用的是本地占位 id。"""
    return snapshot.model_dump(exclude={"session_id"})


def _assert_no_credentials(response: Response, database_url: str) -> None:
    """响应不得包含口令、完整连接 URL 或原始 SQL。"""
    body = response.text
    url = make_url(database_url)
    if url.password:
        assert url.password not in body
    assert url.render_as_string(hide_password=False) not in body
    assert "postgresql+psycopg://" not in body
    assert "SELECT" not in body


# --------------------------------------------------------------- 保存与读取


def test_save_and_get_roundtrip(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
) -> None:
    session = create_combat_session(seed=20260917)
    tracked_session_ids.append(session.session_id)

    repository.save(session)
    restored = repository.get(session.session_id)

    assert restored is not session
    assert restored.snapshot() == session.snapshot()
    assert _state_values(engine, session.session_id) is not None
    assert _events(engine, session.session_id) == []


def test_get_unknown_session_reports_not_found(repository: PostgresSessionRepository) -> None:
    with pytest.raises(SessionNotFoundError):
        repository.get(f"missing-{uuid.uuid4().hex[:16]}")


@pytest.mark.parametrize("seed", EXTREME_SEEDS)
def test_seed_roundtrip_without_precision_loss(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
    seed: int,
) -> None:
    """负数与超 BIGINT 的种子必须无损往返，并且随机数进度一致。"""
    session = create_combat_session(seed=seed)
    tracked_session_ids.append(session.session_id)
    repository.save(session)

    stored = _state_values(engine, session.session_id)
    assert stored is not None
    assert stored["seed"] == str(seed)  # 规范十进制文本，不截断、不取模

    restored = repository.get(session.session_id)
    assert restored.seed == seed

    # 恢复后继续动作，必须与同种子的未中断基线完全一致。
    # 基线同样从第 0 回合起步，两边各自应用同一串动作。
    baseline = _reference_session(seed, ())
    for action in SAFE_PLAN:
        _apply(restored, action)
        _apply(baseline, action)
    assert _without_session_id(restored.snapshot()) == _without_session_id(baseline.snapshot())


def test_snapshot_survives_engine_and_repository_rebuild(
    repository: PostgresSessionRepository,
    make_engine: Callable[[], Engine],
    tracked_session_ids: list[str],
) -> None:
    """多轮保存后重建 Engine 与仓储（模拟应用重启），快照仍然一致。"""
    session = create_combat_session(seed=424242)
    tracked_session_ids.append(session.session_id)

    repository.save(session)
    for action in SAFE_PLAN:
        _apply(session, action)
        repository.save(session)

    rebuilt = PostgresSessionRepository(create_session_factory(make_engine()))
    assert rebuilt.get(session.session_id).snapshot() == session.snapshot()


def test_recovered_session_continues_like_an_uninterrupted_baseline(
    repository: PostgresSessionRepository,
    tracked_session_ids: list[str],
) -> None:
    """恢复后继续动作与未中断基线逐回合一致（只看瞬时快照无法验证随机数进度）。"""
    seed = 987654321
    session = create_combat_session(seed=seed)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    for action in SAFE_PLAN:
        _apply(session, action)
        repository.save(session)

    baseline = _reference_session(seed, SAFE_PLAN)
    restored = repository.get(session.session_id)
    assert _without_session_id(restored.snapshot()) == _without_session_id(baseline.snapshot())

    compared = 0
    for action in ("attack", "use_potion", "attack", "attack"):
        if baseline.snapshot().status is not BattleStatus.ACTIVE:
            break
        _apply(baseline, action)
        _apply(restored, action)
        compared += 1
        assert _without_session_id(restored.snapshot()) == _without_session_id(baseline.snapshot())
    assert compared >= 1


@pytest.mark.parametrize(
    ("seed", "status"),
    [(WON_SEED, BattleStatus.WON), (LOST_SEED, BattleStatus.LOST)],
)
def test_terminal_session_recovers_and_rejects_further_actions(
    repository: PostgresSessionRepository,
    tracked_session_ids: list[str],
    seed: int,
    status: BattleStatus,
) -> None:
    """胜/负终局能完整恢复，恢复后继续动作仍被领域层拒绝。"""
    session = create_combat_session(seed=seed)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    for _ in range(20):
        if session.snapshot().status is not BattleStatus.ACTIVE:
            break
        session.attack()
        repository.save(session)

    assert session.snapshot().status is status
    restored = repository.get(session.session_id)
    assert restored.snapshot() == session.snapshot()

    before = restored.snapshot()
    with pytest.raises(InvalidCombatAction) as exc_info:
        restored.attack()
    assert exc_info.value.code == "battle_not_active"
    assert restored.snapshot() == before


# ----------------------------------------------------------- 重复保存与时间戳


def test_repeated_save_adds_no_duplicate_events_and_updates_timestamps(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
) -> None:
    session = create_combat_session(seed=555)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    created_at, updated_at_initial = _timestamps(engine, session.session_id)

    _apply(session, "attack")
    repository.save(session)
    events_after_update = _events(engine, session.session_id)
    _, updated_at_after_update = _timestamps(engine, session.session_id)

    # 重复保存同一快照：不产生重复事件，也不再触碰 updated_at。
    repository.save(session)
    repository.save(session)
    assert _events(engine, session.session_id) == events_after_update
    assert len(events_after_update) == len(session.snapshot().events)

    created_at_final, updated_at_final = _timestamps(engine, session.session_id)
    assert created_at_final == created_at  # created_at 保持不变
    assert updated_at_after_update > updated_at_initial  # 真正推进时 updated_at 前进
    assert updated_at_final == updated_at_after_update  # no-op 不再推进


# --------------------------------------------------------- 过期副本与分叉历史


def test_stale_copy_cannot_shorten_saved_history(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
) -> None:
    """先读到的副本不得在后来的写入之后覆盖已保存历史。"""
    session = create_combat_session(seed=777)
    tracked_session_ids.append(session.session_id)
    repository.save(session)

    stale = repository.get(session.session_id)  # 过期副本：此时历史为空
    _apply(session, "attack")
    repository.save(session)

    events_before = _events(engine, session.session_id)
    state_before = _state_values(engine, session.session_id)
    assert len(events_before) == 2  # 玩家攻击 + 史莱姆反击

    with pytest.raises(PersistenceConflictError):
        repository.save(stale)

    assert _events(engine, session.session_id) == events_before
    assert _state_values(engine, session.session_id) == state_before


def test_divergent_copy_cannot_overwrite_saved_history(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
) -> None:
    """两个独立读取的副本分别推进：先写入的胜出，后写入的分叉被拒绝。

    交错顺序完全确定（不依赖 sleep）：两次攻击先落库，
    随后副本 A 喝药、副本 B 攻击，两者会在同一序号上给出不同事件。
    """
    session = create_combat_session(seed=24680)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    for _ in range(2):  # 先造成伤害，使「喝药」在领域层合法
        _apply(session, "attack")
        repository.save(session)

    first = repository.get(session.session_id)
    second = repository.get(session.session_id)
    assert first is not second and first is not session
    assert first.snapshot() == second.snapshot()

    _apply(first, "use_potion")
    repository.save(first)
    events_before = _events(engine, session.session_id)
    state_before = _state_values(engine, session.session_id)

    _apply(second, "attack")  # 与已保存历史分叉（同一序号上的事件不同）
    with pytest.raises(PersistenceConflictError):
        repository.save(second)

    assert _events(engine, session.session_id) == events_before
    assert _state_values(engine, session.session_id) == state_before


# ------------------------------------------------------------------- 原子性


def test_failure_after_state_flush_rolls_back_new_session(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新建路径：状态行已 flush、事件未写入时失败，两者一起回滚。"""
    session = create_combat_session(seed=8080)
    tracked_session_ids.append(session.session_id)
    observed: list[dict[str, object]] = []

    def _fail_after_state_flush(db: Session, session_id: str, events: object) -> None:
        observed.append(
            {
                # 同一个事务里查得到刚 flush 的状态行：它已经写进数据库事务，
                # 不再是待写对象（flush 不是提交，所以只对本事务可见）。
                "visible_in_own_transaction": db.get(GameSessionRow, session_id) is not None,
                # 另一条连接看不到它：证明事务确实尚未提交。
                "visible_from_other_connection": _state_values(engine, session_id),
            }
        )
        raise RuntimeError("injected failure after state row flush")

    monkeypatch.setattr(repository, "_insert_events", _fail_after_state_flush)
    with pytest.raises(RuntimeError, match="injected failure"):
        repository.save(session)

    assert observed[0]["visible_in_own_transaction"] is True
    assert observed[0]["visible_from_other_connection"] is None

    # 回滚后不留半写状态。
    assert _state_values(engine, session.session_id) is None
    assert _events(engine, session.session_id) == []


def test_failure_after_state_flush_rolls_back_state_update(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更新路径：状态更新已 flush、事件未写入时失败，两者一起回滚。"""
    session = create_combat_session(seed=9090)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    _apply(session, "attack")

    state_before = _state_values(engine, session.session_id)
    observed: list[dict[str, object]] = []

    def _fail_after_state_flush(db: Session, session_id: str, events: object) -> None:
        row = db.get(GameSessionRow, session_id)
        observed.append(
            {
                "in_transaction_turn": row.turn if row is not None else None,
                "visible_from_other_connection": _state_values(engine, session_id),
            }
        )
        raise RuntimeError("injected failure after state row flush")

    monkeypatch.setattr(repository, "_insert_events", _fail_after_state_flush)
    with pytest.raises(RuntimeError, match="injected failure"):
        repository.save(session)

    assert state_before is not None
    assert observed[0]["in_transaction_turn"] == state_before["turn"] + 1
    assert observed[0]["visible_from_other_connection"] == state_before

    assert _state_values(engine, session.session_id) == state_before
    assert _events(engine, session.session_id) == []


# -------------------------------------------------------------- 篡改与一致性


@pytest.mark.parametrize("tamper", ["event_value", "event_sequence", "state_row"])
def test_tampered_storage_is_reported_and_not_repaired(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
    tamper: str,
) -> None:
    session = create_combat_session(seed=13579)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    for action in SAFE_PLAN:
        _apply(session, action)
        repository.save(session)

    statements = {
        "event_value": (
            "UPDATE combat_events SET value = value + 1 "
            "WHERE session_id = :session_id AND sequence = 1"
        ),
        "event_sequence": (
            "UPDATE combat_events SET sequence = 99 WHERE session_id = :session_id AND sequence = 1"
        ),
        "state_row": "UPDATE game_sessions SET slime_hp = 1 WHERE session_id = :session_id",
    }
    _execute(engine, statements[tamper], session.session_id)
    events_after_tamper = _events(engine, session.session_id)
    state_after_tamper = _state_values(engine, session.session_id)

    with pytest.raises(PersistenceInconsistentError):
        repository.get(session.session_id)

    # 不自动修复、不跳过、不猜测：被改动的内容原样保留。
    assert _events(engine, session.session_id) == events_after_tamper
    assert _state_values(engine, session.session_id) == state_after_tamper


def test_save_rejects_tampered_stored_state_before_appending(
    repository: PostgresSessionRepository,
    engine: Engine,
    tracked_session_ids: list[str],
) -> None:
    """保留对象继续推进时，也不能用新快照静默覆盖已损坏的状态行。"""
    session = create_combat_session(seed=WON_SEED)
    tracked_session_ids.append(session.session_id)
    repository.save(session)
    session.attack()
    repository.save(session)

    _execute(
        engine,
        "UPDATE game_sessions SET slime_hp = 1 WHERE session_id = :session_id",
        session.session_id,
    )
    state_after_tamper = _state_values(engine, session.session_id)
    events_after_tamper = _events(engine, session.session_id)
    timestamps_after_tamper = _timestamps(engine, session.session_id)

    # 使用篡改前保留的合法领域对象继续动作，模拟 get/save 之间存储被外部改动。
    session.attack()
    with pytest.raises(PersistenceInconsistentError):
        repository.save(session)

    assert _state_values(engine, session.session_id) == state_after_tamper
    assert _events(engine, session.session_id) == events_after_tamper
    assert _timestamps(engine, session.session_id) == timestamps_after_tamper


# --------------------------------------------------------------- HTTP 链路


def test_api_session_survives_app_restart(
    postgres_app_config: None,
    tracked_session_ids: list[str],
) -> None:
    """创建 → 动作 → 关闭应用 A → 新应用 B 查询并继续动作，全程走 HTTP。"""
    seed = 24680
    app_a = create_app()
    with TestClient(app_a) as client_a:
        created = client_a.post("/api/v1/game-sessions", json={"seed": seed})
        assert created.status_code == 201
        assert created.json()["seed"] == seed
        session_id = created.json()["session_id"]
        tracked_session_ids.append(session_id)

        for action in SAFE_PLAN:
            response = client_a.post(
                f"/api/v1/game-sessions/{session_id}/actions", json={"action": action}
            )
            assert response.status_code == 200
        before_restart = response.json()

    baseline = _reference_session(seed, SAFE_PLAN)

    app_b = create_app()
    with TestClient(app_b) as client_b:
        restored = client_b.get(f"/api/v1/game-sessions/{session_id}")
        assert restored.status_code == 200
        assert restored.json() == before_restart
        assert _without_session_id(
            GameSnapshot.model_validate(restored.json())
        ) == _without_session_id(baseline.snapshot())

        compared = 0
        for action in ("attack", "use_potion", "attack"):
            if baseline.snapshot().status is not BattleStatus.ACTIVE:
                break
            _apply(baseline, action)
            response = client_b.post(
                f"/api/v1/game-sessions/{session_id}/actions", json={"action": action}
            )
            assert response.status_code == 200
            compared += 1
            assert _without_session_id(
                GameSnapshot.model_validate(response.json())
            ) == _without_session_id(baseline.snapshot())
        assert compared >= 1


def test_api_error_mapping_and_credential_hygiene(
    postgres_app_config: None,
    test_database_url: str,
    engine: Engine,
    tracked_session_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404/409/503/500 的映射，以及「注入故障时响应不泄漏凭据」。"""
    app = create_app()
    with TestClient(app) as client:
        # 404：会话不存在。
        missing = client.get(f"/api/v1/game-sessions/missing-{uuid.uuid4().hex[:12]}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "session_not_found"

        # 500：存储被篡改 → 明确报告数据不一致，而不是伪装成「会话不存在」。
        tampered_id = _create_session(client, tracked_session_ids, seed=13579)
        client.post(f"/api/v1/game-sessions/{tampered_id}/actions", json={"action": "attack"})
        _execute(
            engine,
            "UPDATE combat_events SET value = value + 1 WHERE session_id = :session_id",
            tampered_id,
        )
        inconsistent = client.get(f"/api/v1/game-sessions/{tampered_id}")
        assert inconsistent.status_code == 500
        assert inconsistent.json()["code"] == "persistence_inconsistent"
        _assert_no_credentials(inconsistent, test_database_url)

        # 409（领域）：终局之后继续动作。
        finished_id = _create_session(client, tracked_session_ids, seed=WON_SEED)
        for _ in range(20):
            response = client.post(
                f"/api/v1/game-sessions/{finished_id}/actions", json={"action": "attack"}
            )
            assert response.status_code == 200
            if response.json()["status"] != BattleStatus.ACTIVE.value:
                break
        conflict = client.post(
            f"/api/v1/game-sessions/{finished_id}/actions", json={"action": "attack"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "battle_not_active"

        # 409（持久化冲突）：路由拿到的对象与已存会话不是同一场战斗。
        divergent_id = _create_session(client, tracked_session_ids, seed=111)
        divergent = CombatSession(session_id=divergent_id, seed=222)
        monkeypatch.setattr(app.state.repository, "get", lambda _session_id: divergent)
        rejected = client.post(
            f"/api/v1/game-sessions/{divergent_id}/actions", json={"action": "attack"}
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "persistence_conflict"
        _assert_no_credentials(rejected, test_database_url)

        # 已存会话没有被改写。
        stored = _state_values(engine, divergent_id)
        assert stored is not None
        assert stored["seed"] == "111"
        assert stored["status"] == BattleStatus.ACTIVE.value
        monkeypatch.undo()

        # 503：写入路径遇到连接故障，并且不自动重试。
        unavailable_id = _create_session(client, tracked_session_ids, seed=333)
        attempts: list[int] = []

        def _fail_write(*_args: object, **_kwargs: object) -> None:
            attempts.append(1)
            raise OperationalError("INSERT INTO combat_events ...", {}, Exception("reset"))

        monkeypatch.setattr(app.state.repository, "_insert_events", _fail_write)
        unavailable = client.post(
            f"/api/v1/game-sessions/{unavailable_id}/actions", json={"action": "attack"}
        )
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "persistence_unavailable"
        assert attempts == [1]  # 写入失败不自动重试
        _assert_no_credentials(unavailable, test_database_url)

        # 失败后仍是完整的旧状态：没有半写状态被缓存或落库。
        monkeypatch.undo()
        assert _events(engine, unavailable_id) == []
        after_failure = client.get(f"/api/v1/game-sessions/{unavailable_id}")
        assert after_failure.status_code == 200
        assert after_failure.json()["turn"] == 0


def test_app_engine_is_released_on_shutdown(
    postgres_app_config: None,
    test_database_url: str,
    engine: Engine,
) -> None:
    """应用自建的 Engine 由 lifespan 释放；注入对象的资源由调用方释放。"""
    baseline = _connection_count(engine)

    app = create_app()
    with TestClient(app):
        during = _connection_count(engine)
    after = _connection_count(engine)
    assert during > baseline  # 应用自己的连接池在运行期间占用连接
    assert after == baseline  # 关闭后全部释放

    # 注入仓储时，应用既不创建也不释放连接资源。
    caller_engine = create_db_engine(test_database_url)
    try:
        with caller_engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
        warmed = _connection_count(engine)
        assert warmed > baseline

        injected_app = create_app(
            repository=PostgresSessionRepository(create_session_factory(caller_engine))
        )
        with TestClient(injected_app) as client:
            assert client.get("/health").json() == {"status": "ok"}
        assert _connection_count(engine) == warmed  # 调用方的连接没有被应用释放

        caller_engine.dispose()
        assert _connection_count(engine) == baseline
    finally:
        caller_engine.dispose()


def _create_session(client: TestClient, tracked_session_ids: list[str], *, seed: int) -> str:
    """通过 HTTP 创建会话并登记 id，便于测试结束后精确清理。"""
    response = client.post("/api/v1/game-sessions", json={"seed": seed})
    assert response.status_code == 201
    session_id = response.json()["session_id"]
    tracked_session_ids.append(session_id)
    return session_id
