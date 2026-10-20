"""动作预期推导：在请求之前，由公开规则与当前已核验快照算出预期结果。

Agent 只负责**选择动作**；这个动作的 `expected_status` / `expected_code`
必须由程序确定，不能由模型填写，也不能拿服务实际返回值反推。
本模块就是那条确定性规则：

- 依据只有公开规则（`docs/PHASE_1_PLAN.md#5-最小游戏规则`）与当前快照，
  不导入 `gamepilot.domain`，也不看任何缺陷标识或标准答案；
- 输入快照必须是已经通过初始校验的快照，因此「当前状态」是被验证过的事实。

409 的优先级来自公开 API 契约（`api.routes` → `domain.combat.use_potion`）：
战斗已结束优先于满血，满血优先于药水不足；`attack` 在 active 状态下永远合法。
本模块不复制领域实现，只按契约顺序枚举预期结果，并由测试锁定这一顺序。
"""

from .models import ActionName, ActionStep, SnapshotView
from .rules import (
    CODE_BATTLE_NOT_ACTIVE,
    CODE_BATTLE_NOT_WON,
    CODE_CAMPAIGN_COMPLETE,
    CODE_NO_POTIONS,
    CODE_NOT_ENOUGH_EP,
    CODE_PLAYER_FULL_HP,
    CODE_SKILL_COOLDOWN,
    CODE_SKILL_UNAVAILABLE,
    CODE_SPELL_UNAVAILABLE,
    HEAL_SPELL_COST,
    HTTP_CONFLICT,
    HTTP_OK,
    SPELL_DAMAGE,
)


def derive_expectation(snapshot: SnapshotView, action: ActionName) -> ActionStep:
    """按公开规则推导 `action` 在当前快照下的预期结果。

    推导只使用快照里的 `status` 与玩家 `hp`/`max_hp`/`potions`，
    不使用敌人生命值或事件历史——后者与这三种拒绝条件的判定无关。
    """
    if action == "next_battle":
        if snapshot.ruleset not in ("v3", "v4", "v5") or snapshot.status != "won":
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_BATTLE_NOT_WON
            )
        if snapshot.battle_index >= snapshot.battle_count:
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_CAMPAIGN_COMPLETE
            )
        return ActionStep(action=action, expected_status=HTTP_OK)
    if snapshot.status != "active":
        # 「战斗结束后不允许继续操作」，与具体是 won 还是 lost 无关。
        return ActionStep(
            action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_BATTLE_NOT_ACTIVE
        )
    if action == "attack":
        return ActionStep(action=action, expected_status=HTTP_OK, expected_code=None)
    if action == "power_strike":
        if snapshot.ruleset not in ("v3", "v4", "v5"):
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_SKILL_UNAVAILABLE
            )
        if snapshot.player.power_strike_cooldown:
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_SKILL_COOLDOWN
            )
        return ActionStep(action=action, expected_status=HTTP_OK)
    if action.startswith("cast_"):
        if snapshot.ruleset not in ("v4", "v5"):
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_SPELL_UNAVAILABLE
            )
        cost = HEAL_SPELL_COST if action == "cast_heal" else SPELL_DAMAGE[action][0]
        if snapshot.player.ep < cost:
            return ActionStep(
                action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_NOT_ENOUGH_EP
            )
        return ActionStep(action=action, expected_status=HTTP_OK)
    # 旧规则下满血拒绝；新规则下药水效力固定，但实际回血截断到生命上限。
    if snapshot.ruleset == "v1" and snapshot.player.hp >= snapshot.player.max_hp:
        return ActionStep(
            action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_PLAYER_FULL_HP
        )
    if snapshot.player.potions <= 0:
        return ActionStep(
            action=action, expected_status=HTTP_CONFLICT, expected_code=CODE_NO_POTIONS
        )
    return ActionStep(action=action, expected_status=HTTP_OK, expected_code=None)


__all__ = ["derive_expectation"]
