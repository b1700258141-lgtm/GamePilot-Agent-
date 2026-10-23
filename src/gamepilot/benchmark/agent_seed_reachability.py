"""穷举固定 Agent 动作预算内的缺陷可达性，仅供评测宿主使用。"""

from itertools import product
from typing import Literal

from pydantic import BaseModel, ConfigDict

from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.lab import PROFILE_CATALOG, LabCombatSession, TriggerRecorder

from .agent_manifest import AgentCombination

GameAction = Literal[
    "attack",
    "use_potion",
    "power_strike",
    "cast_meteor",
    "cast_flame",
    "cast_spark",
    "cast_heal",
    "next_battle",
]
GAME_ACTIONS: tuple[GameAction, ...] = (
    "attack",
    "use_potion",
    "power_strike",
    "cast_meteor",
    "cast_flame",
    "cast_spark",
    "cast_heal",
    "next_battle",
)


class ReachabilityCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    seed: int
    profile: str
    goal_id: str
    max_action_attempts: int
    reachable: bool
    witness_actions: list[GameAction]
    sequences_checked: int
    search_space_size: int


def search_fault_reachability(
    seed: int, combination: AgentCombination, *, max_action_attempts: int
) -> ReachabilityCase:
    """按长度和动作顺序穷举；拒绝动作也占一次尝试，但不改变状态。"""
    if not combination.is_designated:
        raise ValueError("可达性搜索只接受预定缺陷机会")
    if max_action_attempts < 1:
        raise ValueError("动作预算至少为 1")
    search_space_size = sum(len(GAME_ACTIONS) ** size for size in range(1, max_action_attempts + 1))
    checked = 0
    for size in range(1, max_action_attempts + 1):
        for actions in product(GAME_ACTIONS, repeat=size):
            checked += 1
            recorder = TriggerRecorder()
            session = LabCombatSession(
                session_id="reachability-probe",
                seed=seed,
                profile=PROFILE_CATALOG[combination.profile],
                recorder=recorder,
            )
            for action in actions:
                try:
                    if action.startswith("cast_"):
                        session.cast_spell(action)
                    else:
                        getattr(session, action)()
                except InvalidCombatAction:
                    pass
                if recorder.get(session.session_id) is not None:
                    return ReachabilityCase(
                        seed=seed,
                        profile=combination.profile,
                        goal_id=combination.goal_id,
                        max_action_attempts=max_action_attempts,
                        reachable=True,
                        witness_actions=list(actions),
                        sequences_checked=checked,
                        search_space_size=search_space_size,
                    )
    return ReachabilityCase(
        seed=seed,
        profile=combination.profile,
        goal_id=combination.goal_id,
        max_action_attempts=max_action_attempts,
        reachable=False,
        witness_actions=[],
        sequences_checked=checked,
        search_space_size=search_space_size,
    )
