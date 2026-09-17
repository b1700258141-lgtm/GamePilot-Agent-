"""应用配置：从环境变量（或本地 `.env`）读取运行参数。

设计约束：

- 导入本模块不会连接数据库，也不会创建 Engine，只做配置解析；
- 代码中不写任何真实凭据，`DATABASE_URL` 必须由外部显式提供；
- 本模块不参与 `create_app()` 的仓储选择，FastAPI 默认仍使用内存仓储。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行期配置。

    字段从环境变量读取（大小写不敏感），`.env` 仅作为本地开发的补充来源。
    `database_url` 没有默认值：缺失时立即报错，避免误连到非预期数据库。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。

    读取环境变量不产生 I/O 副作用；缓存只是为了避免重复解析，
    测试中如需不同配置可直接构造 `Settings(...)`。
    """
    return Settings()
