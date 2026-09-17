"""0001 → 0002 种子列迁移验证：保值升级与受控降级。

每个用例都在自己创建的、名字唯一的临时测试库上执行，绝不触碰开发库：

- 建库前校验名字带专用前缀；
- 只对这个临时库执行 upgrade/downgrade；
- 用后强制删除（`DROP DATABASE ... WITH (FORCE)`）。

降级用例验证的是「数据安全」而不是「降级能跑」：
存在 BIGINT 无法表示的种子时必须显式失败，
并且数据、列类型与迁移版本三者都保持原样。
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from gamepilot.persistence.database import create_db_engine

pytestmark = pytest.mark.postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMP_DB_PREFIX = "gamepilot_test_mig_"
REVISION_0001 = "0001_initial_schema"
REVISION_0002 = "0002_session_seed_text"
BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1


def _insert_session(engine: Engine, session_id: str, seed: str | int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO game_sessions (session_id, seed, status, turn, player_hp, "
                "player_max_hp, potions, slime_hp, slime_max_hp) "
                "VALUES (:session_id, :seed, 'active', 0, 100, 100, 2, 60, 60)"
            ),
            {"session_id": session_id, "seed": seed},
        )


def _seeds(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT session_id, seed FROM game_sessions")).all()
    return {row[0]: row[1] for row in rows}


def _seed_column_type(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'game_sessions' AND column_name = 'seed'"
            )
        ).scalar_one()


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def _redact(output: str, database_url: str) -> str:
    """输出里不得出现数据库口令。"""
    password = make_url(database_url).password
    return output.replace(password, "***") if password else output


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """在子进程里执行真实 alembic 命令，等价于开发者手动执行。

    迁移不依赖仓储后端选择，因此这里只提供 DATABASE_URL。
    """
    environment = {key: value for key, value in os.environ.items() if key != "REPOSITORY_BACKEND"}
    environment["DATABASE_URL"] = database_url
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _assert_alembic_ok(
    result: subprocess.CompletedProcess[str], database_url: str, action: str
) -> None:
    assert result.returncode == 0, (
        f"alembic {action} 失败（退出码 {result.returncode}）：\n"
        f"{_redact(result.stdout[-1500:] + result.stderr[-1500:], database_url)}"
    )


@pytest.fixture
def temp_database(test_database_url: str) -> Iterator[str]:
    """创建名字唯一的临时测试库，用后强制删除。"""
    base = make_url(test_database_url)
    name = f"{TEMP_DB_PREFIX}{uuid.uuid4().hex[:12]}"
    assert name.startswith(TEMP_DB_PREFIX)
    assert name not in {base.database, "postgres"}

    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_upgrade_preserves_legacy_seed_values(temp_database: str) -> None:
    """0001 里的旧数据升级到 0002 后原样保留，只是列类型变为文本。"""
    _assert_alembic_ok(
        _run_alembic(temp_database, "upgrade", REVISION_0001), temp_database, "upgrade 0001"
    )
    legacy = {
        "legacy-zero": 0,
        "legacy-negative": -7,
        "legacy-typical": 123456789,
        "legacy-bigint-max": BIGINT_MAX,
        "legacy-bigint-min": BIGINT_MIN,
    }

    engine = create_db_engine(temp_database)
    try:
        for session_id, seed in legacy.items():
            _insert_session(engine, session_id, seed)
        assert _seed_column_type(engine) == "bigint"

        _assert_alembic_ok(
            _run_alembic(temp_database, "upgrade", "head"), temp_database, "upgrade head"
        )

        assert _seed_column_type(engine) == "text"
        assert _seeds(engine) == {sid: str(seed) for sid, seed in legacy.items()}
        assert _revision(engine) == REVISION_0002
    finally:
        engine.dispose()


def test_downgrade_to_bigint_succeeds_for_in_range_seeds(temp_database: str) -> None:
    """全部取值都在 BIGINT 范围内时，降级成功且数值不变。"""
    _assert_alembic_ok(
        _run_alembic(temp_database, "upgrade", "head"), temp_database, "upgrade head"
    )
    in_range = {"range-min": BIGINT_MIN, "range-zero": 0, "range-max": BIGINT_MAX}

    engine = create_db_engine(temp_database)
    try:
        for session_id, seed in in_range.items():
            _insert_session(engine, session_id, str(seed))

        _assert_alembic_ok(
            _run_alembic(temp_database, "downgrade", REVISION_0001), temp_database, "downgrade"
        )

        assert _seed_column_type(engine) == "bigint"
        assert _seeds(engine) == {sid: seed for sid, seed in in_range.items()}
        assert _revision(engine) == REVISION_0001
    finally:
        engine.dispose()


def test_downgrade_fails_and_preserves_data_for_out_of_range_seed(temp_database: str) -> None:
    """存在 BIGINT 放不下的种子时，降级必须失败且不破坏任何数据与版本。"""
    _assert_alembic_ok(
        _run_alembic(temp_database, "upgrade", "head"), temp_database, "upgrade head"
    )
    huge_seed = 2**80
    survivor = "out-of-range-seed"

    engine = create_db_engine(temp_database)
    try:
        _insert_session(engine, survivor, str(huge_seed))

        result = _run_alembic(temp_database, "downgrade", REVISION_0001)

        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert "cannot downgrade" in output  # 显式失败，而不是静默截断
        # 数据、列类型与迁移版本都保持原样。
        assert _seeds(engine) == {survivor: str(huge_seed)}
        assert _seed_column_type(engine) == "text"
        assert _revision(engine) == REVISION_0002
    finally:
        engine.dispose()
