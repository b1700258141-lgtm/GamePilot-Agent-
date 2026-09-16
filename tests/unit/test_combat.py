"""战斗领域单元测试：初始状态、动作规则、边界与确定性。

所有测试都基于固定种子，不依赖真实网络或外部服务，不会偶发失败。
"""

import pytest

from gamepilot.domain.combat import (
    PLAYER_DAMAGE_RANGE,
    SLIME_DAMAGE_RANGE,
    CombatSession,
    create_combat_session,
)
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.domain.models import BattleStatus


def snapshot_without_id(session: CombatSession) -> dict:
    """比较确定性时排除 session_id（任务单明确允许）。"""
    return session.snapshot().model_dump(exclude={"session_id"})


def play_attacks_until_end(session: CombatSession) -> None:
    """连续攻击直到战斗结束；史莱姆至多 4 回合必死，设上限防死循环。"""
    for _ in range(20):
        if session.snapshot().status is not BattleStatus.ACTIVE:
            return
        session.attack()
    pytest.fail("battle did not finish within 20 turns")


class TestInitialState:
    def test_initial_state_matches_spec(self) -> None:
        session = create_combat_session(seed=42)
        snap = session.snapshot()
        assert snap.seed == 42
        assert snap.status is BattleStatus.ACTIVE
        assert snap.turn == 0
        assert snap.player.hp == 100
        assert snap.player.max_hp == 100
        assert snap.player.potions == 2
        assert snap.slime.hp == 60
        assert snap.slime.max_hp == 60
        assert snap.events == []


class TestAttack:
    def test_damage_stays_within_defined_ranges(self) -> None:
        for seed in range(200):
            session = create_combat_session(seed=seed)
            session.attack()
            events = session.snapshot().events
            player_hit = next(e for e in events if e.actor == "player")
            slime_hit = next(e for e in events if e.actor == "slime")
            assert PLAYER_DAMAGE_RANGE[0] <= player_hit.value <= PLAYER_DAMAGE_RANGE[1]
            assert SLIME_DAMAGE_RANGE[0] <= slime_hit.value <= SLIME_DAMAGE_RANGE[1]

    def test_slime_does_not_retaliate_when_it_dies(self) -> None:
        session = create_combat_session(seed=42)
        play_attacks_until_end(session)
        snap = session.snapshot()
        assert snap.status is BattleStatus.WON
        assert snap.slime.hp == 0
        # 最后一回合只有玩家攻击事件，没有史莱姆反击
        final_turn_events = [e for e in snap.events if e.turn == snap.turn]
        assert [e.actor for e in final_turn_events] == ["player"]
        assert [e.kind for e in final_turn_events] == ["attack"]

    def test_hp_never_leaves_valid_range(self) -> None:
        for seed in range(50):
            session = create_combat_session(seed=seed)
            play_attacks_until_end(session)
            for event in session.snapshot().events:
                assert 0 <= event.player_hp <= 100
                assert 0 <= event.slime_hp <= 60

    def test_successful_attack_increments_turn(self) -> None:
        session = create_combat_session(seed=42)
        session.attack()
        snap = session.snapshot()
        assert snap.turn == 1
        assert all(e.turn == 1 for e in snap.events)


