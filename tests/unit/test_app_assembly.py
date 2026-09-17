"""应用组装单元测试：配置选择、注入优先、资源归属与异常映射。

这些测试不连接数据库：默认后端是内存仓储，
PostgreSQL 分支只用不可达地址验证「启动探测会明确失败」。
真实数据库行为见 `tests/integration/test_postgres_repository.py`。
"""

import logging
import traceback
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

import gamepilot.main as main_module
from gamepilot.config import get_settings
from gamepilot.domain.combat import CombatSession
from gamepilot.main import create_app
from gamepilot.persistence.errors import (
    PersistenceConflictError,
    PersistenceInconsistentError,
    PersistenceUnavailableError,
)
from gamepilot.repositories.memory import InMemorySessionRepository

# 端口 1 上不会有 PostgreSQL：用于验证启动探测，不需要 Docker。
# 显式带上 connect_timeout：本机对无人监听的端口不是立即拒绝而是丢包，
# 没有超时会一直等下去。
UNREACHABLE_URL = "postgresql+psycopg://probe:probe@127.0.0.1:1/probe?connect_timeout=2"
SESSION_ID = "assembly-test-session"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **environment: str | None) -> None:
    """在临时目录中重设配置环境。

    切换工作目录是为了读不到本机 `.env`（`Settings` 从当前目录读取它），
    这样「缺少 DATABASE_URL」这类分支才能被真实覆盖。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REPOSITORY_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key, value in environment.items():
        if value is not None:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_app_defaults_to_memory_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """未配置后端时使用内存仓储，且不需要数据库配置。"""
    _use_config(monkeypatch, tmp_path)
    app = create_app()
    assert isinstance(app.state.repository, InMemorySessionRepository)

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    created = client.post("/api/v1/game-sessions", json={"seed": 7})
    assert created.status_code == 201
    fetched = client.get(f"/api/v1/game-sessions/{created.json()['session_id']}")
    assert fetched.json() == created.json()


def test_postgres_backend_without_url_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式选择 postgres 后缺少 URL 必须报配置错误，不能静默回退内存。"""
    _use_config(monkeypatch, tmp_path, REPOSITORY_BACKEND="postgres")
    with pytest.raises(ValidationError):
        create_app()


def test_illegal_backend_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        REPOSITORY_BACKEND="sqlite",
        DATABASE_URL=UNREACHABLE_URL,
    )
    with pytest.raises(ValidationError):
        create_app()


def test_injected_repository_wins_over_backend_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式注入优先：即使环境里选了 postgres 且没有 URL，也能正常组装。"""
    _use_config(monkeypatch, tmp_path, REPOSITORY_BACKEND="postgres")
    injected = InMemorySessionRepository()
    app = create_app(repository=injected)

    assert app.state.repository is injected
    client = TestClient(app)
    created = client.post("/api/v1/game-sessions", json={"seed": 11})
    assert created.status_code == 201
    assert injected.get(created.json()["session_id"]).seed == 11


def test_injected_repository_survives_lifespan_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """注入对象的资源归调用方：应用关闭时不得释放或替换它。"""
    _use_config(monkeypatch, tmp_path)
    injected = InMemorySessionRepository()
    app = create_app(repository=injected)

    with TestClient(app) as client:
        session_id = client.post("/api/v1/game-sessions", json={"seed": 3}).json()["session_id"]

    # 应用已关闭，但注入的仓储仍可继续使用（没有被 dispose/清空）。
    assert injected.get(session_id).seed == 3


def test_postgres_app_probes_connection_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """创建应用本身不连接数据库；连接探测发生在 lifespan 启动，失败即明确报错。"""
    _use_config(
        monkeypatch,
        tmp_path,
        REPOSITORY_BACKEND="postgres",
        DATABASE_URL=UNREACHABLE_URL,
    )
    app = create_app()

    with pytest.raises(PersistenceUnavailableError, match="not reachable at startup"):
        anyio.run(_enter_lifespan, app)


def test_failed_startup_disposes_engine_and_hides_original_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """探测失败也释放应用 Engine，且抛出的异常链不含原始 SQL 或参数。"""
    marker = "REVIEW_FAKE_SECRET"
    _use_config(
        monkeypatch,
        tmp_path,
        REPOSITORY_BACKEND="postgres",
        DATABASE_URL=UNREACHABLE_URL,
    )
    fake_engine = Mock(spec=Engine)
    fake_engine.connect.side_effect = OperationalError(
        "SELECT REVIEW_SQL", {"token": marker}, Exception(marker)
    )
    monkeypatch.setattr(main_module, "create_db_engine", lambda _url: fake_engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda _engine: Mock())
    app = main_module.create_app()

    with pytest.raises(PersistenceUnavailableError) as exc_info:
        anyio.run(_enter_lifespan, app)

    fake_engine.dispose.assert_called_once_with()
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert marker not in rendered
    assert "SELECT REVIEW_SQL" not in rendered


class _FailingRepository:
    """始终抛出指定错误的仓储替身，用于验证异常映射。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def save(self, session: CombatSession) -> None:
        raise self._error

    def get(self, session_id: str) -> CombatSession:
        raise self._error


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (PersistenceConflictError("conflict"), 409, "persistence_conflict"),
        (PersistenceUnavailableError("unavailable"), 503, "persistence_unavailable"),
        (PersistenceInconsistentError("inconsistent"), 500, "persistence_inconsistent"),
    ],
)
def test_persistence_errors_map_to_stable_http_status(
    error: Exception, status: int, code: str
) -> None:
    app = create_app(repository=_FailingRepository(error))
    client = TestClient(app)
    for response in (
        client.get(f"/api/v1/game-sessions/{SESSION_ID}"),
        client.post(f"/api/v1/game-sessions/{SESSION_ID}/actions", json={"action": "attack"}),
    ):
        assert response.status_code == status
        assert response.json()["code"] == code


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("postgresql+psycopg://gamepilot:secret@127.0.0.1:5432/gamepilot is down"),
        OperationalError(
            "SELECT 1",
            {},
            Exception("connection failed for user=gamepilot password=secret"),
        ),
    ],
)
def test_unexpected_errors_do_not_leak_connection_details(error: Exception) -> None:
    """未预期错误统一返回通用 500，响应里不含原始异常文本或凭据。"""
    app = create_app(repository=_FailingRepository(error))
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/api/v1/game-sessions/{SESSION_ID}")

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "message": "internal server error"}
    assert "secret" not in response.text
    assert "gamepilot" not in response.text


def test_sqlalchemy_errors_do_not_escape_or_leak_into_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """具体数据库处理器消费异常，只记录类型，不让服务器重抛原始 SQL。"""
    marker = "REVIEW_FAKE_SECRET"
    error = ProgrammingError(
        "SELECT REVIEW_SQL",
        {"token": marker},
        Exception(marker),
    )
    app = create_app(repository=_FailingRepository(error))

    # 默认 raise_server_exceptions=True：若异常逃出 ASGI 应用，此处会直接抛出。
    with caplog.at_level(logging.ERROR, logger="gamepilot.api.errors"):
        response = TestClient(app).get(f"/api/v1/game-sessions/{SESSION_ID}")

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "message": "internal server error"}
    assert marker not in response.text
    assert marker not in caplog.text
    assert "SELECT REVIEW_SQL" not in caplog.text
    assert "ProgrammingError" in caplog.text


async def _enter_lifespan(app: FastAPI) -> None:
    """手动进入应用 lifespan，用于断言启动探测行为。"""
    async with app.router.lifespan_context(app):
        pass
