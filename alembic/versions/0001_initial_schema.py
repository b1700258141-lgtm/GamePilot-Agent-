"""建立 game_sessions 与 combat_events 两张表

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-17

本迁移是第二阶段数据库骨架的起点，只创建表、约束与外键：
- game_sessions 保存战斗会话的当前投影；
- combat_events 保存结构化战斗事件，按 (session_id, sequence) 唯一。

约束以检查约束表达（不使用 PostgreSQL 原生 ENUM），
列定义与 src/gamepilot/persistence/models.py 保持一致。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建两张表及其约束、外键。"""
    op.create_table(
        "game_sessions",
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("player_hp", sa.Integer(), nullable=False),
        sa.Column("player_max_hp", sa.Integer(), nullable=False),
        sa.Column("potions", sa.Integer(), nullable=False),
        sa.Column("slime_hp", sa.Integer(), nullable=False),
        sa.Column("slime_max_hp", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.CheckConstraint(
            "status IN ('active', 'won', 'lost')",
            name="ck_game_sessions_status_valid",
        ),
        sa.CheckConstraint("turn >= 0", name="ck_game_sessions_turn_non_negative"),
        sa.CheckConstraint("potions >= 0", name="ck_game_sessions_potions_non_negative"),
        sa.CheckConstraint("player_max_hp > 0", name="ck_game_sessions_player_max_hp_positive"),
        sa.CheckConstraint(
            "player_hp >= 0 AND player_hp <= player_max_hp",
            name="ck_game_sessions_player_hp_within_max",
        ),
        sa.CheckConstraint("slime_max_hp > 0", name="ck_game_sessions_slime_max_hp_positive"),
        sa.CheckConstraint(
            "slime_hp >= 0 AND slime_hp <= slime_max_hp",
            name="ck_game_sessions_slime_hp_within_max",
        ),
    )

    op.create_table(
        "combat_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("player_hp", sa.Integer(), nullable=False),
        sa.Column("slime_hp", sa.Integer(), nullable=False),
        sa.Column("potions", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["game_sessions.session_id"],
            ondelete="CASCADE",
        ),
        # 该唯一约束在 PostgreSQL 中会隐式创建同列 btree 索引，
        # 已满足「按会话按序号顺序读取事件」的需求，不再建立重复索引。
        sa.UniqueConstraint("session_id", "sequence", name="uq_combat_events_session_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_combat_events_sequence_positive"),
        sa.CheckConstraint("turn > 0", name="ck_combat_events_turn_positive"),
        sa.CheckConstraint("actor IN ('player', 'slime')", name="ck_combat_events_actor_valid"),
        sa.CheckConstraint(
            "kind IN ('attack', 'potion', 'retaliate')",
            name="ck_combat_events_kind_valid",
        ),
        sa.CheckConstraint("value >= 0", name="ck_combat_events_value_non_negative"),
        sa.CheckConstraint("player_hp >= 0", name="ck_combat_events_player_hp_non_negative"),
        sa.CheckConstraint("slime_hp >= 0", name="ck_combat_events_slime_hp_non_negative"),
        sa.CheckConstraint(
            "potions IS NULL OR potions >= 0",
            name="ck_combat_events_potions_non_negative",
        ),
    )


def downgrade() -> None:
    """按依赖顺序撤销：先删事件表，再删会话表。"""
    op.drop_table("combat_events")
    op.drop_table("game_sessions")
