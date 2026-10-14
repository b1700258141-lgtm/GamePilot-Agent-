"""v3 连战、经验顺延和可控缺陷的领域边界。"""

import pytest

from gamepilot.domain.combat import CombatSession, create_combat_session
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.lab.profiles import resolve_profile
from gamepilot.lab.recorder import TriggerRecorder
from gamepilot.lab.session import LabCombatSession
from gamepilot.persistence.recovery import StoredEvent, StoredSession, rebuild_session


def defeat_current(session: CombatSession) -> None:
    while session.snapshot().status == "active":
        session.attack()


def test_goblin_awards_thirty_exp_and_two_sequential_levels() -> None:
    session = create_combat_session(12)
    assert session.snapshot().enemy_type == "goblin"
    defeat_current(session)
    snapshot = session.snapshot()
    assert snapshot.player.level == 3
    assert snapshot.player.exp == 0
    assert snapshot.player.max_hp == snapshot.player.hp == 140
    assert snapshot.player.potions == 5
    assert [event.value for event in snapshot.events if event.kind == "level_up"] == [2, 3]


def test_slime_exp_carries_to_next_battle_without_resetting_player() -> None:
    session = create_combat_session(4, battle_count=2)
    assert session.snapshot().enemy_type == "slime"
    defeat_current(session)
    won = session.snapshot()
    assert (won.player.level, won.player.exp) == (1, 10)
    session.next_battle()
    next_state = session.snapshot()
    assert next_state.battle_index == 2
    assert next_state.enemy_type == "goblin"
    assert next_state.enemy_level == 2
    assert next_state.player == won.player
    assert next_state.slime.hp == next_state.slime.max_hp == 80
    assert next_state.turn == won.turn + 1
    defeat_current(session)
    final = session.snapshot()
    assert (final.player.level, final.player.exp) == (3, 10)
    assert final.player.max_hp == final.player.hp == 140
    assert final.player.potions == 5


def test_progressed_campaign_rebuilds_from_saved_events() -> None:
    session = create_combat_session(4, battle_count=2)
    defeat_current(session)
    session.next_battle()
    defeat_current(session)
    original = session.snapshot()
    stored = StoredSession.from_snapshot(original)
    events = [
        StoredEvent(sequence=index, event=event)
        for index, event in enumerate(original.events, start=1)
    ]
    assert rebuild_session(stored, events).snapshot() == original


@pytest.mark.parametrize("battle_count", range(1, 7))
def test_all_campaign_lengths_are_valid(battle_count: int) -> None:
    assert create_combat_session(9, battle_count).snapshot().battle_count == battle_count


def test_six_battle_campaign_can_beat_troll_after_leveling() -> None:
    session = create_combat_session(9, battle_count=6)
    enemies = []
    opening_potion_used = False
    actions = 0
    while actions < 50:
        state = session.snapshot()
        if state.status == "won":
            if state.battle_index == state.battle_count:
                break
            session.next_battle()
            actions += 1
            continue
        if state.status == "lost":
            pytest.fail("seed 9 should survive the six-battle campaign")
        if state.battle_index > len(enemies):
            enemies.append((state.enemy_type, state.player.level))
        if not opening_potion_used and state.player.potions:
            opening_potion_used = True
            session.use_potion()
        elif state.player.hp < state.player.max_hp and state.player.potions:
            session.use_potion()
        else:
            session.attack()
        actions += 1
    final = session.snapshot()
    assert final.status == "won"
    assert final.battle_index == final.battle_count == 6
    assert any(enemy == "troll" and level > 1 for enemy, level in enemies)
    assert actions < 50
    with pytest.raises(InvalidCombatAction) as exc:
        session.next_battle()
    assert exc.value.code == "campaign_complete"


@pytest.mark.parametrize(
    ("seed", "expected"),
    [(3, None), (4, 30), (5, 17)],
)
def test_loot_anomaly_covers_missing_wrong_enemy_and_abnormal_exp(
    seed: int, expected: int | None
) -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="test", seed=seed, profile=resolve_profile("loot_anomaly"), recorder=recorder
    )
    assert session.snapshot().enemy_type == "slime"
    defeat_current(session)
    exp_drops = [
        event.value
        for event in session.snapshot().events
        if event.kind == "drop" and event.item == "exp"
    ]
    assert exp_drops == ([] if expected is None else [expected])
    assert recorder.get("test") is not None


@pytest.mark.parametrize(("seed", "expected_level"), [(9, 2), (12, 1)])
def test_level_anomaly_covers_premature_and_missing_upgrade(seed: int, expected_level: int) -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="test", seed=seed, profile=resolve_profile("level_anomaly"), recorder=recorder
    )
    defeat_current(session)
    assert session.snapshot().player.level == expected_level
    assert recorder.get("test") is not None


def test_seeded_potion_fault_skips_first_eligible_action_then_replays() -> None:
    def run() -> tuple[list[int], object, object]:
        recorder = TriggerRecorder()
        session = LabCombatSession(
            session_id="timed",
            seed=42,
            battle_count=2,
            profile=resolve_profile("normal"),
            fault_ids=frozenset({"potion_not_consumed"}),
            timing_mode="seeded",
            recorder=recorder,
        )
        potions = []
        session.use_potion()
        potions.append(session.snapshot().player.potions)
        assert recorder.get("timed") is None
        session.use_potion()
        potions.append(session.snapshot().player.potions)
        return potions, recorder.get("timed"), session.snapshot()

    first = run()
    assert first[0] == [2, 2]
    assert first[1].fault_id == "potion_not_consumed"
    assert (first[1].battle_index, first[1].turn, first[1].opportunity) == (1, 2, 2)
    assert run() == first


def test_seeded_loot_fault_can_first_appear_in_second_battle() -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="timed-loot",
        seed=5,
        battle_count=2,
        profile=resolve_profile("normal"),
        fault_ids=frozenset({"loot_anomaly"}),
        timing_mode="seeded",
        recorder=recorder,
    )
    defeat_current(session)
    first_exp = [e.value for e in session.snapshot().events if e.item == "exp"]
    assert first_exp == [10]
    assert recorder.get("timed-loot") is None
    session.next_battle()
    defeat_current(session)
    exp = [e.value for e in session.snapshot().events if e.item == "exp"]
    assert exp == [10, 17]
    trigger = recorder.get("timed-loot")
    assert (trigger.battle_index, trigger.opportunity) == (2, 2)


def test_gold_fault_does_not_count_troll_without_gold_as_trigger() -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="troll-gold",
        seed=96,
        profile=resolve_profile("loot_anomaly"),
        timing_mode="seeded",
        recorder=recorder,
    )
    assert session.snapshot().enemy_type == "troll"
    assert session._drop_reward() is None
    assert recorder.get("troll-gold") is None


def test_seeded_gold_fault_can_add_invalid_item_to_troll_drop() -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="troll-stone",
        seed=8,
        profile=resolve_profile("loot_anomaly"),
        timing_mode="seeded",
        recorder=recorder,
    )
    assert session.snapshot().enemy_type == "troll"
    assert session._drop_reward() == ("stone", 1)
    assert recorder.get("troll-stone") is not None
