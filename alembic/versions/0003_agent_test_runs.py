"""Persist local Agent test progress and reports.

Revision ID: 0003_agent_test_runs
Revises: 0002_session_seed_text
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_agent_test_runs"
down_revision: str | Sequence[str] | None = "0002_session_seed_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_test_runs",
        sa.Column("run_id", sa.String(length=40), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_test_runs")
