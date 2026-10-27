"""测试目标与**确定性覆盖条件**。

目标只描述「要验证什么行为」，不描述动作顺序——顺序是模型自己的事。
但「目标是否达成」不能由模型自称：下面每个目标都有一组覆盖条件，
全部由已执行步骤的真实观测与通过的规则检查算出。

三条共同约定：

- 单纯 `finish` 不满足任何覆盖条件；
- 只观察到 409 不算覆盖，必须由 GET 证明状态与事件未变（R-REJECT-NO-STATE-CHANGE 通过）；
- 触发缺陷的运行会先在某条规则上失败并早停，覆盖条件自然不成立，
  于是「目标覆盖」与「发现缺陷」是两条独立证据。

这里只使用公开规则与公开观测，不包含 profile、缺陷编号或标准答案。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from gamepilot.testing.models import StepReport
from gamepilot.testing.rules import (
    R_POTION_CAP,
    R_POTION_DECREMENTS,
    R_POTION_HEAL,
    R_REJECT_NO_STATE_CHANGE,
)

from .models import GoalCondition, GoalCoverage

GOAL_FULL_HEALTH = "full-health"
GOAL_HEALING = "healing"
GOAL_VICTORY = "victory"
GOAL_BATTLE_END = "battle-end"

GOAL_IDS: tuple[str, ...] = (GOAL_FULL_HEALTH, GOAL_HEALING, GOAL_VICTORY)

COND_REJECTED_AT_FULL_HP = "rejected-at-full-hp"
COND_REJECTED_UNCHANGED = "rejected-unchanged"
COND_POTION_FROM_INJURED = "potion-from-injured"
COND_HEAL_AND_POTIONS_CHECKED = "heal-and-potions-checked"
COND_WON_OBSERVED = "won-observed"
COND_POST_VICTORY_REJECTED = "post-victory-rejected"
COND_BATTLE_ENDED = "battle-ended"
COND_POST_BATTLE_REJECTED = "post-battle-rejected"


def _passed(step: StepReport, rule_id: str) -> bool:
    return any(check.rule_id == rule_id and check.status == "pass" for check in step.checks)


def _full_health_conditions(steps: Sequence[StepReport]) -> list[GoalCondition]:
    used = [
        step
        for step in steps
        if step.action == "use_potion"
        and step.before.player.hp >= step.before.player.max_hp
        and step.observed_status == 200
    ]
    checked = [
        step
        for step in used
        if _passed(step, R_POTION_HEAL)
        and _passed(step, R_POTION_CAP)
        and _passed(step, R_POTION_DECREMENTS)
    ]
    return [
        GoalCondition(
            condition_id=COND_REJECTED_AT_FULL_HP,
            met=bool(used),
            evidence_step=used[0].index if used else None,
            detail=(
                f"第 {used[0].index} 步：满血使用药水成功"
                if used
                else "尚未在满血状态下成功使用药水"
            ),
        ),
        GoalCondition(
            condition_id=COND_REJECTED_UNCHANGED,
            met=bool(checked),
            evidence_step=checked[0].index if checked else None,
            detail=(
                f"第 {checked[0].index} 步：实际回血、血量上限和药水消耗均通过"
                if checked
                else "缺少满血喝药规则通过的证据"
            ),
        ),
    ]


def _healing_conditions(steps: Sequence[StepReport]) -> list[GoalCondition]:
    healed = [
        step
        for step in steps
        if step.action == "use_potion"
        and step.before.player.hp < step.before.player.max_hp
        and step.observed_status == 200
    ]
    checked = [
        step
        for step in healed
        if _passed(step, R_POTION_HEAL)
        and _passed(step, R_POTION_CAP)
        and _passed(step, R_POTION_DECREMENTS)
    ]
    return [
        GoalCondition(
            condition_id=COND_POTION_FROM_INJURED,
            met=bool(healed),
            evidence_step=healed[0].index if healed else None,
            detail=(
                f"第 {healed[0].index} 步：在受伤快照（{healed[0].before.player.hp}"
                f"/{healed[0].before.player.max_hp}）下使用药水并成功执行"
                if healed
                else "没有在受伤快照下成功执行过 use_potion"
            ),
        ),
        GoalCondition(
            condition_id=COND_HEAL_AND_POTIONS_CHECKED,
            met=bool(checked),
            evidence_step=checked[0].index if checked else None,
            detail=(
                f"第 {checked[0].index} 步：{R_POTION_HEAL}、{R_POTION_CAP}、"
                f"{R_POTION_DECREMENTS} 全部通过"
                if checked
                else f"缺少 {R_POTION_HEAL} / {R_POTION_CAP} / {R_POTION_DECREMENTS} 全部通过的一步"
            ),
        ),
    ]


def _victory_conditions(steps: Sequence[StepReport]) -> list[GoalCondition]:
    won_at: int | None = None
    for step in steps:
        if (
            step.after is not None
            and step.after.status == "won"
            and step.after.battle_index == step.after.battle_count
            and step.status == "pass"
        ):
            won_at = step.index
            break
    rejected_after_win = [
        step
        for step in steps
        if won_at is not None
        and step.index > won_at
        and step.observed_status == 409
        and step.observed_code == "battle_not_active"
        and _passed(step, R_REJECT_NO_STATE_CHANGE)
    ]

    return [
        GoalCondition(
            condition_id=COND_WON_OBSERVED,
            met=won_at is not None,
            evidence_step=won_at,
            detail=(
                f"第 {won_at} 步：观察到状态变为 won 且该步规则检查通过"
                if won_at is not None
                else "没有观察到一次规则检查通过的获胜回合"
            ),
        ),
        GoalCondition(
            condition_id=COND_POST_VICTORY_REJECTED,
            met=bool(rejected_after_win),
            evidence_step=rejected_after_win[0].index if rejected_after_win else None,
            detail=(
                f"第 {rejected_after_win[0].index} 步：获胜后动作被 409 battle_not_active 拒绝，"
                "且 GET 证明状态未变"
                if rejected_after_win
                else "获胜之后没有观察到「动作被拒且状态未变」的证据"
            ),
        ),
    ]


def _battle_end_conditions(steps: Sequence[StepReport]) -> list[GoalCondition]:
    ended = next(
        (
            step
            for step in steps
            if step.after is not None
            and step.after.status in ("won", "lost")
            and (step.after.status == "lost" or step.after.battle_index == step.after.battle_count)
            and step.status == "pass"
        ),
        None,
    )
    rejected = next(
        (
            step
            for step in steps
            if ended is not None
            and step.index > ended.index
            and step.observed_status == 409
            and step.observed_code == "battle_not_active"
            and _passed(step, R_REJECT_NO_STATE_CHANGE)
        ),
        None,
    )
    return [
        GoalCondition(
            condition_id=COND_BATTLE_ENDED,
            met=ended is not None,
            evidence_step=ended.index if ended else None,
            detail=(
                f"第 {ended.index} 步：观察到战斗状态 {ended.after.status}"
                if ended is not None and ended.after is not None
                else "尚未观察到玩家或敌人阵亡后的游戏终态"
            ),
        ),
        GoalCondition(
            condition_id=COND_POST_BATTLE_REJECTED,
            met=rejected is not None,
            evidence_step=rejected.index if rejected else None,
            detail=(
                f"第 {rejected.index} 步：终态后操作被拒且状态未变"
                if rejected
                else "尚未验证终态后不能继续行动"
            ),
        ),
    ]


@dataclass(frozen=True)
class GoalSpec:
    """一个测试目标：对外编号、给模型的目标文本与覆盖条件求值器。"""

    goal_id: str
    goal: str
    evaluate: Callable[[Sequence[StepReport]], list[GoalCondition]]


GOAL_CATALOG: dict[str, GoalSpec] = {
    GOAL_BATTLE_END: GoalSpec(
        goal_id=GOAL_BATTLE_END,
        goal="按照所选策略战斗至终态，并验证阵亡后不能继续行动",
        evaluate=_battle_end_conditions,
    ),
    GOAL_FULL_HEALTH: GoalSpec(
        goal_id=GOAL_FULL_HEALTH,
        goal="验证满血时使用药水：实际回血为零、消耗一瓶且生命不越界",
        evaluate=_full_health_conditions,
    ),
    GOAL_HEALING: GoalSpec(
        goal_id=GOAL_HEALING,
        goal="验证受伤后的治疗边界及药水资源变化",
        evaluate=_healing_conditions,
    ),
    GOAL_VICTORY: GoalSpec(
        goal_id=GOAL_VICTORY,
        goal="验证击败敌人的回合及战斗结束后的操作限制",
        evaluate=_victory_conditions,
    ),
}


class UnknownGoalError(ValueError):
    """非法的目标编号：明确报错，不退回默认目标。"""

    def __init__(self, goal_id: str) -> None:
        super().__init__(f"未知的测试目标：{goal_id!r}；合法取值：{', '.join(GOAL_IDS)}")
        self.goal_id = goal_id


def resolve_goal(goal_id: str) -> GoalSpec:
    try:
        return GOAL_CATALOG[goal_id]
    except KeyError:
        raise UnknownGoalError(goal_id) from None


def evaluate_coverage(goal_id: str, steps: Sequence[StepReport]) -> GoalCoverage:
    """按目标编号对已执行步骤求值；条件全部满足才算覆盖。"""
    spec = resolve_goal(goal_id)
    conditions = spec.evaluate(steps)
    return GoalCoverage(
        goal_id=spec.goal_id,
        goal=spec.goal,
        met=bool(conditions) and all(item.met for item in conditions),
        conditions=conditions,
    )


__all__ = [
    "COND_HEAL_AND_POTIONS_CHECKED",
    "COND_POTION_FROM_INJURED",
    "COND_POST_VICTORY_REJECTED",
    "COND_REJECTED_AT_FULL_HP",
    "COND_REJECTED_UNCHANGED",
    "COND_WON_OBSERVED",
    "GOAL_CATALOG",
    "GOAL_BATTLE_END",
    "GOAL_FULL_HEALTH",
    "GOAL_HEALING",
    "GOAL_IDS",
    "GOAL_VICTORY",
    "GoalSpec",
    "UnknownGoalError",
    "evaluate_coverage",
    "resolve_goal",
]
