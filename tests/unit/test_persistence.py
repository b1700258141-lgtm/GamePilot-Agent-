"""持久化基础设施单元测试：只检查映射结构与工厂行为，不连接真实数据库。

真实的建表与约束验证由 Alembic 命令在 PostgreSQL 上完成（见 README）。
这里刻意不使用 SQLite 冒充 PostgreSQL 集成测试。

需要验证「导入模块不产生副作用」时，用子进程运行探针，
避免在同一进程内反复 reload 模块而污染其他测试的表映射。
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import gamepilot.persistence.database as database_module

# 导入 models 以把所有表注册到 Base.metadata。
import gamepilot.persistence.models  # noqa: F401
from gamepilot.persistence.database import Base, create_db_engine, create_session_factory
from gamepilot.persistence.models import CombatEventRow, GameSessionRow

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_TABLE = "game_sessions"
EVENT_TABLE = "combat_events"

SESSION_COLUMNS = {
    "session_id",
    "seed",
    "status",
    "turn",
    "player_hp",
    "player_max_hp",
    "potions",
    "slime_hp",
    "slime_max_hp",
    "created_at",
    "updated_at",
}

EVENT_COLUMNS = {
    "id",
    "session_id",
    "sequence",
    "turn",
    "actor",
    "kind",
    "value",
    "player_hp",
    "slime_hp",
    "potions",
}


def _table(name: str):
    return Base.metadata.tables[name]


def _check_constraint(table_name: str, constraint_name: str) -> CheckConstraint:
    for constraint in _table(table_name).constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == constraint_name:
            return constraint
    raise AssertionError(f"{table_name} 缺少检查约束 {constraint_name}")


def _check_constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in _table(table_name).constraints
        if isinstance(constraint, CheckConstraint)
    }


def _only_foreign_key(table_name: str) -> ForeignKey:
    foreign_keys = [fk for column in _table(table_name).columns for fk in column.foreign_keys]
    assert len(foreign_keys) == 1, foreign_keys
    return foreign_keys[0]


def _run_probe(code: str) -> subprocess.CompletedProcess[str]:
    """在与测试相同的解释器中运行探针代码，并保证能导入本地包。"""
    existing = os.environ.get("PYTHONPATH")
    paths = [str(PROJECT_ROOT / "src"), *([existing] if existing else [])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=PROJECT_ROOT,
        check=False,
    )


def test_metadata_contains_exactly_expected_tables() -> None:
    assert set(Base.metadata.tables) == {SESSION_TABLE, EVENT_TABLE}


def test_game_sessions_columns_are_not_nullable() -> None:
    table = _table(SESSION_TABLE)
    assert set(table.c.keys()) == SESSION_COLUMNS
    assert all(not column.nullable for column in table.columns)


def test_game_sessions_primary_key_and_column_types() -> None:
    table = _table(SESSION_TABLE)
    assert list(table.primary_key.columns.keys()) == ["session_id"]
    assert isinstance(table.c.session_id.type, String)
    assert table.c.session_id.type.length == 32
    # 种子以规范十进制文本存储：领域/API 的种子是无界整数，BIGINT 存不下。
    assert isinstance(table.c.seed.type, Text)
    assert isinstance(table.c.status.type, String)
    assert table.c.status.type.length == 16
    for name in ("turn", "player_hp", "player_max_hp", "potions", "slime_hp", "slime_max_hp"):
        assert isinstance(table.c[name].type, Integer)


def test_game_sessions_timestamps_are_timezone_aware_and_db_generated() -> None:
    table = _table(SESSION_TABLE)
    for name in ("created_at", "updated_at"):
        column = table.c[name]
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True
        assert column.server_default is not None


def test_game_sessions_check_constraints() -> None:
    assert _check_constraint_names(SESSION_TABLE) == {
        "ck_game_sessions_status_valid",
        "ck_game_sessions_turn_non_negative",
        "ck_game_sessions_potions_non_negative",
        "ck_game_sessions_player_max_hp_positive",
        "ck_game_sessions_player_hp_within_max",
        "ck_game_sessions_slime_max_hp_positive",
        "ck_game_sessions_slime_hp_within_max",
    }
    status_sql = str(_check_constraint(SESSION_TABLE, "ck_game_sessions_status_valid").sqltext)
    for value in ("active", "won", "lost"):
        assert f"'{value}'" in status_sql
    player_sql = str(
        _check_constraint(SESSION_TABLE, "ck_game_sessions_player_hp_within_max").sqltext
    )
    assert "player_hp <= player_max_hp" in player_sql
    slime_sql = str(
        _check_constraint(SESSION_TABLE, "ck_game_sessions_slime_hp_within_max").sqltext
    )
    assert "slime_hp <= slime_max_hp" in slime_sql


def test_combat_events_columns_and_nullability() -> None:
    table = _table(EVENT_TABLE)
    assert set(table.c.keys()) == EVENT_COLUMNS
    # 只有 potions 允许为空（史莱姆事件不记录玩家药水数）。
    assert {column.name for column in table.columns if column.nullable} == {"potions"}


def test_combat_events_primary_key_is_database_generated() -> None:
    table = _table(EVENT_TABLE)
    assert list(table.primary_key.columns.keys()) == ["id"]
    assert isinstance(table.c.id.type, BigInteger)
    assert table.c.id.autoincrement is True


def test_combat_events_unique_constraint_on_session_and_sequence() -> None:
    uniques = [
        constraint
        for constraint in _table(EVENT_TABLE).constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    assert len(uniques) == 1
    assert uniques[0].name == "uq_combat_events_session_sequence"
    assert [column.name for column in uniques[0].columns] == ["session_id", "sequence"]


def test_combat_events_foreign_key_cascades_from_game_sessions() -> None:
    foreign_key = _only_foreign_key(EVENT_TABLE)
    assert foreign_key.target_fullname == f"{SESSION_TABLE}.session_id"
    assert foreign_key.ondelete == "CASCADE"
    assert _table(EVENT_TABLE).c.session_id.nullable is False


def test_combat_events_check_constraints() -> None:
    assert _check_constraint_names(EVENT_TABLE) == {
        "ck_combat_events_sequence_positive",
        "ck_combat_events_turn_positive",
        "ck_combat_events_actor_valid",
        "ck_combat_events_kind_valid",
        "ck_combat_events_value_non_negative",
        "ck_combat_events_player_hp_non_negative",
        "ck_combat_events_slime_hp_non_negative",
        "ck_combat_events_potions_non_negative",
    }
    actor_sql = str(_check_constraint(EVENT_TABLE, "ck_combat_events_actor_valid").sqltext)
    for value in ("player", "slime"):
        assert f"'{value}'" in actor_sql
    kind_sql = str(_check_constraint(EVENT_TABLE, "ck_combat_events_kind_valid").sqltext)
    for value in ("attack", "potion", "retaliate"):
        assert f"'{value}'" in kind_sql
    potions_sql = str(
        _check_constraint(EVENT_TABLE, "ck_combat_events_potions_non_negative").sqltext
    )
    assert "potions IS NULL" in potions_sql


def test_orm_rows_are_not_domain_models() -> None:
    """ORM 类只做表映射，不得替换或继承领域模型。"""
    from gamepilot.domain.combat import CombatSession
    from gamepilot.domain.models import CombatEvent, GameSnapshot

    domain_types = (CombatSession, GameSnapshot, CombatEvent)
    for row_class in (GameSessionRow, CombatEventRow):
        for domain_type in domain_types:
            assert not issubclass(row_class, domain_type)
            assert not isinstance(row_class(), domain_type)


def test_create_db_engine_uses_given_url_without_connecting() -> None:
    url = "postgresql+psycopg://someone:pw@db.invalid:5432/gamepilot"
    engine = create_db_engine(url)
    try:
        assert isinstance(engine, Engine)
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.url.render_as_string(hide_password=False) == url
        # 惰性连接：创建 Engine 本身不会建立任何连接。
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def test_create_session_factory_binds_engine_without_connecting() -> None:
    engine = create_db_engine("postgresql+psycopg://someone:pw@db.invalid:5432/gamepilot")
    try:
        session = create_session_factory(engine)()
        try:
            assert isinstance(session, Session)
            assert session.get_bind() is engine
            assert session.expire_on_commit is False
        finally:
            session.close()
    finally:
        engine.dispose()


def test_database_module_has_no_module_level_engine_or_session() -> None:
    assert not any(isinstance(value, Engine) for value in vars(database_module).values())
    assert not any(isinstance(value, Session) for value in vars(database_module).values())


def test_importing_persistence_does_not_create_engine() -> None:
    probe = (
        "import sqlalchemy\n"
        "created = []\n"
        "def _spy(*args, **kwargs):\n"
        "    created.append((args, kwargs))\n"
        "    raise AssertionError('导入期间不应创建 Engine')\n"
        "sqlalchemy.create_engine = _spy\n"
        "import gamepilot.config\n"
        "import gamepilot.persistence.database\n"
        "import gamepilot.persistence.models\n"
        "assert created == [], created\n"
        "print('no-engine')\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, result.stderr
    assert "no-engine" in result.stdout


def test_domain_layer_does_not_import_persistence() -> None:
    """依赖方向守卫：领域层不得反向依赖持久化实现。"""
    probe = (
        "import sys\n"
        "import gamepilot.domain.combat\n"
        "import gamepilot.domain.models\n"
        "prefix = 'gamepilot.persistence'\n"
        "leaked = sorted(name for name in sys.modules if name.startswith(prefix))\n"
        "assert leaked == [], leaked\n"
        "print('clean')\n"
    )
    result = _run_probe(probe)
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
