"""Add v4 enemy types and persistent EP and magic projections.

Revision ID: 0007_ep_magic_enemies
Revises: 0006_power_strike
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_ep_magic_enemies"
down_revision: str | Sequence[str] | None = "0006_power_strike"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, default in (("ep", "100"), ("max_ep", "100"), ("magic", "10")):
        op.add_column(
            "game_sessions", sa.Column(name, sa.Integer(), nullable=False, server_default=default)
        )
    op.create_check_constraint(
        "ck_game_sessions_ep_magic_valid",
        "game_sessions",
        "max_ep >= 100 AND ep >= 0 AND ep <= max_ep AND magic >= 10",
    )
    op.drop_constraint("ck_game_sessions_enemy_type_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_enemy_type_valid",
        "game_sessions",
        "enemy_type IN ('slime', 'goblin', 'skeleton', 'wolf', 'orc', 'troll', 'wraith', 'dragon')",
    )
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid", "game_sessions", "ruleset IN ('v1', 'v2', 'v3', 'v4')"
    )
    op.drop_constraint("ck_combat_events_actor_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_actor_valid",
        "combat_events",
        "actor IN ('player', 'slime', 'goblin', 'skeleton', 'wolf', "
        "'orc', 'troll', 'wraith', 'dragon')",
    )
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'power_strike', 'cast_spark', 'cast_flame', 'cast_meteor', "
        "'cast_heal', 'potion', 'retaliate', 'drop', 'level_up', 'next_battle')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'power_strike', 'potion', 'retaliate', "
        "'drop', 'level_up', 'next_battle')",
    )
    op.drop_constraint("ck_combat_events_actor_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_actor_valid",
        "combat_events",
        "actor IN ('player', 'slime', 'goblin', 'troll')",
    )
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid", "game_sessions", "ruleset IN ('v1', 'v2', 'v3')"
    )
    op.drop_constraint("ck_game_sessions_enemy_type_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_enemy_type_valid",
        "game_sessions",
        "enemy_type IN ('slime', 'goblin', 'troll')",
    )
    op.drop_constraint("ck_game_sessions_ep_magic_valid", "game_sessions", type_="check")
    for name in ("magic", "max_ep", "ep"):
        op.drop_column("game_sessions", name)
