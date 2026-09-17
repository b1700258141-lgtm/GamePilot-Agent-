"""应用配置：从环境变量（或本地 `.env`）读取运行参数。

设计约束：

- 导入本模块不会连接数据库，也不会创建 Engine，只做配置解析；
- 代码中不写任何真实凭据，连接 URL 由外部显式提供；
- 默认后端是内存仓储：只有显式选择 postgres 时才要求数据库配置，
  并且永远不会因为「配置缺失」而静默回退到内存。
"""

from enum import Enum
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 本阶段只支持同步 psycopg 3 驱动。
POSTGRES_URL_PREFIX = "postgresql+psycopg://"


class RepositoryBackend(str, Enum):
    """仓储后端选择。

    - `memory`：进程内内存仓储，默认值，原有测试与演示无需数据库；
    - `postgres`：PostgreSQL 仓储，必须提供可用的 `DATABASE_URL`。
    """

    MEMORY = "memory"
    POSTGRES = "postgres"


def _require_postgres_url(database_url: str | None, *, purpose: str) -> str:
    """校验并返回 PostgreSQL URL；缺失或协议不符时明确报错。"""
    if not database_url:
        raise ValueError(f"DATABASE_URL is required {purpose}")
    if not database_url.startswith(POSTGRES_URL_PREFIX):
        raise ValueError(f"DATABASE_URL must start with {POSTGRES_URL_PREFIX!r} {purpose}")
    return database_url


class Settings(BaseSettings):
    """运行期配置。

    字段从环境变量读取（大小写不敏感），`.env` 仅作为本地开发的补充来源。
    非法后端取值会直接产生配置校验错误，不会退化成某个默认后端。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    repository_backend: RepositoryBackend = RepositoryBackend.MEMORY
    database_url: str | None = None

    @model_validator(mode="after")
    def _validate_database_url(self) -> "Settings":
        if self.repository_backend is RepositoryBackend.POSTGRES:
            _require_postgres_url(self.database_url, purpose="when REPOSITORY_BACKEND=postgres")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    读取环境变量不产生 I/O 副作用；缓存只是为了避免重复解析，
    测试中如需不同配置可直接构造 `Settings(...)`。
    """
    return Settings()


def require_database_url() -> str:
    """返回必填的 PostgreSQL URL，供 Alembic 等数据库工具使用。

    与 `Settings` 的差别：这里不关心 `REPOSITORY_BACKEND`，
    任何时候都必须能拿到可用的 postgresql+psycopg URL，
    避免迁移在默认的 memory 配置下静默拿到空连接串。
    """
    return _require_postgres_url(get_settings().database_url, purpose="to run database migrations")
