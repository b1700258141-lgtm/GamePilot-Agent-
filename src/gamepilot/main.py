"""FastAPI 应用入口：组装路由、异常处理与仓储。

使用应用工厂 create_app，便于测试中注入独立的仓储实例；
模块级 app 供 uvicorn 启动使用。

仓储后端由配置决定（`REPOSITORY_BACKEND`，默认 `memory`）：

- `memory`：进程内内存仓储，不需要数据库配置；
- `postgres`：由 lifespan 创建 Engine 与 Session Factory，并在关闭时 dispose。

显式传入 `repository` 时以注入为准：不读取后端配置、不创建 Engine，
注入对象的资源由调用方负责释放。

应用启动不会自动执行迁移，也不使用 `metadata.create_all()`；
PostgreSQL 后端必须在启动前显式执行 `alembic upgrade head`。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .api.agent_test_routes import router as agent_test_router
from .api.errors import register_exception_handlers
from .api.preview_routes import router as preview_router
from .api.routes import router
from .config import RepositoryBackend, get_settings, require_database_url
from .domain.combat import create_combat_session
from .persistence.database import create_db_engine, create_session_factory
from .persistence.errors import PersistenceUnavailableError
from .preview_visitors import PreviewVisitorManager
from .repositories.base import SessionRepository
from .repositories.memory import InMemorySessionRepository
from .repositories.postgres import PostgresSessionRepository
from .web_runs import AgentWebRunManager, public_preview_enabled

API_TITLE = "GamePilot"
API_VERSION = "0.1.0"


def create_app(repository: SessionRepository | None = None) -> FastAPI:
    """创建 FastAPI 应用；默认按配置选择仓储后端，默认后端是内存仓储。"""
    engine: Engine | None = None
    if repository is not None:
        resolved: SessionRepository = repository
        agent_db_factory = None
    else:
        settings = get_settings()
        if settings.repository_backend is RepositoryBackend.POSTGRES:
            engine = create_db_engine(require_database_url())
            agent_db_factory = create_session_factory(engine)
            resolved = PostgresSessionRepository(agent_db_factory)
        else:
            agent_db_factory = None
            resolved = InMemorySessionRepository()

    if public_preview_enabled() and not isinstance(resolved, InMemorySessionRepository):
        raise ValueError("公开预览须使用内存仓储，避免访客数据写入持久化数据库")
    run_manager = AgentWebRunManager(agent_db_factory)
    visitors = PreviewVisitorManager(run_manager) if public_preview_enabled() else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if engine is not None:
                _probe_database(engine)
            if visitors is not None:
                visitors.start()
            yield
        finally:
            if visitors is not None:
                await visitors.stop()
            if engine is not None:
                # 应用创建的 Engine 由应用释放；注入对象的资源归调用方所有。
                engine.dispose()

    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description="游戏智能测试 Agent 平台的确定性回合制战斗靶场（第一阶段）。",
        lifespan=lifespan,
    )
    app.state.repository = resolved
    app.state.agent_run_manager = run_manager
    app.state.preview_visitors = visitors

    # 注入仓储时不推断其底层类型；由调用方持有的依赖不在此处探测。
    def readiness_probe() -> None:
        if engine is not None:
            _probe_database(engine, startup=False)

    app.state.readiness_probe = readiness_probe
    # 正常服务固定使用领域层的会话工厂；缺陷靶场由自己的装配覆盖它。
    app.state.session_factory = create_combat_session
    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(agent_test_router)
    app.include_router(preview_router)

    # 本地手动测试页与 API 同源，无需额外前端服务或跨域配置。
    app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    return app


def _probe_database(engine: Engine, *, startup: bool = True) -> None:
    """探测连接；启动和运行期就绪检查共用同一安全错误边界。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        # Lifespan 异常由服务器记录。隐藏原始异常链，避免连接参数或 SQL
        # 进入服务器日志；对外只保留稳定、安全的启动错误。
        message = "database is not reachable at startup" if startup else "database is not reachable"
        raise PersistenceUnavailableError(message) from None


app = create_app()
