"""本地 Agent 自动测试入口；浏览器不直接调用模型或选择游戏会话工厂。"""

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from gamepilot.agent.goals import GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY
from gamepilot.lab.profiles import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_NORMAL,
)
from gamepilot.web_runs import WEB_BUDGET, AgentWebRunManager, real_key

router = APIRouter(prefix="/api/v1/agent-test-runs", tags=["Agent 自动测试"])


class StartAgentTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    goal_id: Literal[GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY]
    seed: int | None = Field(default=None, ge=-(2**53 - 1), le=2**53 - 1)
    profile: Literal[
        PROFILE_NORMAL,
        FAULT_POTION_OVERHEAL,
        FAULT_POTION_NOT_CONSUMED,
        FAULT_RETALIATE_AFTER_DEATH,
    ] = PROFILE_NORMAL
    provider_mode: Literal["offline", "real"] = "offline"
    paid_ack: bool = False


def get_manager(request: Request) -> AgentWebRunManager:
    return request.app.state.agent_run_manager  # type: ignore[no-any-return]


@router.get("/config")
def config() -> dict[str, object]:
    return {
        "real_available": real_key() is not None,
        "budget": WEB_BUDGET.model_dump(),
    }


@router.post("", status_code=202)
async def start_run(
    payload: StartAgentTestRequest,
    manager: Annotated[AgentWebRunManager, Depends(get_manager)],
) -> dict[str, object]:
    if payload.provider_mode == "real" and not payload.paid_ack:
        raise HTTPException(status_code=400, detail="真实模型测试须单独确认付费范围")
    if payload.provider_mode == "offline" and payload.paid_ack:
        raise HTTPException(status_code=400, detail="离线演练不接受付费确认")
    config = payload.model_dump()
    if config["seed"] is None:
        config["seed"] = secrets.randbits(31)
    try:
        return await manager.start(config)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/{run_id}")
def get_run(
    run_id: str, manager: Annotated[AgentWebRunManager, Depends(get_manager)]
) -> dict[str, object]:
    result = manager.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="自动测试运行不存在")
    return result


@router.get("/{run_id}/report")
def get_report(
    run_id: str, manager: Annotated[AgentWebRunManager, Depends(get_manager)]
) -> dict[str, object]:
    result = manager.report(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="运行报告尚未生成或运行不存在")
    return result
