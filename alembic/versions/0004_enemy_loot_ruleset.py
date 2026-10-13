"""Add versioned enemies, gold and loot events without changing stored v1 games.

Revision ID: 0004_enemy_loot_ruleset
Revises: 0003_agent_test_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_enemy_loot_ruleset"
down_revision: str | Sequence[str] | None = "0003_agent_test_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "game_sessions", sa.Column("gold", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "game_sessions",
        sa.Column("enemy_type", sa.String(16), nullable=False, server_default="slime"),
    )
    op.add_column(
        "game_sessions", sa.Column("ruleset", sa.String(8), nullable=False, server_default="v1")
    )
    op.add_column("combat_events", sa.Column("item", sa.String(32), nullable=True))
    op.create_check_constraint("ck_game_sessions_gold_non_negative", "game_sessions", "gold >= 0")
    op.create_check_constraint(
        "ck_game_sessions_enemy_type_valid",
        "game_sessions",
        "enemy_type IN ('slime', 'goblin', 'troll')",
    )
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid", "game_sessions", "ruleset IN ('v1', 'v2')"
    )
    op.drop_constraint("ck_combat_events_actor_valid", "combat_events", type_="check")
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_actor_valid",
        "combat_events",
        "actor IN ('player', 'slime', 'goblin', 'troll')",
    )
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'potion', 'retaliate', 'drop')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.drop_constraint("ck_combat_events_actor_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_actor_valid", "combat_events", "actor IN ('player', 'slime')"
    )
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'potion', 'retaliate')",
    )
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.drop_constraint("ck_game_sessions_enemy_type_valid", "game_sessions", type_="check")
    op.drop_constraint("ck_game_sessions_gold_non_negative", "game_sessions", type_="check")
    op.drop_column("combat_events", "item")
    op.drop_column("game_sessions", "ruleset")
    op.drop_column("game_sessions", "enemy_type")
    op.drop_column("game_sessions", "gold")
