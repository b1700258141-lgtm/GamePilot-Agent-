"""配置模块单元测试。

只验证环境变量解析行为，不连接数据库、不读取真实凭据；
构造 Settings 时显式传入 `_env_file=None`，避免本机 `.env` 影响断言。

需要验证「缺失配置」的分支时，先切到临时目录，让 `.env` 不再被读到，
再清空 `get_settings` 的缓存，避免本机开发配置泄漏进断言。
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from gamepilot.config import (
    RepositoryBackend,
    Settings,
    get_settings,
    require_database_url,
)

POSTGRES_URL = "postgresql+psycopg://user:pw@db.internal:5432/gamepilot"


def _without_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让 get_settings() 读不到本机 .env，并清空缓存。"""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def test_settings_defaults_to_memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认后端是内存仓储：不配置数据库也能构造出配置。"""
    monkeypatch.delenv("REPOSITORY_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.repository_backend is RepositoryBackend.MEMORY
    assert settings.database_url is None


def test_settings_reads_database_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    settings = Settings(_env_file=None)
    assert settings.database_url == POSTGRES_URL


def test_memory_backend_does_not_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """内存模式不需要数据库配置（TASK-002A 的「始终必填」改为条件必填）。"""
    monkeypatch.setenv("REPOSITORY_BACKEND", "memory")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url is None


def test_postgres_backend_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """选择 postgres 后端后 URL 必填：缺失时明确报错，不回退内存。"""
    monkeypatch.setenv("REPOSITORY_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_postgres_backend_rejects_non_psycopg_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """只接受同步 psycopg 3 驱动，避免落到其他驱动或异步实现。"""
    monkeypatch.setenv("REPOSITORY_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/gamepilot")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_illegal_backend_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法后端取值直接报配置错误，不静默退化成某个后端。"""
    monkeypatch.setenv("REPOSITORY_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "repository_backend" in str(excinfo.value)


def test_settings_ignores_unrelated_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一环境里存在其他变量（如 POSTGRES_PASSWORD）不应导致配置解析失败。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/gamepilot")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unrelated-value")
    settings = Settings(_env_file=None)
    assert settings.database_url.endswith("/gamepilot")
    assert not hasattr(settings, "postgres_password")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/cached")
    get_settings.cache_clear()
    try:
        first = get_settings()
        assert first.database_url.endswith("/cached")
        assert get_settings() is first
    finally:
        get_settings.cache_clear()


def test_require_database_url_ignores_backend_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """迁移工具必须拿到显式 URL：即使应用跑在默认的内存后端也一样。"""
    monkeypatch.delenv("REPOSITORY_BACKEND", raising=False)
    monkeypatch.setenv("DATABASE_URL", POSTGRES_URL)
    _without_env_file(monkeypatch, tmp_path)
    try:
        assert require_database_url() == POSTGRES_URL
    finally:
        get_settings.cache_clear()


def test_require_database_url_reports_missing_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺少 URL 时立即报错，而不是回退成一个空连接串。"""
    monkeypatch.delenv("REPOSITORY_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _without_env_file(monkeypatch, tmp_path)
    try:
        with pytest.raises(ValueError, match="DATABASE_URL is required"):
            require_database_url()
    finally:
        get_settings.cache_clear()


def test_require_database_url_rejects_non_psycopg_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/gamepilot")
    _without_env_file(monkeypatch, tmp_path)
    try:
        with pytest.raises(ValueError, match="postgresql\\+psycopg"):
            require_database_url()
    finally:
        get_settings.cache_clear()
