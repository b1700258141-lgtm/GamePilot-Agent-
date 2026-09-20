"""领域层规则扩展点的回归测试：默认实现必须与改动前的数值行为逐点一致。

这里的伤害值与状态不是从当前输出反推的，而是**改动前实测记录**下来的
（seed 固定时伤害序列完全确定）：任何一处随机数调用顺序、事件字段或
回合推进发生变化，这些字面量都会对不上。

扩展点是给缺陷靶场用的窄替换点，不是公开 API：默认实现就是原有规则本身。
"""

import pytest

from gamepilot.domain.combat import (
    INITIAL_POTIONS,
    PLAYER_MAX_HP,
    POTION_HEAL,
    SLIME_MAX_HP,
    CombatSession,
    create_combat_session,
    new_session_identity,
)

EventRow = tuple[int, str, str, int, int, int, int | None]


def _trace(session: CombatSession) -> list[EventRow]:
    """事件的完整线上字段序列：回合、行动方、类型、数值、双方生命与药水。"""
    return [
        (
            event.turn,
            event.actor,
            event.kind,
            event.value,
            event.player_hp,
            event.slime_hp,
            event.potions,
        )
        for event in session.snapshot().events
    ]


def _play(seed: int, actions: list[str]) -> CombatSession:
    session = CombatSession(session_id="fixed-session-id", seed=seed)
    for action in actions:
        getattr(session, action)()
    return session


# ------------------------------------------------ 改动前实测的固定轨迹（seed 42）


def test_attack_then_potion_trace_is_unchanged() -> None:
    session = _play(42, ["attack", "use_potion"])

    assert _trace(session) == [
        (1, "player", "attack", 19, 100, 41, 2),
        (1, "slime", "retaliate", 20, 80, 41, None),
        (2, "player", "potion", 20, 100, 41, 1),
        (2, "slime", "retaliate", 28, 72, 41, None),
    ]
    snapshot = session.snapshot()
    assert snapshot.status.value == "active"
    assert (snapshot.player.hp, snapshot.player.potions, snapshot.slime.hp) == (72, 1, 41)


def test_three_attacks_end_in_win_without_extra_retaliation() -> None:
    session = _play(42, ["attack", "attack", "attack"])

    assert _trace(session) == [
        (1, "player", "attack", 19, 100, 41, 2),
        (1, "slime", "retaliate", 20, 80, 41, None),
        (2, "player", "attack", 22, 80, 19, 2),
        (2, "slime", "retaliate", 27, 53, 19, None),
        (3, "player", "attack", 21, 53, 0, 2),
    ]
    snapshot = session.snapshot()
    assert snapshot.status.value == "won"
    assert (snapshot.player.hp, snapshot.slime.hp) == (53, 0)


def test_losing_trajectory_is_unchanged() -> None:
    session = _play(1252, ["attack", "attack", "attack"])

    assert _trace(session) == [
        (1, "player", "attack", 22, 100, 38, 2),
        (1, "slime", "retaliate", 35, 65, 38, None),
        (2, "player", "attack", 18, 65, 20, 2),
        (2, "slime", "retaliate", 31, 34, 20, None),
        (3, "player", "attack", 19, 34, 1, 2),
        (3, "slime", "retaliate", 34, 0, 1, None),
    ]
    snapshot = session.snapshot()
    assert snapshot.status.value == "lost"
    assert (snapshot.player.hp, snapshot.slime.hp) == (0, 1)


def test_potion_caps_heal_at_missing_hp() -> None:
    session = _play(12345, ["attack", "use_potion", "attack"])

    assert _trace(session) == [
        (1, "player", "attack", 24, 100, 36, 2),
        (1, "slime", "retaliate", 20, 80, 36, None),
        (2, "player", "potion", 20, 100, 36, 1),
        (2, "slime", "retaliate", 29, 71, 36, None),
        (3, "player", "attack", 23, 71, 13, 1),
        (3, "slime", "retaliate", 26, 45, 13, None),
    ]
    snapshot = session.snapshot()
    assert (snapshot.status.value, snapshot.player.potions, snapshot.slime.hp) == ("active", 1, 13)


def test_heal_equals_missing_hp_at_boundary() -> None:
    """缺失生命值正好等于治疗量时，正常规则不会超过上限。"""
    # seed 24：第一击 24 点，反击正好 25 点，玩家生命 75（缺失 25 = 治疗量）。
    session = _play(24, ["attack"])
    assert session.snapshot().player.hp == 75

    session.use_potion()
    snapshot = session.snapshot()
    assert _trace(session)[2] == (2, "player", "potion", POTION_HEAL, 100, 36, 1)
    # 治疗正好补满，没有溢出；随后史莱姆反击 26，玩家生命 74。
    assert snapshot.player.hp == 74
    assert snapshot.player.potions == 1


# ---------------------------------------------------------------- 扩展点默认值


def test_default_extension_points_are_the_normal_rules() -> None:
    session = CombatSession(session_id="s", seed=1)

    # 默认治疗量不超过缺失生命值：缺失 20 时只补 20，缺失足够时才补满 25。
    assert session._heal_amount(missing_hp=20) == 20
    assert session._heal_amount(missing_hp=POTION_HEAL) == POTION_HEAL
    assert session._heal_amount(missing_hp=PLAYER_MAX_HP) == POTION_HEAL
    assert session._potions_after_use() == INITIAL_POTIONS - 1
    assert session._should_retaliate(enemy_defeated=False) is True
    assert session._should_retaliate(enemy_defeated=True) is False


@pytest.mark.parametrize("missing_hp", [0, 1, 24, 25])
def test_heal_never_exceeds_missing_hp(missing_hp: int) -> None:
    session = CombatSession(session_id="s", seed=1)

    assert session._heal_amount(missing_hp=missing_hp) == min(POTION_HEAL, missing_hp)


def test_constants_are_unchanged() -> None:
    assert (PLAYER_MAX_HP, SLIME_MAX_HP, INITIAL_POTIONS, POTION_HEAL) == (100, 60, 2, 25)


# ------------------------------------------------------------ 会话创建与身份


def test_create_combat_session_uses_explicit_seed() -> None:
    session = create_combat_session(seed=42)

    assert session.seed == 42
    assert session.snapshot().player.hp == PLAYER_MAX_HP


def test_new_session_identity_generates_unique_ids_and_seeds() -> None:
    first_id, first_seed = new_session_identity(None)
    second_id, second_seed = new_session_identity(None)

    assert first_id != second_id
    assert 0 <= first_seed < 2**31
    assert 0 <= second_seed < 2**31

    generated_id, resolved_seed = new_session_identity(7)
    assert resolved_seed == 7
    assert generated_id
