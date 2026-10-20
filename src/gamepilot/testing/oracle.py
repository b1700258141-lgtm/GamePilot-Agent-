"""独立判定器：只读请求、预期、快照与事件。

边界（与任务单一致）：

- 不导入 `gamepilot.domain`：不调用 CombatSession，不重放实现，不读私有属性；
- 不访问仓储、恢复模块或数据库；
- 不复制随机数实现：伤害只校验「在规则区间内」，扣血只校验
  「事件里的数值与 HP 变化自洽」，而不是复算一个「正确答案」。

因此这里没有任何随机取值，所有判定都是确定性的比较。
判定结果带稳定 rule_id 与步骤位置，失败时同时保留期望与实际。
"""

from collections.abc import Sequence

from .models import ActionStep, HttpObservation, RuleCheck, SnapshotView, normalize_snapshot
from .rules import (
    ENEMY_DAMAGE_RANGES,
    ENEMY_EXP,
    ENEMY_GOLD,
    ENEMY_LEVELS,
    ENEMY_MAX_HP,
    EP_PER_LEVEL,
    HEAL_SPELL_BASE,
    HEAL_SPELL_COST,
    INITIAL_EP,
    INITIAL_MAGIC,
    INITIAL_POTIONS,
    PLAYER_DAMAGE_MAX,
    PLAYER_DAMAGE_MIN,
    PLAYER_MAX_HP,
    POTION_HEAL,
    POWER_STRIKE_BONUS,
    POWER_STRIKE_COOLDOWN,
    R_ATTACK_DAMAGE,
    R_ATTACK_HP_APPLY,
    R_BATTLE_ADVANCE,
    R_CONTROL_MATCH,
    R_CREATE_GET_IDENTICAL,
    R_CREATE_SEED,
    R_CREATE_STATUS,
    R_EP_COST,
    R_EVENT_ACTOR_ORDER,
    R_EVENT_COUNT,
    R_EVENT_TURN,
    R_EXP_BALANCE,
    R_EXP_REWARD,
    R_EXPECTED_CODE,
    R_EXPECTED_STATUS,
    R_FINAL_STATUS,
    R_GET_STATUS,
    R_HISTORY_PREFIX,
    R_INIT_STATE,
    R_INVARIANTS,
    R_LAST_EVENT_MATCHES_SNAPSHOT,
    R_LEVEL_UP,
    R_LOOT_BALANCE,
    R_LOOT_ITEM,
    R_LOOT_PRESENT,
    R_LOOT_RANGE,
    R_MAGIC_GROWTH,
    R_MAGIC_HEAL,
    R_NO_ACTION_AFTER_PLAYER_DEATH,
    R_NO_RETALIATE_ON_KILL,
    R_POTION_CAP,
    R_POTION_DECREMENTS,
    R_POTION_HEAL,
    R_REJECT_NO_STATE_CHANGE,
    R_RETALIATE_ONCE,
    R_RETALIATE_POTIONS_NULL,
    R_SKILL_COOLDOWN,
    R_SKILL_POWER,
    R_STATUS_CONSISTENT,
    R_TURN_INCREMENT,
    RULE_CATALOG,
    SPELL_DAMAGE,
)


