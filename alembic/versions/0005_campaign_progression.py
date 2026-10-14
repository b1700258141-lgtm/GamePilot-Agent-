"""Add versioned campaigns and player progression while preserving v1/v2 saves.

Revision ID: 0005_campaign_progression
Revises: 0004_enemy_loot_ruleset
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_campaign_progression"
down_revision: str | Sequence[str] | None = "0004_enemy_loot_ruleset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, default in (
        ("level", "1"),
        ("exp", "0"),
        ("battle_count", "1"),
        ("battle_index", "1"),
    ):
        op.add_column(
            "game_sessions", sa.Column(name, sa.Integer(), nullable=False, server_default=default)
        )
    op.create_check_constraint(
        "ck_game_sessions_level_exp_valid", "game_sessions", "level >= 1 AND exp >= 0"
    )
    op.create_check_constraint(
        "ck_game_sessions_battle_range",
        "game_sessions",
        "battle_count BETWEEN 1 AND 6 AND battle_index BETWEEN 1 AND battle_count",
    )
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid", "game_sessions", "ruleset IN ('v1', 'v2', 'v3')"
    )
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'potion', 'retaliate', 'drop', 'level_up', 'next_battle')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'potion', 'retaliate', 'drop')",
    )
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid", "game_sessions", "ruleset IN ('v1', 'v2')"
    )
    op.drop_constraint("ck_game_sessions_battle_range", "game_sessions", type_="check")
    op.drop_constraint("ck_game_sessions_level_exp_valid", "game_sessions", type_="check")
    for name in ("battle_index", "battle_count", "exp", "level"):
        op.drop_column("game_sessions", name)
