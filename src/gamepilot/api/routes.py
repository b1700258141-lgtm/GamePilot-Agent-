"""HTTP 路由层：只做参数解析与调用，游戏规则全部在领域层。

依赖方向：API 层 → 领域层 → 仓储抽象；路由不包含任何
伤害计算、状态转换或随机逻辑。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from gamepilot.domain.combat import create_combat_session
from gamepilot.domain.models import GameSnapshot
from gamepilot.repositories.base import SessionRepository

from .schemas import ActionRequest, CreateSessionRequest, ErrorResponse

router = APIRouter()

# OpenAPI 错误声明；实际映射见 api/errors.py。
_NOT_FOUND_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "游戏会话不存在（session_not_found）",
}
_STATE_CONFLICT_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "动作与当前战斗状态冲突，或存储历史与本次写入冲突",
}
_PERSISTENCE_CONFLICT_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "存储中的历史与本次写入冲突（persistence_conflict）",
}
_PERSISTENCE_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {
        "model": ErrorResponse,
        "description": "存储数据无法确定性恢复，或其他内部错误（persistence_inconsistent）",
    },
    503: {
        "model": ErrorResponse,
        "description": "数据库暂时不可用（persistence_unavailable）",
    },
}


def get_repository(request: Request) -> SessionRepository:
    """从应用状态取出仓储实例（由 create_app 注入）。"""
    return request.app.state.repository


@router.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    """存活探针：服务是否可用。"""
    return {"status": "ok"}


@router.post(
    "/api/v1/game-sessions",
    status_code=201,
    response_model=GameSnapshot,
    responses={409: _PERSISTENCE_CONFLICT_RESPONSE, **_PERSISTENCE_RESPONSES},
    summary="创建游戏会话",
)
def create_session(
    payload: CreateSessionRequest,
    repository: Annotated[SessionRepository, Depends(get_repository)],
) -> GameSnapshot:
    """创建一场玩家对史莱姆的战斗，返回完整初始快照。"""
    session = create_combat_session(seed=payload.seed)
    repository.save(session)
    return session.snapshot()


@router.get(
    "/api/v1/game-sessions/{session_id}",
    response_model=GameSnapshot,
    responses={404: _NOT_FOUND_RESPONSE, **_PERSISTENCE_RESPONSES},
    summary="查询会话状态",
)
def get_session(
    session_id: str,
    repository: Annotated[SessionRepository, Depends(get_repository)],
) -> GameSnapshot:
    """返回会话当前完整快照；不存在时由异常处理器返回 HTTP 404。"""
    return repository.get(session_id).snapshot()


@router.post(
    "/api/v1/game-sessions/{session_id}/actions",
    response_model=GameSnapshot,
    responses={
        404: _NOT_FOUND_RESPONSE,
        409: _STATE_CONFLICT_RESPONSE,
        **_PERSISTENCE_RESPONSES,
    },
    summary="执行动作",
)
def perform_action(
    session_id: str,
    payload: ActionRequest,
    repository: Annotated[SessionRepository, Depends(get_repository)],
) -> GameSnapshot:
    """执行 attack 或 use_potion；领域冲突由异常处理器返回 HTTP 409。"""
    session = repository.get(session_id)
    if payload.action == "attack":
        session.attack()
    else:
        session.use_potion()
    repository.save(session)
    return session.snapshot()
