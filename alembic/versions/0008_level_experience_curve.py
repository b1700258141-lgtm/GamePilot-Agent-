"""Allow v5 sessions with increasing experience requirements.

Revision ID: 0008_level_experience_curve
Revises: 0007_ep_magic_enemies
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_level_experience_curve"
down_revision: str | Sequence[str] | None = "0007_ep_magic_enemies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid",
        "game_sessions",
        "ruleset IN ('v1', 'v2', 'v3', 'v4', 'v5')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_game_sessions_ruleset_valid", "game_sessions", type_="check")
    op.create_check_constraint(
        "ck_game_sessions_ruleset_valid",
        "game_sessions",
        "ruleset IN ('v1', 'v2', 'v3', 'v4')",
    )
