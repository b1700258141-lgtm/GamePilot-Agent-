"""本地 Agent 自动测试入口；浏览器不直接调用模型或选择游戏会话工厂。"""

import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamepilot.agent.goals import GOAL_BATTLE_END, GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY
from gamepilot.agent.skills import DEFAULT_SKILL_ID
from gamepilot.lab.profiles import (
    FAULT_IDS,
    FAULT_LEVEL_ANOMALY,
    FAULT_LOOT_ANOMALY,
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    FAULT_SKILL_POWER_ANOMALY,
    PROFILE_NORMAL,
)
from gamepilot.lab.selection import RANDOM_FAULT_PERCENT
from gamepilot.web_runs import (
    PREVIEW_ACCESS_MESSAGE,
    WEB_BUDGET,
    AgentWebRunManager,
    public_preview_enabled,
    real_key,
)

router = APIRouter(prefix="/api/v1/agent-test-runs", tags=["Agent 自动测试"])


class StartAgentTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    goal_id: Literal[GOAL_BATTLE_END, GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY] = (
        GOAL_BATTLE_END
    )
    skill_id: str = DEFAULT_SKILL_ID
    seed: int | None = Field(default=None, ge=-(2**53 - 1), le=2**53 - 1)
    battle_count: int = Field(default=1, ge=1, le=6)
    profile: (
        Literal[
            PROFILE_NORMAL,
            FAULT_POTION_OVERHEAL,
            FAULT_POTION_NOT_CONSUMED,
            FAULT_RETALIATE_AFTER_DEATH,
            FAULT_LOOT_ANOMALY,
            FAULT_LEVEL_ANOMALY,
            FAULT_SKILL_POWER_ANOMALY,
        ]
        | None
    ) = None
    forced_faults: list[
        Literal[
            FAULT_POTION_OVERHEAL,
            FAULT_POTION_NOT_CONSUMED,
            FAULT_RETALIATE_AFTER_DEATH,
            FAULT_LOOT_ANOMALY,
            FAULT_LEVEL_ANOMALY,
            FAULT_SKILL_POWER_ANOMALY,
        ]
    ] = Field(default_factory=list, max_length=6)
    provider_mode: Literal["offline", "real"] = "offline"
    paid_ack: bool = False

    @field_validator("forced_faults")
    @classmethod
    def distinct_faults(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("不能重复勾选同一缺陷")
        return value

    @model_validator(mode="after")
    def one_fault_configuration(self) -> "StartAgentTestRequest":
        if self.profile is not None and self.forced_faults:
            raise ValueError("profile 与 forced_faults 不能同时指定")
        return self


def get_manager(request: Request) -> AgentWebRunManager:
    return request.app.state.agent_run_manager  # type: ignore[no-any-return]


@router.get("/config")
def config() -> dict[str, object]:
    return {
        "real_available": real_key() is not None,
        "real_access_request_only": public_preview_enabled(),
        "budget": WEB_BUDGET.model_dump(),
        "random_fault_percent": RANDOM_FAULT_PERCENT,
        "fault_ids": FAULT_IDS,
    }


class AddSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4000)

    @field_validator("name", "instructions")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("skill 名称和内容不能为空")
        return value


@router.get("/skills")
def list_skills(
    manager: Annotated[AgentWebRunManager, Depends(get_manager)],
) -> list[dict[str, object]]:
    skills = manager.skills.list()
    if public_preview_enabled():
        skills = [skill for skill in skills if skill.built_in]
    return [skill.model_dump() for skill in skills]


@router.post("/skills", status_code=201)
def add_skill(
    payload: AddSkillRequest,
    manager: Annotated[AgentWebRunManager, Depends(get_manager)],
) -> dict[str, object]:
    if public_preview_enabled():
        raise HTTPException(status_code=403, detail=PREVIEW_ACCESS_MESSAGE)
    try:
        return manager.skills.add(payload.name, payload.instructions).model_dump()
    except OSError:
        raise HTTPException(status_code=503, detail="无法保存 skill，请检查本地存储") from None


@router.post("", status_code=202)
async def start_run(
    payload: StartAgentTestRequest,
    manager: Annotated[AgentWebRunManager, Depends(get_manager)],
) -> dict[str, object]:
    if payload.provider_mode == "real" and public_preview_enabled():
        raise HTTPException(status_code=403, detail=PREVIEW_ACCESS_MESSAGE)
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
