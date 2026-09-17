"""确定性恢复：把「状态行 + 事件行」还原成领域会话。

恢复不使用 Pickle，也不从仓储改写领域对象的私有字段，而是
**用同一个种子从零重放玩家动作**，让领域层自己重新生成随机数与史莱姆反击：

1. 解析种子文本，构造 `CombatSession`，随机数生成器回到起点；
2. 只重放玩家事件（attack / potion）；史莱姆反击由领域逻辑在动作之后自行生成，
   绝不从数据库注入，否则随机数进度会被伪造的事件推进；
3. 比较「重放产生的完整事件序列」与「数据库事件序列」；
4. 比较重放后的最终状态与状态行。

任一步对不上都会抛出 `PersistenceInconsistentError`：调用方得到的是明确的
数据一致性错误，而不是被猜测、跳过或修补过的战斗。

本模块不导入 SQLAlchemy，也不执行任何 I/O，因此可以脱离数据库单独测试。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, fields

from gamepilot.domain.combat import CombatSession
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.domain.models import CombatEvent, GameSnapshot

from .errors import PersistenceConflictError, PersistenceInconsistentError

# 规范十进制文本：可带负号，无前导零、无正号、无空白。
# 领域与 API 的种子是无界 Python 整数，落库时统一用它序列化。
_SEED_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)")


def serialize_seed(seed: int) -> str:
    """把领域种子写成规范十进制文本。

    仅用于仓储落库；种子不参与任何数据库数值计算，因此这里不做范围限制。
    """
    text = str(seed)
    if not _SEED_PATTERN.fullmatch(text):
        raise ValueError(f"seed {seed!r} is not an integer")
    return text


def parse_seed(seed_text: str, *, session_id: str) -> int:
    """把存储的种子文本还原为 Python 整数；非规范文本视为数据不一致。"""
    if not _SEED_PATTERN.fullmatch(seed_text):
        raise PersistenceInconsistentError(
            f"session {session_id}: stored seed {seed_text!r} is not canonical decimal text"
        )
    return int(seed_text)


@dataclass(frozen=True)
class StoredSession:
    """状态行的领域无关副本，字段与 `game_sessions` 的列一一对应。"""

    session_id: str
    seed: str
    status: str
    turn: int
    player_hp: int
    player_max_hp: int
    potions: int
    slime_hp: int
    slime_max_hp: int

    @classmethod
    def from_snapshot(cls, snapshot: GameSnapshot) -> "StoredSession":
        """从领域快照提取待写入的状态投影。"""
        return cls(
            session_id=snapshot.session_id,
            seed=serialize_seed(snapshot.seed),
            status=snapshot.status.value,
            turn=snapshot.turn,
            player_hp=snapshot.player.hp,
            player_max_hp=snapshot.player.max_hp,
            potions=snapshot.player.potions,
            slime_hp=snapshot.slime.hp,
            slime_max_hp=snapshot.slime.max_hp,
        )


@dataclass(frozen=True)
class StoredEvent:
    """事件行的领域无关副本：会话内序号 + 领域事件。"""

    sequence: int
    event: CombatEvent


def ensure_contiguous_sequences(session_id: str, sequences: Sequence[int]) -> None:
    """校验事件序号从 1 开始且连续；否则视为存储被破坏。

    序号是恢复顺序的唯一依据，缺号或多号都意味着历史无法确定性重放。
    """
    if list(sequences) != list(range(1, len(sequences) + 1)):
        raise PersistenceInconsistentError(
            f"session {session_id}: stored event sequences are not contiguous from 1: "
            f"{list(sequences)}"
        )


def ensure_stored_prefix(
    session_id: str,
    *,
    stored: Sequence[CombatEvent],
    incoming: Sequence[CombatEvent],
) -> None:
    """校验既有事件是待保存事件的完整前缀，否则报告历史冲突。

    这条校验（配合状态行锁）阻止过期副本或分叉副本静默覆盖已保存历史：
    既拒绝缩短历史，也拒绝在任意位置改写既有事件。
    """
    if len(incoming) < len(stored):
        raise PersistenceConflictError(
            f"session {session_id}: refusing to shorten stored history "
            f"from {len(stored)} events to {len(incoming)}"
        )
    for index, stored_event in enumerate(stored, start=1):
        incoming_event = incoming[index - 1]
        if stored_event != incoming_event:
            raise PersistenceConflictError(
                f"session {session_id}: event #{index} diverges from stored history "
                f"(stored {stored_event.turn}/{stored_event.actor}/{stored_event.kind}, "
                f"incoming {incoming_event.turn}/{incoming_event.actor}/{incoming_event.kind})"
            )


def rebuild_session(state: StoredSession, events: Sequence[StoredEvent]) -> CombatSession:
    """用状态行与事件行确定性重建领域会话。

    返回的是独立的新对象，与数据库行或任何缓存对象都不共享状态。
    """
    session_id = state.session_id
    seed = parse_seed(state.seed, session_id=session_id)
    ensure_contiguous_sequences(session_id, [stored.sequence for stored in events])

    session = CombatSession(session_id=session_id, seed=seed)
    for stored in events:
        _replay_player_action(session, stored.event, session_id=session_id)

    snapshot = session.snapshot()
    stored_events = [stored.event for stored in events]
    _ensure_events_match(session_id, generated=list(snapshot.events), stored=stored_events)
    _ensure_state_matches(state, snapshot)
    return session


def _replay_player_action(session: CombatSession, event: CombatEvent, *, session_id: str) -> None:
    """重放单条事件；史莱姆反击不重放，由上一个玩家动作自动生成。"""
    if event.actor == "slime":
        if event.kind != "retaliate":
            raise PersistenceInconsistentError(
                f"session {session_id}: turn {event.turn} recorded unexpected "
                f"slime event kind {event.kind!r}"
            )
        return
    if event.actor != "player" or event.kind not in ("attack", "potion"):
        raise PersistenceInconsistentError(
            f"session {session_id}: turn {event.turn} recorded unsupported event "
            f"{event.actor}/{event.kind}"
        )

    try:
        if event.kind == "attack":
            session.attack()
        else:
            session.use_potion()
    except InvalidCombatAction as exc:
        # 领域层拒绝这个动作，说明存储的历史本身不可能发生。
        raise PersistenceInconsistentError(
            f"session {session_id}: turn {event.turn} action {event.kind} is not "
            f"replayable ({exc.code})"
        ) from exc


def _ensure_events_match(
    session_id: str, *, generated: Sequence[CombatEvent], stored: Sequence[CombatEvent]
) -> None:
    """逐条比较重放结果与存储事件：顺序、回合、行动方、类型、数值、双方 HP、药水。"""
    if len(generated) != len(stored):
        raise PersistenceInconsistentError(
            f"session {session_id}: stored history has {len(stored)} events but replay "
            f"produced {len(generated)}"
        )
    for index, (expected, actual) in enumerate(zip(generated, stored, strict=True), start=1):
        if expected != actual:
            raise PersistenceInconsistentError(
                f"session {session_id}: event #{index} differs from replay "
                f"(replayed {expected.turn}/{expected.actor}/{expected.kind} "
                f"value={expected.value} player_hp={expected.player_hp} "
                f"slime_hp={expected.slime_hp} potions={expected.potions!r}; "
                f"stored {actual.turn}/{actual.actor}/{actual.kind} "
                f"value={actual.value} player_hp={actual.player_hp} "
                f"slime_hp={actual.slime_hp} potions={actual.potions!r})"
            )


def _ensure_state_matches(state: StoredSession, snapshot: GameSnapshot) -> None:
    """比较重放后的最终状态与状态行。"""
    replayed = StoredSession.from_snapshot(snapshot)
    if replayed == state:
        return

    differences = ", ".join(
        f"{field.name}: stored={getattr(state, field.name)!r} "
        f"replayed={getattr(replayed, field.name)!r}"
        for field in fields(state)
        if getattr(state, field.name) != getattr(replayed, field.name)
    )
    raise PersistenceInconsistentError(
        f"session {state.session_id}: stored state differs from replay ({differences})"
    )