class TestPotion:
    def test_potion_heals_exactly_25_when_below_cap(self) -> None:
        # 两次攻击后史莱姆必然存活，玩家生命至多 60，治疗不会被上限截断。
        session = create_combat_session(seed=42)
        for _ in range(2):
            session.attack()
        before = session.snapshot()
        session.use_potion()
        after = session.snapshot()
        # 注意：药水事件记录的是反击前的治疗结果，之后史莱姆还会反击
        turn_events = [e for e in after.events if e.turn == after.turn]
        potion_event = next(e for e in turn_events if e.kind == "potion")
        retaliate_event = next(e for e in turn_events if e.kind == "retaliate")
        assert potion_event.value == 25
        assert potion_event.player_hp == before.player.hp + 25
        assert after.player.hp == potion_event.player_hp - retaliate_event.value
        assert after.player.potions == before.player.potions - 1

    def test_potion_heal_is_capped_at_max_hp(self) -> None:
        session = create_combat_session(seed=42)
        session.attack()  # seed=42 的首次反击造成 20 点伤害，+25 会被上限截断
        before = session.snapshot()
        session.use_potion()
        after = session.snapshot()
        turn_events = [e for e in after.events if e.turn == after.turn]
        potion_event = next(e for e in turn_events if e.kind == "potion")
        assert potion_event.player_hp == 100  # 治疗后被上限截断到满血
        assert potion_event.value == 100 - before.player.hp
        assert after.player.potions == before.player.potions - 1
        assert after.player.hp <= 100  # 反击后生命仍在合法范围内

    def test_potion_at_full_hp_is_rejected_without_partial_change(self) -> None:
        session = create_combat_session(seed=42)
        before = session.snapshot()
        with pytest.raises(InvalidCombatAction) as exc_info:
            session.use_potion()
        assert exc_info.value.code == "player_full_hp"
        assert session.snapshot() == before

    def test_potion_without_potions_is_rejected_without_partial_change(self) -> None:
        session = create_combat_session(seed=42)
        session.attack()
        session.use_potion()
        session.attack()
        session.use_potion()
        assert session.snapshot().player.potions == 0
        before = session.snapshot()
        assert before.player.hp < 100  # 第二瓶药水的反击已让玩家脱离满血
        with pytest.raises(InvalidCombatAction) as exc_info:
            session.use_potion()
        assert exc_info.value.code == "no_potions"
        assert session.snapshot() == before


class TestBattleEnd:
    def test_actions_after_battle_end_are_rejected(self) -> None:
        session = create_combat_session(seed=42)
        play_attacks_until_end(session)
        assert session.snapshot().status is BattleStatus.WON
        before = session.snapshot()
        with pytest.raises(InvalidCombatAction) as exc_info:
            session.attack()
        assert exc_info.value.code == "battle_not_active"
        with pytest.raises(InvalidCombatAction) as exc_info:
            session.use_potion()
        assert exc_info.value.code == "battle_not_active"
        assert session.snapshot() == before

    def test_player_death_sets_lost_status(self) -> None:
        """失败状态必须能通过公开动作到达，不能修改会话私有字段。"""
        session = create_combat_session(seed=1252)
        for _ in range(3):
            session.attack()
        snap = session.snapshot()
        assert snap.status is BattleStatus.LOST
        assert snap.player.hp == 0
        before = snap
        with pytest.raises(InvalidCombatAction) as exc_info:
            session.attack()
        assert exc_info.value.code == "battle_not_active"
        assert session.snapshot() == before


class TestDeterminism:
    def test_same_seed_and_same_actions_produce_identical_results(self) -> None:
        def replay(seed: int) -> dict:
            # 该序列对任意种子都合法：2 次攻击后史莱姆必然存活（2×25=50 < 60），
            # 玩家生命 ∈ [76, 84] < 100，药水可用
            session = create_combat_session(seed=seed)
            session.attack()
            session.attack()
            session.use_potion()
            play_attacks_until_end(session)
            return snapshot_without_id(session)

        assert replay(7) == replay(7)

    def test_sessions_with_same_seed_are_independent_but_identical(self) -> None:
        first = create_combat_session(seed=9)
        second = create_combat_session(seed=9)
        first.attack()
        first.attack()  # 两步后史莱姆必然存活（2×25=50 < 60）
        second.attack()  # 只走一步，不受 first 影响
        assert second.snapshot().turn == 1
        solo = create_combat_session(seed=9)
        solo.attack()
        assert snapshot_without_id(second) == snapshot_without_id(solo)

    def test_session_random_state_does_not_leak_into_other_sessions(self) -> None:
        first = create_combat_session(seed=1)
        second = create_combat_session(seed=2)
        before = snapshot_without_id(second)
        play_attacks_until_end(first)
        assert snapshot_without_id(second) == before
