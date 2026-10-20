"""Allow the power strike event in persisted combat histories.

Revision ID: 0006_power_strike
Revises: 0005_campaign_progression
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_power_strike"
down_revision: str | Sequence[str] | None = "0005_campaign_progression"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'power_strike', 'potion', 'retaliate', "
        "'drop', 'level_up', 'next_battle')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_combat_events_kind_valid", "combat_events", type_="check")
    op.create_check_constraint(
        "ck_combat_events_kind_valid",
        "combat_events",
        "kind IN ('attack', 'potion', 'retaliate', 'drop', 'level_up', 'next_battle')",
    )
