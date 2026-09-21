"""Agent 评测清单：4 profile × 3 目标 = 12 组合的固定标准答案。

这是一套**明确标注的子集试验**（`docs/claude-tasks/TASK-003C-langgraph-test-agent.md`
第 7 节），不声称完成全部 32 组合的 Agent 对照。原 32 组合与七项指标保持不变，
继续作为确定性回归，不被重命名为 Agent 成绩。

清单与实现分离的意义：目标文本、脚本对照、预定缺陷机会与目标规则都**先于运行写死**。
Agent 的导入链读不到本模块，因此模型不可能沿导入链拿到标准答案；
反过来，实际结果与清单不符时也只能如实列出，不能改清单去迎合实现。

三个概念分得很清，不能互相顶替：

- **覆盖目标**：程序根据真实观测算出的确定性条件（`agent.goals`）；
- **发现缺陷**：独立判定器在某条规则上给出的 fail 结论；
- **真实触发**：靶场记录器在宿主侧记录的「这次会话确实走到了缺陷分支」。

评测只把「触发 + 命中目标规则」当成一次检出，不凭 profile 认定。
"""

from dataclasses import dataclass

from gamepilot.agent.goals import GOAL_CATALOG, GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY
from gamepilot.agent.models import BudgetSpec
from gamepilot.lab import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_CATALOG,
    PROFILES,
)
from gamepilot.testing.rules import (
    R_NO_RETALIATE_ON_KILL,
    R_POTION_CAP,
    R_POTION_DECREMENTS,
)

AGENT_BENCHMARK_VERSION = "1.2.0"
AGENT_MANIFEST_SOURCE = (
    "docs/claude-tasks/TASK-003C-langgraph-test-agent.md#7-最小评测12-组合配对试验"
)

# 全部 12 格统一使用既有对应场景的 seed=42，Agent 与脚本对照完全同参。
AGENT_SEED = 42

AGENT_GOAL_IDS: tuple[str, ...] = (GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY)

# 每个目标对应的**脚本对照**（对照组用原有 Scenario，不放宽也不改写）。
GOAL_SCRIPT_COUNTERPART: dict[str, str] = {
    GOAL_FULL_HEALTH: "potion-at-full-hp-rejected",
    GOAL_HEALING: "attack-then-potion",
    GOAL_VICTORY: "win-then-rejected",
}

# 预定缺陷机会：两个 healing 格 + 一个 victory 格。分母固定为 3，
# 未到达与执行错误都不缩小分母，只如实出现在明细里。
DESIGNATED_OPPORTUNITIES: tuple[tuple[str, str], ...] = (
    (FAULT_POTION_OVERHEAL, GOAL_HEALING),
    (FAULT_POTION_NOT_CONSUMED, GOAL_HEALING),
    (FAULT_RETALIATE_AFTER_DEATH, GOAL_VICTORY),
)

# 每个预定机会必须命中的规则编号；命中多条时按编号去重计数。
OPPORTUNITY_TARGET_RULES: dict[tuple[str, str], tuple[str, ...]] = {
    (FAULT_POTION_OVERHEAL, GOAL_HEALING): (R_POTION_CAP,),
    (FAULT_POTION_NOT_CONSUMED, GOAL_HEALING): (R_POTION_DECREMENTS,),
    (FAULT_RETALIATE_AFTER_DEATH, GOAL_VICTORY): (R_NO_RETALIATE_ON_KILL,),
}

# 12 格共用的预算：与 Agent 的保守默认值一致，评测不额外放宽。
AGENT_EVAL_BUDGET = BudgetSpec()


@dataclass(frozen=True)
class AgentCombination:
    """清单里的一格：某个 profile 上某个目标的预期。"""

    profile: str
    goal_id: str

    @property
    def goal(self) -> str:
        return GOAL_CATALOG[self.goal_id].goal

    @property
    def fault_id(self) -> str | None:
        """本格所属的缺陷编号（normal 列为 None）。"""
        return PROFILE_CATALOG[self.profile].fault_id

    @property
    def script_case_id(self) -> str:
        """对照组的脚本场景编号。"""
        return GOAL_SCRIPT_COUNTERPART[self.goal_id]

    @property
    def is_designated(self) -> bool:
        return (self.profile, self.goal_id) in OPPORTUNITY_TARGET_RULES

    @property
    def target_rules(self) -> tuple[str, ...]:
        return OPPORTUNITY_TARGET_RULES.get((self.profile, self.goal_id), ())


def _build_combinations() -> tuple[AgentCombination, ...]:
    return tuple(
        AgentCombination(profile=profile, goal_id=goal_id)
        for profile in PROFILES
        for goal_id in AGENT_GOAL_IDS
    )


AGENT_COMBINATIONS: tuple[AgentCombination, ...] = _build_combinations()

# normal 列的格数：误报指标的分母来自清单，不来自实际跑到的格数。
NORMAL_CELLS: tuple[AgentCombination, ...] = tuple(
    row for row in AGENT_COMBINATIONS if row.fault_id is None
)

# 变体列（会植入缺陷的格）。
VARIANT_CELLS: tuple[AgentCombination, ...] = tuple(
    row for row in AGENT_COMBINATIONS if row.fault_id is not None
)


__all__ = [
    "AGENT_BENCHMARK_VERSION",
    "AGENT_COMBINATIONS",
    "AGENT_EVAL_BUDGET",
    "AGENT_GOAL_IDS",
    "AGENT_MANIFEST_SOURCE",
    "AGENT_SEED",
    "DESIGNATED_OPPORTUNITIES",
    "GOAL_SCRIPT_COUNTERPART",
    "NORMAL_CELLS",
    "OPPORTUNITY_TARGET_RULES",
    "VARIANT_CELLS",
    "AgentCombination",
]
