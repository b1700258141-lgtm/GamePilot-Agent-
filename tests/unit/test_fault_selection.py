"""随机靶场只按 seed 选缺陷，且保留足够多的正常种子。"""

import pytest

from gamepilot.domain.combat import create_combat_session
from gamepilot.lab.profiles import FAULT_IDS
from gamepilot.lab.selection import activation_opportunity, select_faults


def test_seeded_selection_is_reproducible_and_has_at_least_sixty_percent_clean_seeds() -> None:
    selections = [select_faults(seed) for seed in range(1000)]
    assert selections == [select_faults(seed) for seed in range(1000)]
    assert sum(not item.fault_ids for item in selections) >= 600
    assert {item.fault_ids[0] for item in selections if item.fault_ids} == set(FAULT_IDS)
    assert all(item.mode == "random" and len(item.fault_ids) <= 1 for item in selections)


def test_forced_faults_are_distinct_and_return_in_catalog_order() -> None:
    expected = (FAULT_IDS[0], FAULT_IDS[-1])
    assert select_faults(42, (FAULT_IDS[-1], FAULT_IDS[0])).fault_ids == expected
    assert select_faults(42, expected).mode == "forced"
    with pytest.raises(ValueError):
        select_faults(42, (FAULT_IDS[0], FAULT_IDS[0]))
    with pytest.raises(ValueError):
        select_faults(42, ("not_a_fault",))


def test_seeded_activation_can_delay_each_fault_without_changing_selection() -> None:
    for fault_id in FAULT_IDS:
        targets = [activation_opportunity(seed, 6, fault_id) for seed in range(100)]
        assert targets == [activation_opportunity(seed, 6, fault_id) for seed in range(100)]
        assert all(1 <= target <= 6 for target in targets)
        assert any(target > 1 for target in targets)
    assert activation_opportunity(42, 2, "potion_not_consumed") == 2
    with pytest.raises(ValueError):
        activation_opportunity(42, 2, "not_a_fault")


def test_clean_six_battle_seeds_can_reach_a_terminal_state_within_action_budget() -> None:
    """校验 60% 门槛不只是无缺陷比例，也有终态测试的动作空间。"""
    reachable = 0
    for seed in range(1000):
        if select_faults(seed).fault_ids:
            continue
        session = create_combat_session(seed, battle_count=6)
        opening_potion_used = False
        for _ in range(49):
            state = session.snapshot()
            if state.status == "lost" or (
                state.status == "won" and state.battle_index == state.battle_count
            ):
                reachable += 1
                break
            if state.status == "won":
                session.next_battle()
            elif not opening_potion_used and state.player.potions:
                opening_potion_used = True
                session.use_potion()
            elif state.player.hp < state.player.max_hp and state.player.potions:
                session.use_potion()
            else:
                session.attack()
    assert reachable >= 600
