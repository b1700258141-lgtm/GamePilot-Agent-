"""PostgreSQL 仓储实现：同步 SQLAlchemy 2.x + psycopg 3。

职责边界：

- 只做「领域会话 ↔ 关系行」的读写与事务控制，
  不包含任何战斗规则、随机数或 HTTP 逻辑；
- 每次操作使用独立的 ORM Session，操作结束即归还连接，
  仓储本身不持有跨请求的 Session；
- 一次 save 的写入范围就是一个事务：状态行与新事件一起提交、一起回滚；
- 加锁顺序固定为「先状态行、后事件行」，读写两侧保持一致，避免死锁。

恢复与校验逻辑放在 `gamepilot.persistence.recovery`，本模块只负责取数、
写数与事务边界。

部署边界：本阶段按「单写入方顺序操作」运行，不提供分布式请求幂等。
状态行锁 + 事件前缀校验用于阻止过期副本静默覆盖历史，
而不是为了支持多写入方并发。
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from psycopg import errors as psycopg_errors
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gamepilot.domain.combat import CombatSession
from gamepilot.domain.errors import SessionNotFoundError
from gamepilot.domain.models import CombatEvent
from gamepilot.persistence.errors import (
    PersistenceConflictError,
    PersistenceInconsistentError,
    PersistenceUnavailableError,
)
from gamepilot.persistence.models import CombatEventRow, GameSessionRow
from gamepilot.persistence.recovery import (
    StoredEvent,
    StoredSession,
    ensure_stored_prefix,
    rebuild_session,
)

# 可归因为「并发写入冲突」的唯一约束：
# 会话主键竞争（同一 id 被并发创建），以及同一会话内事件序号重复。
_CONFLICT_CONSTRAINTS = frozenset({"game_sessions_pkey", "uq_combat_events_session_sequence"})


class PostgresSessionRepository:
    """把会话状态与事件持久化到 PostgreSQL。

    构造时只接收 Session Factory，因此可以安全地在应用内长期持有：
    连接由 Factory 背后的 Engine 连接池管理，不随仓储实例泄漏。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(self, session: CombatSession) -> None:
        """在单个事务内写入状态行与新事件，提交成功后才返回。"""
        snapshot = session.snapshot()
        state = StoredSession.from_snapshot(snapshot)
        events = [
            StoredEvent(sequence=index, event=event)
            for index, event in enumerate(snapshot.events, start=1)
        ]

        try:
            with self._transaction() as db:
                self._write(db, state, events)
        except SQLAlchemyError as exc:
            mapped = _map_database_error(exc, operation="saving", session_id=state.session_id)
            if mapped is exc:
                raise
            raise mapped from exc

    def get(self, session_id: str) -> CombatSession:
        """返回独立重建的领域会话；数据不可恢复时不返回猜测结果。"""
        try:
            with self._transaction() as db:
                return self._read(db, session_id)
        except SQLAlchemyError as exc:
            mapped = _map_database_error(exc, operation="loading", session_id=session_id)
            if mapped is exc:
                raise
            raise mapped from exc

    # ------------------------------------------------------------------ 写

    def _write(self, db: Session, state: StoredSession, events: Sequence[StoredEvent]) -> None:
        # 无论新建还是更新都先锁状态行：并发写同一会话时在此串行化。
        row = db.get(GameSessionRow, state.session_id, with_for_update=True)
        if row is None:
            self._insert_new(db, state, events)
            return
        self._update_existing(db, row, state, events)

    def _insert_new(self, db: Session, state: StoredSession, events: Sequence[StoredEvent]) -> None:
        """新建会话：写入状态行与全部既有事件（通常为空）。"""
        db.add(GameSessionRow(**_state_values(state)))
        # 先 flush 状态行：事件行通过外键依赖它，
        # 同时让「状态已写、事件未写」的失败注入点稳定可预期。
        db.flush()
        self._insert_events(db, state.session_id, events)

    def _update_existing(
        self,
        db: Session,
        row: GameSessionRow,
        state: StoredSession,
        events: Sequence[StoredEvent],
    ) -> None:
        """更新会话：校验既有历史后追加新事件，绝不改写或删除既有事件。"""
        session_id = state.session_id
        stored_state = _stored_state(row)
        stored_events = self._load_events(db, session_id)

        # 在覆盖状态行或追加事件前，先确认数据库当前保存的投影与事件历史
        # 本身能够确定性重放。否则一次合法的新保存会掩盖既有损坏。
        rebuild_session(stored_state, stored_events)

        if stored_state.seed != state.seed:
            raise PersistenceConflictError(
                f"session {session_id}: stored seed differs from the seed being saved"
            )

        stored_history = [item.event for item in stored_events]
        incoming_history = [item.event for item in events]

        if len(stored_history) == len(incoming_history):
            if stored_history != incoming_history:
                raise PersistenceConflictError(
                    f"session {session_id}: history being saved diverges from stored "
                    f"history of the same length ({len(stored_history)} events)"
                )
            if stored_state != state:
                # 事件完全相同意味着状态是唯一确定的；状态行对不上说明存储被破坏。
                raise PersistenceInconsistentError(
                    f"session {session_id}: stored state row disagrees with the stored history"
                )
            # 内容完全一致：作为 no-op 成功，不产生重复记录。
            return

        ensure_stored_prefix(session_id, stored=stored_history, incoming=incoming_history)

        for column, value in _state_values(state).items():
            setattr(row, column, value)
        # 先把状态行更新 flush 出去，再追加事件：写入顺序确定，
        # flush 不是提交，两者仍属于同一个事务，失败时一起回滚。
        db.flush()
        self._insert_events(db, session_id, list(events)[len(stored_events) :])

    def _insert_events(self, db: Session, session_id: str, events: Sequence[StoredEvent]) -> None:
        """只追加事件行。序号取自待保存快照的顺序，不依赖数据库自增 id。"""
        db.add_all([_event_row(session_id, stored) for stored in events])

    # ------------------------------------------------------------------ 读

    def _read(self, db: Session, session_id: str) -> CombatSession:
        # 先对状态行加共享锁，再读事件行：写方必须先取同一状态行的排他锁，
        # 因此本事务存活期间事件不会被并发写入改动。
        # 这样即使默认隔离级别是 READ COMMITTED（两条 SELECT 各自取快照），
        # 也能得到「状态行与事件行互相一致」的视图，而不必提升隔离级别。
        row = db.get(GameSessionRow, session_id, with_for_update={"read": True})
        if row is None:
            raise SessionNotFoundError(session_id)
        events = self._load_events(db, session_id)
        return rebuild_session(_stored_state(row), events)

    def _load_events(self, db: Session, session_id: str) -> list[StoredEvent]:
        """按会话内序号升序读取事件；(session_id, sequence) 唯一约束已提供索引。"""
        rows = db.scalars(
            select(CombatEventRow)
            .where(CombatEventRow.session_id == session_id)
            .order_by(CombatEventRow.sequence)
        ).all()
        return [StoredEvent(sequence=row.sequence, event=_domain_event(row)) for row in rows]

    # -------------------------------------------------------------- 事务边界

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """一次仓储操作 = 一个事务；异常回滚，结束时归还连接。"""
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def _state_values(state: StoredSession) -> dict[str, object]:
    """状态行字段值；键名与 `game_sessions` 的列一一对应。"""
    return {
        "session_id": state.session_id,
        "seed": state.seed,
        "status": state.status,
        "turn": state.turn,
        "player_hp": state.player_hp,
        "player_max_hp": state.player_max_hp,
        "potions": state.potions,
        "slime_hp": state.slime_hp,
        "slime_max_hp": state.slime_max_hp,
    }


