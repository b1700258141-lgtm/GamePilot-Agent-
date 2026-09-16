"""FastAPI 应用入口：组装路由、异常处理与仓储。

使用应用工厂 create_app，便于测试中注入独立的仓储实例；
模块级 app 供 uvicorn 启动使用。
"""

from fastapi import FastAPI

from .api.errors import register_exception_handlers
from .api.routes import router
from .repositories.base import SessionRepository
from .repositories.memory import InMemorySessionRepository

API_TITLE = "GamePilot"
API_VERSION = "0.1.0"


def create_app(repository: SessionRepository | None = None) -> FastAPI:
    """创建 FastAPI 应用；默认使用进程内内存仓储。"""
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description="游戏智能测试 Agent 平台的确定性回合制战斗靶场（第一阶段）。",
    )
    app.state.repository = repository if repository is not None else InMemorySessionRepository()
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()
