"""PostgreSQL 集成测试夹具。

安全边界：

- 只使用显式的 `TEST_DATABASE_URL`，绝不回退到开发用的 `DATABASE_URL`；
- 连接前校验目标库名确实是专用测试库 `gamepilot_test`，不是就不跑；
- 清理只针对测试自己创建的会话 id（外键级联删除事件），
  不使用 TRUNCATE、不 drop 整库、不触碰开发库或数据卷；
- 输出 URL 时一律隐藏口令。
"""

import os
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gamepilot.config import get_settings
from gamepilot.domain.combat import CombatSession
from gamepilot.persistence.database import create_db_engine, create_session_factory
from gamepilot.persistence.models import GameSessionRow
from gamepilot.repositories.postgres import PostgresSessionRepository

TEST_DATABASE_NAME = "gamepilot_test"
EXPECTED_REVISION = "0002_session_seed_text"


def hide_password(url: str) -> str:
    """渲染 URL 时隐藏口令，保证断言消息里不出现凭据。"""
    return make_url(url).render_as_string(hide_password=True)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """返回经过校验的 TEST_DATABASE_URL。"""
    raw = os.environ.get("TEST_DATABASE_URL")
    if not raw:
        # 正常情况下根 conftest 已经跳过；这里只是防御性兜底。
        pytest.skip("TEST_DATABASE_URL 未设置")

    url = make_url(raw)
    if url.drivername != "postgresql+psycopg":
        pytest.fail(f"TEST_DATABASE_URL 必须使用 postgresql+psycopg 驱动：{hide_password(raw)}")
    if url.database != TEST_DATABASE_NAME:
        pytest.fail(
            f"TEST_DATABASE_URL 必须指向专用测试库 {TEST_DATABASE_NAME!r}，"
            f"当前指向 {url.database!r}：{hide_password(raw)}"
        )
    return raw


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    """连接测试库并确认已迁移到最新版本。

    不可达或未迁移都属于失败（不是跳过）：如果这里静默跳过，
    「集成测试全部通过」就会变成假象。
    """
    engine = create_db_engine(test_database_url)
    try:
        try:
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
        except SQLAlchemyError as exc:
            pytest.fail(
                f"无法读取测试库 {TEST_DATABASE_NAME} 的迁移版本（{type(exc).__name__}）；"
                "请确认数据库可连接，并以 TEST_DATABASE_URL 执行 alembic upgrade head"
            )
        if revision != EXPECTED_REVISION:
            pytest.fail(
                f"测试库 {TEST_DATABASE_NAME} 当前迁移版本为 {revision!r}，"
                f"期望 {EXPECTED_REVISION!r}；请先执行 alembic upgrade head"
            )
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def tracked_session_ids(engine: Engine) -> Iterator[list[str]]:
    """记录本测试创建过的会话 id，并在结束后按 id 精确删除。

    只删除这些 id 对应的行；事件行由外键 ON DELETE CASCADE 连带删除。
    """
    session_ids: list[str] = []
    yield session_ids
    if not session_ids:
        return
    with engine.begin() as connection:
        connection.execute(delete(GameSessionRow).where(GameSessionRow.session_id.in_(session_ids)))


class TrackedPostgresRepository(PostgresSessionRepository):
    """真实仓储，额外记录本测试创建过的会话 id 以便精确清理。"""

    def __init__(self, factory: sessionmaker[Session], tracked_ids: list[str]) -> None:
        super().__init__(factory)
        self._tracked_ids = tracked_ids

    def save(self, session: CombatSession) -> None:
        super().save(session)
        if session.session_id not in self._tracked_ids:
            self._tracked_ids.append(session.session_id)


@pytest.fixture
def repository(
    session_factory: sessionmaker[Session], tracked_session_ids: list[str]
) -> TrackedPostgresRepository:
    """指向测试库的仓储；用它创建的会话会被自动登记以便清理。"""
    return TrackedPostgresRepository(session_factory, tracked_session_ids)


@pytest.fixture
def make_engine(test_database_url: str) -> Iterator[Callable[[], Engine]]:
    """额外创建 Engine（模拟应用重启），测试结束时统一释放。"""
    engines: list[Engine] = []

    def _create() -> Engine:
        engine = create_db_engine(test_database_url)
        engines.append(engine)
        return engine

    yield _create
    for engine in engines:
        engine.dispose()


@pytest.fixture
def postgres_app_config(monkeypatch: pytest.MonkeyPatch, test_database_url: str) -> Iterator[None]:
    """让 create_app() 组装出指向测试库的 PostgreSQL 应用。

    环境变量优先于本机 `.env`，因此这里显式覆盖 DATABASE_URL；
    测试库名已由 test_database_url 夹具校验过。
    """
    monkeypatch.setenv("REPOSITORY_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
