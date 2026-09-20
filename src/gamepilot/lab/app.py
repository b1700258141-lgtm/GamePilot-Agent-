"""独立组装的缺陷靶场应用。

与正常服务的关系：

- 复用同一份路由与异常处理（`api.routes` / `api.errors`），因此 HTTP 路径、
  响应结构与错误码完全一致；
- 显式创建自己的 `InMemorySessionRepository`，与正常服务的内存仓储互不影响；
- 只在本应用实例上覆盖 `session_factory`，把新会话引到指定的 profile。

明确不做的事：

- 不导入 `gamepilot.main`——那个模块在导入时就会执行模块级 `app = create_app()`，
  会读取正常配置并可能创建数据库 Engine；
- 不读取 `.env` 或任何 Settings，不支持按环境变量选择仓储；
- 不接受外部仓储、数据库连接串或可调用代码路径。

因此即使运行环境把 `REPOSITORY_BACKEND` 设成 `postgres`、`DATABASE_URL`
是无效值，靶场也只使用内存，不会创建数据库 Engine。
"""

from fastapi import FastAPI

from gamepilot.api.errors import register_exception_handlers
from gamepilot.api.routes import router
from gamepilot.repositories.memory import InMemorySessionRepository

from .profiles import FaultProfile, resolve_profile
from .recorder import TriggerRecorder
from .session import make_session_factory

LAB_API_TITLE = "GamePilot 缺陷靶场"
LAB_API_VERSION = "0.1.0"
LAB_API_DESCRIPTION = (
    "固定一种可控缺陷的独立内存靶场，仅用于本地测试；不连接数据库，也不提供切换缺陷的接口。"
)


def create_lab_app(profile: str, *, recorder: TriggerRecorder | None = None) -> FastAPI:
    """创建一个只跑指定 profile 的靶场应用。

    `recorder` 由宿主提供时，触发记录会写进调用方持有的对象里，
    评测器据此核对标准答案；不提供时应用内部自建一个（`serve` 用）。
    """
    resolved = resolve_profile(profile)
    trigger_recorder = recorder if recorder is not None else TriggerRecorder()

    app = FastAPI(
        title=LAB_API_TITLE,
        version=LAB_API_VERSION,
        description=LAB_API_DESCRIPTION,
    )
    app.state.repository = InMemorySessionRepository()
    app.state.session_factory = make_session_factory(resolved, trigger_recorder)
    # 下面两项只供宿主（CLI、评测器、测试）自省，不参与任何 HTTP 响应，
    # 也不出现在 OpenAPI 文档里；正常服务的应用没有这两个属性。
    app.state.lab_profile = resolved
    app.state.trigger_recorder = trigger_recorder
    register_exception_handlers(app)
    app.include_router(router)
    return app


def lab_profile_of(app: FastAPI) -> FaultProfile:
    """取出靶场应用的 profile；正常服务的应用没有这个属性时会明确报错。"""
    profile = getattr(app.state, "lab_profile", None)
    if not isinstance(profile, FaultProfile):
        raise AttributeError("该应用不是缺陷靶场：没有 lab_profile")
    return profile