def _stored_state(row: GameSessionRow) -> StoredSession:
    return StoredSession(
        session_id=row.session_id,
        seed=row.seed,
        status=row.status,
        turn=row.turn,
        player_hp=row.player_hp,
        player_max_hp=row.player_max_hp,
        potions=row.potions,
        slime_hp=row.slime_hp,
        slime_max_hp=row.slime_max_hp,
    )


def _domain_event(row: CombatEventRow) -> CombatEvent:
    return CombatEvent(
        turn=row.turn,
        actor=row.actor,
        kind=row.kind,
        value=row.value,
        player_hp=row.player_hp,
        slime_hp=row.slime_hp,
        potions=row.potions,
    )


def _event_row(session_id: str, stored: StoredEvent) -> CombatEventRow:
    event = stored.event
    return CombatEventRow(
        session_id=session_id,
        sequence=stored.sequence,
        turn=event.turn,
        actor=event.actor,
        kind=event.kind,
        value=event.value,
        player_hp=event.player_hp,
        slime_hp=event.slime_hp,
        potions=event.potions,
    )


def _map_database_error(
    exc: SQLAlchemyError, *, operation: str, session_id: str
) -> SQLAlchemyError:
    """把数据库异常映射为稳定的仓储错误；无法归类时原样返回。

    只有确定的连接/可用性故障才映射为「暂时不可用」；
    编程错误等确定性故障保持原异常，由上层按通用内部错误处理。
    异常消息只包含操作与会话 id，不带连接串、凭据或原始 SQL。
    """
    if isinstance(exc, IntegrityError):
        if _unique_violation_constraint(exc) in _CONFLICT_CONSTRAINTS:
            return PersistenceConflictError(
                f"session {session_id}: concurrent write conflict while {operation}"
            )
        # 其他完整性错误不在这里吞掉，交给上层暴露为内部错误。
        return exc
    if isinstance(exc, (OperationalError, InterfaceError)) or getattr(
        exc, "connection_invalidated", False
    ):
        return PersistenceUnavailableError(
            f"database is unavailable while {operation} session {session_id}"
        )
    return exc


def _unique_violation_constraint(exc: IntegrityError) -> str | None:
    """返回唯一约束冲突的约束名；不是唯一约束冲突时返回 None。"""
    original = exc.orig
    if not isinstance(original, psycopg_errors.UniqueViolation):
        return None
    return original.diag.constraint_name