def _fmt(value: object) -> str:
    """把期望/实际值渲染成报告里可读的短字符串。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key}: {_fmt(item)}" for key, item in value.items()) + "}"
    return str(value)


def _experience_outcome(level: int, exp: int, gained: int, ruleset: str) -> tuple[int, int]:
    """独立按公开门槛计算升级次数与余量，不读取游戏实现。"""
    remaining = exp + gained
    levels = 0
    threshold = 15 if ruleset != "v5" else 15 * level
    while remaining >= threshold:
        remaining -= threshold
        levels += 1
        threshold = 15 if ruleset != "v5" else 15 * (level + levels)
    return levels, remaining


def _equality_rule(rule_id: str, step: int | None, expected: object, actual: object) -> RuleCheck:
    """按「期望 == 实际」判定一条规则。"""
    passed = expected == actual
    return RuleCheck(
        rule_id=rule_id,
        status="pass" if passed else "fail",
        expected=_fmt(expected),
        actual=_fmt(actual),
        message="" if passed else RULE_CATALOG.get(rule_id, ""),
        step=step,
    )


def _boolean_rule(
    rule_id: str, step: int | None, passed: bool, expected: str, actual: str, message: str = ""
) -> RuleCheck:
    """按一个已经算好的布尔结论判定一条规则，期望/实际由调用方给出。"""
    return RuleCheck(
        rule_id=rule_id,
        status="pass" if passed else "fail",
        expected=expected,
        actual=actual,
        message="" if passed else (message or RULE_CATALOG.get(rule_id, "")),
        step=step,
    )


def _same_rule(
    rule_id: str, step: int | None, left: object, right: object, subject: str, message: str = ""
) -> RuleCheck:
    """判定「两侧必须相同」，但结论文本里不写运行期取值。

    session_id 每次运行都不同：把它写进 expected/actual 会让同一场景的重跑
    必然产生差异，重跑就失去意义。取值本身留在前后快照里，结论只说是否相同。
    """
    passed = left == right
    return _boolean_rule(
        rule_id,
        step,
        passed,
        f"{subject}必须相同",
        "相同" if passed else "不同（见前后快照）",
        message,
    )


# ------------------------------------------------------------------ 创建与初始


def check_create(observation: HttpObservation, step: int | None = None) -> list[RuleCheck]:
    """创建会话的接口预期：HTTP 201。"""
    return [
        _equality_rule(R_CREATE_STATUS, step, 201, observation.status_code),
    ]


def check_seed(snapshot: SnapshotView, expected_seed: int) -> list[RuleCheck]:
    return [_equality_rule(R_CREATE_SEED, None, expected_seed, snapshot.seed)]


def check_get(observation: HttpObservation, step: int | None = None) -> list[RuleCheck]:
    return [_equality_rule(R_GET_STATUS, step, 200, observation.status_code)]


def check_initial_state(created: SnapshotView, fetched: SnapshotView) -> list[RuleCheck]:
    """初始状态规则，以及「创建后 GET 与创建响应一致」。"""
    checks = [
        _equality_rule(R_INIT_STATE, None, PLAYER_MAX_HP, created.player.hp),
        _equality_rule(R_INIT_STATE, None, PLAYER_MAX_HP, created.player.max_hp),
        _equality_rule(
            R_INIT_STATE,
            None,
            2 if created.ruleset == "v1" else INITIAL_POTIONS,
            created.player.potions,
        ),
        _equality_rule(R_INIT_STATE, None, 0, created.player.gold),
        _equality_rule(R_INIT_STATE, None, 1, created.player.level),
        _equality_rule(R_INIT_STATE, None, 0, created.player.exp),
        _equality_rule(R_INIT_STATE, None, INITIAL_EP, created.player.ep),
        _equality_rule(R_INIT_STATE, None, INITIAL_EP, created.player.max_ep),
        _equality_rule(R_INIT_STATE, None, INITIAL_MAGIC, created.player.magic),
        _equality_rule(R_INIT_STATE, None, 0, created.player.power_strike_cooldown),
        _equality_rule(R_INIT_STATE, None, 1, created.battle_index),
        _boolean_rule(
            R_INIT_STATE,
            None,
            1 <= created.battle_count <= 6,
            "battle_count in [1, 6]",
            str(created.battle_count),
        ),
        _equality_rule(
            R_INIT_STATE,
            None,
            ENEMY_LEVELS.get(created.enemy_type),
            created.enemy_level,
        ),
        _boolean_rule(
            R_INIT_STATE,
            None,
            created.enemy_type in ENEMY_MAX_HP,
            "known enemy type",
            created.enemy_type,
        ),
        _equality_rule(R_INIT_STATE, None, ENEMY_MAX_HP.get(created.enemy_type), created.slime.hp),
        _equality_rule(
            R_INIT_STATE, None, ENEMY_MAX_HP.get(created.enemy_type), created.slime.max_hp
        ),
        _equality_rule(R_INIT_STATE, None, "active", created.status),
        _equality_rule(R_INIT_STATE, None, 0, created.turn),
        _equality_rule(R_INIT_STATE, None, [], created.events),
        _same_rule(
            R_CREATE_GET_IDENTICAL,
            None,
            created,
            fetched,
            "创建响应与随后 GET 的完整状态",
        ),
    ]
    return checks


# ---------------------------------------------------------------------- 单步


def check_step(
    step: int,
    plan: ActionStep,
    before: SnapshotView,
    observation: HttpObservation,
    after: SnapshotView | None,
) -> list[RuleCheck]:
    """一次成功动作的接口预期与规则判定。

    治疗后的顺序在这里被显式处理：喝药事件记录的是**反击前**的 HP，
    因此治疗量按动作前快照计算、反击按药水事件后的 HP 计算，
    不能拿动作前后的最终 HP 差当作治疗量。
    """
    checks = []
    if before.status == "lost":
        checks.append(
            _boolean_rule(
                R_NO_ACTION_AFTER_PLAYER_DEATH,
                step,
                observation.status_code == 409,
                "玩家死亡后动作返回 409",
                f"HTTP {observation.status_code}",
            )
        )
    checks.append(
        _equality_rule(R_EXPECTED_STATUS, step, plan.expected_status, observation.status_code)
    )
    if plan.expected_code is not None:
        checks.append(
            _equality_rule(R_EXPECTED_CODE, step, plan.expected_code, observation.error_code)
        )
    if after is None:
        return checks

    new_events = after.events[len(before.events) :]
    checks.extend(_invariant_checks(step, before, after))
    checks.append(_equality_rule(R_TURN_INCREMENT, step, before.turn + 1, after.turn))
    checks.append(
        _equality_rule(R_HISTORY_PREFIX, step, before.events, after.events[: len(before.events)])
    )
    checks.append(
        _boolean_rule(
            R_EVENT_TURN,
            step,
            all(event.turn == after.turn for event in new_events),
            f"all new events turn == {after.turn}",
            _fmt([event.turn for event in new_events]),
        )
    )

    if plan.action == "next_battle":
        checks.extend(_advance_checks(step, before, after, new_events))
    elif plan.action in ("attack", "power_strike", "cast_spark", "cast_flame", "cast_meteor"):
        checks.extend(_attack_checks(step, before, after, new_events, plan.action))
        if before.ruleset not in ("v3", "v4", "v5") or after.slime.hp != 0:
            checks.append(
                _equality_rule(R_ATTACK_HP_APPLY, step, before.player.potions, after.player.potions)
            )
    elif plan.action == "cast_heal":
        checks.extend(_heal_spell_checks(step, before, after, new_events))
    else:
        checks.extend(_potion_checks(step, before, new_events))
        checks.append(
            _equality_rule(
                R_POTION_DECREMENTS, step, before.player.potions - 1, after.player.potions
            )
        )

    if plan.action != "next_battle":
        checks.extend(_resource_checks(step, plan.action, before, after))

    events_in_bounds = all(
        0 <= event.player_hp <= after.player.max_hp
        and 0 <= event.slime_hp <= max(before.slime.max_hp, after.slime.max_hp)
        and (event.potions is None or event.potions >= 0)
        for event in new_events
    )
    checks.append(
        _boolean_rule(
            R_INVARIANTS,
            step,
            events_in_bounds,
            "事件 HP 在上下限内且药水数量非负",
            "正常" if events_in_bounds else "事件状态越界",
        )
    )
    checks.extend(_status_checks(step, after))
    checks.extend(_last_event_checks(step, after))
    return checks


def _attack_checks(
    step: int, before: SnapshotView, after: SnapshotView, new_events: Sequence, action: str
) -> list[RuleCheck]:
    if before.ruleset in ("v3", "v4", "v5"):
        return _v3_attack_checks(step, before, after, new_events, action)
    if not new_events:
        return [
            _boolean_rule(
                R_EVENT_ACTOR_ORDER,
                step,
                False,
                "player attack event first",
                "no new events",
                "成功动作必须产生新事件",
            )
        ]
    attack = new_events[0]
    enemy_down = attack.slime_hp == 0
    drop_expected = (
        enemy_down and before.ruleset == "v2" and before.enemy_type in ("slime", "goblin")
    )
    expected_count = (2 if drop_expected else 1) if enemy_down else 2
    checks = [
        _boolean_rule(
            R_EVENT_ACTOR_ORDER,
            step,
            attack.actor == "player" and attack.kind == "attack",
            "first new event: actor=player kind=attack",
            f"actor={attack.actor} kind={attack.kind}",
        ),
        _boolean_rule(
            R_ATTACK_DAMAGE,
            step,
            PLAYER_DAMAGE_MIN <= attack.value <= PLAYER_DAMAGE_MAX,
            f"damage in [{PLAYER_DAMAGE_MIN}, {PLAYER_DAMAGE_MAX}]",
            str(attack.value),
        ),
        _boolean_rule(
            R_ATTACK_HP_APPLY,
            step,
            attack.slime_hp == max(0, before.slime.hp - attack.value)
            and attack.player_hp == before.player.hp
            and attack.potions == before.player.potions,
            f"slime.hp = max(0, {before.slime.hp} - damage), "
            f"player.hp = {before.player.hp}, potions = {before.player.potions}",
            f"slime.hp={attack.slime_hp} player.hp={attack.player_hp} potions={attack.potions}",
        ),
        _equality_rule(R_EVENT_COUNT, step, expected_count, len(new_events)),
    ]
    if enemy_down:
        checks.append(
            _boolean_rule(
                R_NO_RETALIATE_ON_KILL,
                step,
                not any(event.kind == "retaliate" for event in new_events[1:]),
                "敌人被击杀时本回合不反击",
                f"{len(new_events)} new events",
            )
        )
        if drop_expected:
            drops = [event for event in new_events[1:] if event.kind == "drop"]
            checks.append(_equality_rule(R_LOOT_PRESENT, step, 1, len(drops)))
            if drops:
                drop = drops[0]
                low, high = (1, 3) if before.enemy_type == "slime" else (10, 20)
                checks.extend(
                    [
                        _boolean_rule(
                            R_LOOT_ITEM, step, drop.item == "gold", "item=gold", str(drop.item)
                        ),
                        _boolean_rule(
                            R_LOOT_RANGE,
                            step,
                            low <= drop.value <= high,
                            f"{low} <= gold <= {high}",
                            str(drop.value),
                        ),
                        _equality_rule(
                            R_LOOT_BALANCE,
                            step,
                            before.player.gold + (drop.value if drop.item == "gold" else 0),
                            after.player.gold,
                        ),
                    ]
                )
    else:
        checks.extend(_retaliate_checks(step, before, new_events, previous_hp=attack.player_hp))
        checks.append(_equality_rule(R_LOOT_BALANCE, step, before.player.gold, after.player.gold))
    return checks


def _v3_attack_checks(
    step: int, before: SnapshotView, after: SnapshotView, new_events: Sequence, action: str
) -> list[RuleCheck]:
    if not new_events:
        return [_boolean_rule(R_EVENT_COUNT, step, False, "至少一条攻击事件", "无事件")]
    attack = new_events[0]
    bonus = (before.player.level - 1) * 10
    if action == "attack":
        low, high = PLAYER_DAMAGE_MIN + bonus, PLAYER_DAMAGE_MAX + bonus
        damage_rule = R_ATTACK_DAMAGE
    elif action == "power_strike":
        low = PLAYER_DAMAGE_MIN + bonus + POWER_STRIKE_BONUS
        high = PLAYER_DAMAGE_MAX + bonus + POWER_STRIKE_BONUS
        damage_rule = R_SKILL_POWER
    else:
        base_low, base_high = SPELL_DAMAGE[action][1]
        low, high = base_low + before.player.magic, base_high + before.player.magic
        damage_rule = R_SKILL_POWER
    defeated = attack.slime_hp == 0
    checks = [
        _boolean_rule(
            R_EVENT_ACTOR_ORDER,
            step,
            attack.actor == "player" and attack.kind == action,
            f"首条事件为玩家 {action}",
            f"{attack.actor}/{attack.kind}",
        ),
        _boolean_rule(
            damage_rule,
            step,
            low <= attack.value <= high,
            f"伤害在 {low}～{high}",
            str(attack.value),
        ),
        _boolean_rule(
            R_ATTACK_HP_APPLY,
            step,
            attack.slime_hp == max(0, before.slime.hp - attack.value)
            and attack.player_hp == before.player.hp
            and attack.potions == before.player.potions,
            "攻击事件按伤害扣敌人生命，玩家生命和药水不变",
            f"敌人 {attack.slime_hp} 玩家 {attack.player_hp} 药水 {attack.potions}",
        ),
        _equality_rule(R_BATTLE_ADVANCE, step, before.battle_index, after.battle_index),
    ]
    if not defeated:
        checks.extend(_retaliate_checks(step, before, new_events, previous_hp=attack.player_hp))
        checks.extend(
            [
                _equality_rule(R_EVENT_COUNT, step, 2, len(new_events)),
                _equality_rule(R_LOOT_BALANCE, step, before.player.gold, after.player.gold),
                _equality_rule(R_EXP_BALANCE, step, before.player.exp, after.player.exp),
                _equality_rule(R_LEVEL_UP, step, before.player.level, after.player.level),
                _equality_rule(R_LEVEL_UP, step, before.player.max_hp, after.player.max_hp),
                _equality_rule(R_LEVEL_UP, step, before.player.potions, after.player.potions),
            ]
        )
        return checks

    checks.append(
        _boolean_rule(
            R_NO_RETALIATE_ON_KILL,
            step,
            not any(event.kind == "retaliate" for event in new_events[1:]),
            "击杀后不反击",
            str([event.kind for event in new_events[1:]]),
        )
    )
    drops = [event for event in new_events[1:] if event.kind == "drop"]
    gold_expected = (
        before.enemy_type in ENEMY_GOLD
        if before.ruleset in ("v4", "v5")
        else before.enemy_type in ("slime", "goblin")
    )
    gold_drops = [event for event in drops if event.item != "exp"]
    checks.append(_equality_rule(R_LOOT_PRESENT, step, int(gold_expected), len(gold_drops)))
    if gold_drops:
        gold = gold_drops[0]
        low, high = ENEMY_GOLD.get(before.enemy_type, (10, 20))
        checks.extend(
            [
                _equality_rule(R_LOOT_ITEM, step, "gold", gold.item),
                _boolean_rule(
                    R_LOOT_RANGE,
                    step,
                    low <= gold.value <= high,
                    f"金币 {low}～{high}",
                    str(gold.value),
                ),
            ]
        )
    expected_gold = gold_drops[0].value if gold_drops and gold_drops[0].item == "gold" else 0
    checks.append(
        _equality_rule(R_LOOT_BALANCE, step, before.player.gold + expected_gold, after.player.gold)
    )
    exp_expected = ENEMY_EXP[before.enemy_type]
    exp_drops = [event for event in drops if event.item == "exp"]
    checks.append(
        _boolean_rule(
            R_EXP_REWARD,
            step,
            len(exp_drops) == 1 and exp_drops[0].value == exp_expected,
            f"恰好一次 exp 掉落，数量 {exp_expected}",
            str([(event.item, event.value) for event in drops]),
        )
    )
    levels, remainder = _experience_outcome(
        before.player.level, before.player.exp, exp_expected, before.ruleset
    )
    level_events = [event for event in new_events[1:] if event.kind == "level_up"]
    checks.extend(
        [
            _equality_rule(R_EXP_BALANCE, step, remainder, after.player.exp),
            _equality_rule(R_LEVEL_UP, step, before.player.level + levels, after.player.level),
            _equality_rule(
                R_LEVEL_UP, step, before.player.max_hp + 20 * levels, after.player.max_hp
            ),
            _equality_rule(
                R_LEVEL_UP,
                step,
                before.player.max_hp + 20 * levels if levels else before.player.hp,
                after.player.hp,
            ),
            _equality_rule(R_LEVEL_UP, step, before.player.potions + levels, after.player.potions),
            *(
                [
                    _equality_rule(
                        R_MAGIC_GROWTH,
                        step,
                        before.player.max_ep + EP_PER_LEVEL * levels,
                        after.player.max_ep,
                    ),
                    _equality_rule(
                        R_MAGIC_GROWTH, step, before.player.magic + 5 * levels, after.player.magic
                    ),
                ]
                if before.ruleset in ("v4", "v5")
                else []
            ),
            _equality_rule(
                R_LEVEL_UP,
                step,
                list(range(before.player.level + 1, before.player.level + levels + 1)),
                [event.value for event in level_events],
            ),
            _equality_rule(
                R_LEVEL_UP,
                step,
                [before.player.max_hp + 20 * index for index in range(1, levels + 1)],
                [event.player_hp for event in level_events],
            ),
            _equality_rule(
                R_LEVEL_UP,
                step,
                [before.player.potions + index for index in range(1, levels + 1)],
                [event.potions for event in level_events],
            ),
            _equality_rule(
                R_EVENT_COUNT, step, 1 + int(gold_expected) + 1 + levels, len(new_events)
            ),
        ]
    )
    return checks


def _advance_checks(
    step: int, before: SnapshotView, after: SnapshotView, new_events: Sequence
) -> list[RuleCheck]:
    event = new_events[0] if new_events else None
    enemy_hp = ENEMY_MAX_HP.get(after.enemy_type)
    return [
        _equality_rule(R_EVENT_COUNT, step, 1, len(new_events)),
        _boolean_rule(
            R_EVENT_ACTOR_ORDER,
            step,
            event is not None
            and event.actor == "player"
            and event.kind == "next_battle"
            and event.value == before.battle_index + 1,
            "玩家进入下一场事件",
            str(event),
        ),
        _boolean_rule(
            R_BATTLE_ADVANCE,
            step,
            before.status == "won"
            and before.battle_index < before.battle_count
            and after.status == "active"
            and after.battle_index == before.battle_index + 1
            and after.battle_count == before.battle_count
            and after.slime.hp == enemy_hp == after.slime.max_hp
            and after.enemy_level == ENEMY_LEVELS.get(after.enemy_type),
            "下一场开始，敌人按固定属性满血登场",
            str(after.battle_index),
        ),
        _boolean_rule(
            R_BATTLE_ADVANCE,
            step,
            before.player == after.player,
            "玩家生命、药水、金币、等级和经验完整继承",
            str(after.player),
        ),
    ]


def _potion_checks(step: int, before: SnapshotView, new_events: Sequence) -> list[RuleCheck]:
    if not new_events:
        return [
            _boolean_rule(
                R_EVENT_ACTOR_ORDER,
                step,
                False,
                "player potion event first",
                "no new events",
                "成功动作必须产生新事件",
            )
        ]
    potion = new_events[0]
    expected_heal = min(POTION_HEAL, before.player.max_hp - before.player.hp)
    expected_value = expected_heal if before.ruleset == "v1" else POTION_HEAL
    checks = [
        _boolean_rule(
            R_EVENT_ACTOR_ORDER,
            step,
            potion.actor == "player" and potion.kind == "potion",
            "first new event: actor=player kind=potion",
            f"actor={potion.actor} kind={potion.kind}",
        ),
        _boolean_rule(
            R_POTION_HEAL,
            step,
            potion.value == expected_value
            and potion.player_hp == before.player.hp + expected_heal
            and potion.slime_hp == before.slime.hp,
            f"药水效力 {expected_value}，实际回血 min({POTION_HEAL}, "
            f"{before.player.max_hp} - {before.player.hp}) = {expected_heal}",
            f"heal={potion.value} event player_hp={potion.player_hp}",
        ),
        _boolean_rule(
            R_POTION_CAP,
            step,
            potion.player_hp <= before.player.max_hp,
            f"药水事件 HP <= {before.player.max_hp}",
            str(potion.player_hp),
        ),
        _equality_rule(R_POTION_DECREMENTS, step, before.player.potions - 1, potion.potions),
        _equality_rule(R_EVENT_COUNT, step, 2, len(new_events)),
    ]
    # 喝药的反击按「治疗之后」的 HP 计算，不能拿动作前的 HP 算。
    checks.extend(_retaliate_checks(step, before, new_events, previous_hp=potion.player_hp))
    return checks


def _heal_spell_checks(
    step: int, before: SnapshotView, after: SnapshotView, new_events: Sequence
) -> list[RuleCheck]:
    if not new_events:
        return [_boolean_rule(R_EVENT_COUNT, step, False, "治疗魔法事件", "无事件")]
    spell = new_events[0]
    power = HEAL_SPELL_BASE + before.player.magic
    healed = min(power, before.player.max_hp - before.player.hp)
    checks = [
        _equality_rule(R_EVENT_COUNT, step, 2, len(new_events)),
        _boolean_rule(
            R_EVENT_ACTOR_ORDER,
            step,
            spell.actor == "player" and spell.kind == "cast_heal",
            "首条事件为玩家治疗魔法",
            f"{spell.actor}/{spell.kind}",
        ),
        _boolean_rule(
            R_MAGIC_HEAL,
            step,
            spell.value == power
            and spell.player_hp == before.player.hp + healed
            and spell.slime_hp == before.slime.hp
            and spell.potions == before.player.potions,
            f"威力 {power}，实际回血 {healed}，敌人和药水不变",
            f"威力 {spell.value}，事件生命 {spell.player_hp}",
        ),
        _equality_rule(R_BATTLE_ADVANCE, step, before.battle_index, after.battle_index),
    ]
    checks.extend(_retaliate_checks(step, before, new_events, previous_hp=spell.player_hp))
    return checks


def _resource_checks(
    step: int, action: str, before: SnapshotView, after: SnapshotView
) -> list[RuleCheck]:
    cooldown = (
        POWER_STRIKE_COOLDOWN
        if action == "power_strike"
        else max(0, before.player.power_strike_cooldown - 1)
        if before.ruleset in ("v3", "v4", "v5")
        else 0
    )
    checks = [_equality_rule(R_SKILL_COOLDOWN, step, cooldown, after.player.power_strike_cooldown)]
    if before.ruleset not in ("v4", "v5"):
        checks.extend(
            [
                _equality_rule(R_EP_COST, step, before.player.ep, after.player.ep),
                _equality_rule(R_MAGIC_GROWTH, step, before.player.magic, after.player.magic),
            ]
        )
        return checks
    cost = (
        HEAL_SPELL_COST
        if action == "cast_heal"
        else SPELL_DAMAGE[action][0]
        if action in SPELL_DAMAGE
        else 0
    )
    defeated = action in ("attack", "power_strike", *SPELL_DAMAGE) and after.slime.hp == 0
    levels = (
        _experience_outcome(
            before.player.level, before.player.exp, ENEMY_EXP[before.enemy_type], before.ruleset
        )[0]
        if defeated
        else 0
    )
    expected_max_ep = before.player.max_ep + EP_PER_LEVEL * levels
    expected_ep = expected_max_ep if levels else before.player.ep - cost
    checks.extend(
        [
            _equality_rule(R_EP_COST, step, expected_ep, after.player.ep),
            _equality_rule(R_MAGIC_GROWTH, step, expected_max_ep, after.player.max_ep),
            _equality_rule(
                R_MAGIC_GROWTH, step, before.player.magic + 5 * levels, after.player.magic
            ),
        ]
    )
    return checks


def _retaliate_checks(
    step: int, before: SnapshotView, new_events: Sequence, *, previous_hp: int
) -> list[RuleCheck]:
    if len(new_events) < 2:
        return [
            _boolean_rule(
                R_RETALIATE_ONCE,
                step,
                False,
                "敌人存活时恰好一次反击",
                f"only {len(new_events)} new events",
            )
        ]
    retaliate = new_events[1]
    low, high = (20, 35) if before.ruleset == "v1" else ENEMY_DAMAGE_RANGES[before.enemy_type]
    return [
        _boolean_rule(
            R_RETALIATE_ONCE,
            step,
            retaliate.actor == before.enemy_type
            and retaliate.kind == "retaliate"
            and low <= retaliate.value <= high
            and retaliate.player_hp == max(0, previous_hp - retaliate.value)
            and retaliate.slime_hp == new_events[0].slime_hp,
            f"{before.enemy_type} retaliate with damage in [{low}, {high}] "
            f"and player.hp = max(0, {previous_hp} - damage)",
            f"actor={retaliate.actor} kind={retaliate.kind} value={retaliate.value} "
            f"player_hp={retaliate.player_hp}",
        ),
        _equality_rule(R_RETALIATE_POTIONS_NULL, step, None, retaliate.potions),
    ]


def check_rejected_unchanged(
    step: int, before: SnapshotView, fetched: SnapshotView
) -> list[RuleCheck]:
    """预期 409 之后必须由 GET 证明状态与完整事件未变。"""
    return [
        _same_rule(
            R_REJECT_NO_STATE_CHANGE,
            step,
            before,
            fetched,
            "被拒绝动作前后的完整状态与事件",
        )
    ]


def check_final_status(step: int | None, snapshot: SnapshotView, expected: str) -> list[RuleCheck]:
    """场景结束时的状态必须符合场景声明（lost 也可能是通过）。"""
    return [_equality_rule(R_FINAL_STATUS, step, expected, snapshot.status)]


def check_control(
    main_status_codes: Sequence[int | None],
    main_snapshot: SnapshotView,
    control_status_codes: Sequence[int | None],
    control_snapshot: SnapshotView,
) -> list[RuleCheck]:
    """对照会话：同 seed 下状态、事件与响应码必须一致。"""
    return [
        _equality_rule(R_CONTROL_MATCH, None, list(main_status_codes), list(control_status_codes)),
        _equality_rule(
            R_CONTROL_MATCH,
            None,
            normalize_snapshot(main_snapshot),
            normalize_snapshot(control_snapshot),
        ),
    ]


# ------------------------------------------------------------------ 公共判定


def _invariant_checks(step: int, before: SnapshotView, after: SnapshotView) -> list[RuleCheck]:
    """不变量：HP 上下限、药水非负递减、身份字段不变。"""
    checks = [
        _same_rule(
            R_INVARIANTS, step, before.session_id, after.session_id, "动作前后的 session_id"
        ),
        _equality_rule(R_INVARIANTS, step, before.seed, after.seed),
        *(
            []
            if before.ruleset in ("v3", "v4", "v5")
            else [
                _equality_rule(R_INVARIANTS, step, before.player.max_hp, after.player.max_hp),
                _equality_rule(R_INVARIANTS, step, before.slime.max_hp, after.slime.max_hp),
            ]
        ),
        _boolean_rule(
            R_INVARIANTS,
            step,
            0 <= after.player.hp <= after.player.max_hp,
            f"0 <= player.hp <= {after.player.max_hp}",
            str(after.player.hp),
        ),
        _boolean_rule(
            R_INVARIANTS,
            step,
            0 <= after.slime.hp <= after.slime.max_hp,
            f"0 <= slime.hp <= {after.slime.max_hp}",
            str(after.slime.hp),
        ),
        _boolean_rule(
            R_INVARIANTS,
            step,
            0 <= after.player.potions
            and (
                before.ruleset in ("v3", "v4", "v5")
                or after.player.potions <= before.player.potions
            ),
            "药水非负，旧规则下只减不增",
            str(after.player.potions),
        ),
        _boolean_rule(
            R_INVARIANTS,
            step,
            after.player.level >= 1 and after.player.exp >= 0,
            "等级至少1且经验非负",
            f"等级{after.player.level}经验{after.player.exp}",
        ),
        _boolean_rule(
            R_INVARIANTS,
            step,
            0 <= after.player.ep <= after.player.max_ep and after.player.magic >= INITIAL_MAGIC,
            "EP 在 [0, max_ep] 且魔力不少于初始值",
            f"EP {after.player.ep}/{after.player.max_ep}，魔力 {after.player.magic}",
        ),
    ]
    return checks


def _status_checks(step: int | None, snapshot: SnapshotView) -> list[RuleCheck]:
    """状态与双方 HP 一致：won ⇔ 敌人 0，lost ⇔ 玩家 0，否则 active。"""
    if snapshot.status == "won":
        passed = snapshot.slime.hp == 0
    elif snapshot.status == "lost":
        passed = snapshot.player.hp == 0
    elif snapshot.status == "active":
        passed = snapshot.slime.hp > 0 and snapshot.player.hp > 0
    else:
        passed = False
    return [
        _boolean_rule(
            R_STATUS_CONSISTENT,
            step,
            passed,
            "status=won ⇒ slime.hp=0；status=lost ⇒ player.hp=0；status=active ⇒ 双方存活",
            f"status={snapshot.status} player.hp={snapshot.player.hp} slime.hp={snapshot.slime.hp}",
        )
    ]


def _last_event_checks(step: int | None, snapshot: SnapshotView) -> list[RuleCheck]:
    """最后事件的双方 HP 与最终快照一致。"""
    if not snapshot.events:
        return []
    last = snapshot.events[-1]
    return [
        _boolean_rule(
            R_LAST_EVENT_MATCHES_SNAPSHOT,
            step,
            last.player_hp == snapshot.player.hp and last.slime_hp == snapshot.slime.hp,
            f"last event hp = ({snapshot.player.hp}, {snapshot.slime.hp})",
            f"last event hp = ({last.player_hp}, {last.slime_hp})",
        )
    ]
