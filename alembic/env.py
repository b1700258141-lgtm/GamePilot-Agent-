"""Alembic 迁移环境。

连接 URL 的唯一来源是项目配置模块 `gamepilot.config.Settings`
（环境变量 `DATABASE_URL`，或本地 `.env`），`alembic.ini` 中不保存凭据。

迁移必须由开发者显式执行；应用启动时不会自动运行迁移，
也不会用 `metadata.create_all()` 代替迁移。
"""

from logging.config import fileConfig

from alembic import context

from gamepilot.config import get_settings

# 导入 models 以注册所有表到 Base.metadata。
from gamepilot.persistence import models  # noqa: F401
from gamepilot.persistence.database import Base, create_db_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 与 `alembic check` 的比对基准。
target_metadata = Base.metadata


def _database_url() -> str:
    """从项目配置读取数据库 URL；缺失时立即报错，不使用任何隐式默认值。"""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：不连接数据库，只按配置渲染 SQL（用于审阅与交付脚本）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：用项目统一的 Engine 工厂建立连接并执行迁移。"""
    connectable = create_db_engine(_database_url())
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        # 迁移是一次性操作，退出前归还并关闭所有连接。
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
