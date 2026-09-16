"""HTTP 路由层：只做参数解析与调用，游戏规则全部在领域层。

依赖方向：API 层 → 领域层 → 仓储抽象；路由不包含任何
伤害计算、状态转换或随机逻辑。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from gamepilot.domain.combat import create_combat_session
from gamepilot.domain.models import GameSnapshot
from gamepilot.repositories.base import SessionRepository

from .schemas import ActionRequest, CreateSessionRequest, ErrorResponse

router = APIRouter()


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
    responses={404: {"model": ErrorResponse, "description": "游戏会话不存在"}},
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
        404: {"model": ErrorResponse, "description": "游戏会话不存在"},
        409: {"model": ErrorResponse, "description": "动作与当前战斗状态冲突"},
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
