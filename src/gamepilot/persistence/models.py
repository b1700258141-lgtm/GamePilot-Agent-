"""ORM 表映射：只描述关系表结构，不包含任何游戏规则。

与领域模型的边界（重要）：

- 这些类不是 `gamepilot.domain` 中 `CombatSession`、`GameSnapshot`、
  `CombatEvent` 的替代或子类，API Schema 同样不受影响；
- 伤害计算、胜负判断、随机数等规则只存在于领域层，ORM 类只做列映射；
- 领域层不导入本模块，依赖方向始终是「仓储实现 → ORM」，
  因此领域逻辑可以在没有数据库的情况下单独测试。

索引说明：`combat_events` 上的 `(session_id, sequence)` 唯一约束在
PostgreSQL 中会隐式创建同列 btree 索引，已能支撑「按会话按序号顺序读取事件」，
因此不再额外建立重复索引。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

# 与领域枚举保持一致的取值集合，通过检查约束在数据库层兜底。
SESSION_STATUSES = ("active", "won", "lost")
EVENT_ACTORS = ("player", "slime")
EVENT_KINDS = ("attack", "potion", "retaliate")

# 用于生成 IN (...) 检查约束的取值列表；顺序固定，保证迁移可复现。
_STATUS_LIST = ", ".join(f"'{value}'" for value in SESSION_STATUSES)
_ACTOR_LIST = ", ".join(f"'{value}'" for value in EVENT_ACTORS)
_KIND_LIST = ", ".join(f"'{value}'" for value in EVENT_KINDS)


class GameSessionRow(Base):
    """`game_sessions`：战斗会话的当前投影。

    只保存恢复战斗所需的确定性事实：种子、当前状态投影与时间戳。
    随机数生成器位置不落库，由事件序列重放恢复（见第二阶段方案第 5 节）。
    """

    __tablename__ = "game_sessions"

    session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    player_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    player_max_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    potions: Mapped[int] = mapped_column(Integer, nullable=False)
    slime_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    slime_max_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_LIST})", name="ck_game_sessions_status_valid"),
        CheckConstraint("turn >= 0", name="ck_game_sessions_turn_non_negative"),
        CheckConstraint("potions >= 0", name="ck_game_sessions_potions_non_negative"),
        CheckConstraint("player_max_hp > 0", name="ck_game_sessions_player_max_hp_positive"),
        CheckConstraint(
            "player_hp >= 0 AND player_hp <= player_max_hp",
            name="ck_game_sessions_player_hp_within_max",
        ),
        CheckConstraint("slime_max_hp > 0", name="ck_game_sessions_slime_max_hp_positive"),
        CheckConstraint(
            "slime_hp >= 0 AND slime_hp <= slime_max_hp",
            name="ck_game_sessions_slime_hp_within_max",
        ),
    )


class CombatEventRow(Base):
    """`combat_events`：结构化战斗事件的持久化形式。

    事件是恢复与审计的事实来源：`sequence` 给出会话内的稳定顺序，
    外键保证事件不会指向不存在的会话，删除会话时事件级联删除。
    """

    __tablename__ = "combat_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("game_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    player_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    slime_hp: Mapped[int] = mapped_column(Integer, nullable=False)
    # 仅玩家动作事件记录剩余药水，因此允许为空；非空时不得为负数。
    potions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_combat_events_session_sequence"),
        CheckConstraint("sequence > 0", name="ck_combat_events_sequence_positive"),
        CheckConstraint("turn > 0", name="ck_combat_events_turn_positive"),
        CheckConstraint(f"actor IN ({_ACTOR_LIST})", name="ck_combat_events_actor_valid"),
        CheckConstraint(f"kind IN ({_KIND_LIST})", name="ck_combat_events_kind_valid"),
        CheckConstraint("value >= 0", name="ck_combat_events_value_non_negative"),
        CheckConstraint("player_hp >= 0", name="ck_combat_events_player_hp_non_negative"),
        CheckConstraint("slime_hp >= 0", name="ck_combat_events_slime_hp_non_negative"),
        CheckConstraint(
            "potions IS NULL OR potions >= 0",
            name="ck_combat_events_potions_non_negative",
        ),
    )
