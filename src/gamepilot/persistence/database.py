"""SQLAlchemy 基础设施：声明式 Base、Engine 工厂与 Session 工厂。

职责边界：

- 本模块只负责装配连接与映射层，不含任何仓储业务逻辑；
- 导入本模块不会创建 Engine，也不会连接数据库；
- Engine 与 Session 都由调用方显式创建和释放，没有模块级单例，
  便于测试注入和后续按请求管理会话生命周期。
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """项目所有 ORM 表的声明式基类。

    `Base.metadata` 是 Alembic `target_metadata` 的来源，
    也是测试中断言表结构的只读入口。
    """


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    """按显式传入的 URL 创建同步 Engine。

    Engine 自身持有连接池，但创建时不建立任何连接（首次执行语句才连接）。
    调用方负责在不再使用时调用 `engine.dispose()`。

    `pool_pre_ping` 会在借出连接前做一次轻量探测，避免使用到已被
    数据库或网络中断的连接。
    """
    return create_engine(database_url, echo=echo, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """按已创建的 Engine 生成 Session 工厂。

    返回的是工厂而不是 Session 实例：每次业务操作各自取得并关闭会话，
    不使用跨请求的长生命周期 Session。
    """
    return sessionmaker(bind=engine, expire_on_commit=False)
