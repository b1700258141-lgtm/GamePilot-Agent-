"""把 game_sessions.seed 由 BIGINT 改为文本

Revision ID: 0002_session_seed_text
Revises: 0001_initial_schema
Create Date: 2026-09-17

背景：领域与 API 的种子是无界 Python 整数（例如 2**80），BIGINT
无法覆盖现有输入范围。种子不参与数据库数值计算，因此用规范十进制
文本直接保留 Python 整数表示，避免为 NUMERIC 人为选择精度上限；
仓储在读写时与 int 互转。

本迁移只改列类型，不重写已应用的 0001，也不改动任何既有数值：
- 升级：bigint → text，用 `seed::text` 转换，已存数据原样保留；
- 降级：text → bigint，仅当所有值都是可放入 BIGINT 的规范十进制整数时执行。
  存在无法表示的值时必须显式失败：不截断、不取模、不删除行，
  由 PostgreSQL 的事务性 DDL 回滚整个迁移，数据与迁移版本都保持原状。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_session_seed_text"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 规范十进制文本：可带负号，无前导零、无正号、无空白。
_SEED_PATTERN = r"^-?(0|[1-9][0-9]*)$"
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1


def upgrade() -> None:
    """BIGINT → text，保留全部既有取值。"""
    op.alter_column(
        "game_sessions",
        "seed",
        existing_type=sa.BigInteger(),
        type_=sa.Text(),
        existing_nullable=False,
        postgresql_using="seed::text",
    )


def downgrade() -> None:
    """text → BIGINT；存在 BIGINT 无法表示的值时显式失败并保留数据。"""
    connection = op.get_bind()

    # 分两步检查：先确认全部是数字文本，再做数值范围比较，
    # 避免在非数字文本上直接 cast 触发数据库错误。
    non_numeric = connection.execute(
        sa.text("SELECT count(*) FROM game_sessions WHERE seed !~ :pattern"),
        {"pattern": _SEED_PATTERN},
    ).scalar_one()

    out_of_range = 0
    if non_numeric == 0:
        out_of_range = connection.execute(
            sa.text(
                "SELECT count(*) FROM game_sessions "
                "WHERE seed::numeric < :min_value OR seed::numeric > :max_value"
            ),
            {"min_value": _BIGINT_MIN, "max_value": _BIGINT_MAX},
        ).scalar_one()

    if non_numeric or out_of_range:
        raise RuntimeError(
            "cannot downgrade game_sessions.seed to BIGINT: "
            f"{non_numeric} value(s) are not canonical decimal integers and "
            f"{out_of_range} value(s) are outside BIGINT range; "
            "no rows were modified - resolve the data before downgrading"
        )

    op.alter_column(
        "game_sessions",
        "seed",
        existing_type=sa.Text(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        postgresql_using="seed::bigint",
    )
