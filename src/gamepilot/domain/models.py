"""领域数据模型：战斗状态、结构化事件与游戏快照。

这些模型只描述数据，不包含任何状态转换规则；规则集中在 combat.py。
使用 Pydantic 是为了获得稳定的序列化与校验，不依赖任何 Web 框架。
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class BattleStatus(str, Enum):
    """战斗状态。"""

    ACTIVE = "active"
    WON = "won"
    LOST = "lost"


class CombatantState(BaseModel):
    """战斗单位状态。"""

    hp: int
    max_hp: int


class PlayerState(CombatantState):
    """玩家状态：在生命值之外还持有药水数量。"""

    potions: int


EventActor = Literal["player", "slime"]
EventKind = Literal["attack", "potion", "retaliate"]


class CombatEvent(BaseModel):
    """一条结构化战斗事件，供后续 Agent 观察与缺陷报告消费。

    `value` 的含义取决于 `kind`：
    - attack / retaliate：造成的伤害；
    - potion：实际恢复的生命值（可能被生命上限截断）。

    事件不携带时间戳，保证相同种子与相同动作序列下事件完全可比。
    """

    turn: int
    actor: EventActor
    kind: EventKind
    value: int
    player_hp: int
    slime_hp: int
    potions: int | None = None  # 事件结束后的剩余药水，仅玩家动作事件记录


class GameSnapshot(BaseModel):
    """游戏会话的完整快照，同时作为 API 的响应模型。"""

    session_id: str
    seed: int
    status: BattleStatus
    turn: int
    player: PlayerState
    slime: CombatantState
    events: list[CombatEvent] = Field(default_factory=list)
