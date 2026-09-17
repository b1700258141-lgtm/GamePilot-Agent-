"""配置模块单元测试。

只验证环境变量解析行为，不连接数据库、不读取真实凭据；
构造 Settings 时显式传入 `_env_file=None`，避免本机 `.env` 影响断言。
"""

import pytest
from pydantic import ValidationError

from gamepilot.config import Settings, get_settings


def test_settings_reads_database_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@db.internal:5432/gamepilot")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql+psycopg://user:pw@db.internal:5432/gamepilot"


def test_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有默认值：缺失时必须直接报错，而不是连到某个隐式默认库。"""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


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
