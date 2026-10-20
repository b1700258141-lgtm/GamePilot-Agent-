"""v4 新敌人、EP 魔法及独立判定器对技能异常的覆盖。"""

import pytest

from gamepilot.agent.goals import GOAL_BATTLE_END
from gamepilot.agent.models import ChatMessage
from gamepilot.agent.prompts import render_snapshot
from gamepilot.api.routes import create_session, perform_action
from gamepilot.api.schemas import ActionRequest, CreateSessionRequest
from gamepilot.benchmark.agent_eval import observation_reacting_double, read_observation
from gamepilot.domain.combat import CombatSession, create_combat_session
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.lab.profiles import resolve_profile
from gamepilot.lab.recorder import TriggerRecorder
from gamepilot.lab.session import LabCombatSession, make_session_factory
from gamepilot.persistence.recovery import StoredEvent, StoredSession, rebuild_session
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.testing.models import ActionStep, HttpObservation, SnapshotView
from gamepilot.testing.oracle import check_initial_state, check_step
from gamepilot.testing.planning import derive_expectation
from gamepilot.testing.rules import R_SKILL_POWER


def _view(session: CombatSession) -> SnapshotView:
    return SnapshotView.model_validate(session.snapshot().model_dump())


def _checks(session: CombatSession, before: SnapshotView, action: str):
    after = _view(session)
    observed = HttpObservation(
        method="POST", path="/actions", status_code=200, body=after.model_dump()
    )
    return check_step(1, ActionStep(action=action), before, observed, after)


def test_default_offensive_strategy_cycles_spells_and_falls_back_to_attack() -> None:
    provider = observation_reacting_double(GOAL_BATTLE_END)
    decisions = (
        (0, 150, "power_strike"),
        (2, 150, "cast_meteor"),
        (1, 80, "cast_flame"),
        (0, 40, "power_strike"),
        (2, 40, "cast_spark"),
        (1, 20, "cast_spark"),
        (0, 0, "power_strike"),
        (2, 10, "attack"),
    )
    for cooldown, ep, expected in decisions:
        observation = (
            "状态：active\n规则集：v5\n连战：第 1/1 场\n"
            f"玩家：生命 100/100，药水 0，EP {ep}/150，重击冷却 {cooldown}\n"
            "史莱姆：等级 1，生命 60/60"
        )
        reply = provider._responder([ChatMessage(role="user", content=observation)])
        assert reply.tool_calls[0].arguments == {"action": expected}

    provider = observation_reacting_double(GOAL_BATTLE_END)
    opening = (
        "状态：active\n规则集：v5\n连战：第 1/1 场\n"
        "玩家：生命 100/100，药水 3，EP 100/100，重击冷却 0\n"
        "史莱姆：等级 1，生命 60/60"
    )
    injured = opening.replace("生命 100/100，药水 3", "生命 85/100，药水 2")
    assert provider._responder([ChatMessage(role="user", content=opening)]).tool_calls[
        0
    ].arguments == {"action": "use_potion"}
    assert provider._responder([ChatMessage(role="user", content=injured)]).tool_calls[
        0
    ].arguments == {"action": "power_strike"}


def test_all_eight_enemies_appear_and_pass_initial_and_attack_rules() -> None:
    found: dict[str, int] = {}
    for seed in range(1000):
        enemy = create_combat_session(seed).snapshot().enemy_type
        found.setdefault(enemy, seed)
    assert set(found) == {"slime", "goblin", "skeleton", "wolf", "orc", "troll", "wraith", "dragon"}
    for enemy, seed in found.items():
        session = create_combat_session(seed)
        before = _view(session)
        assert before.enemy_type == enemy
        assert before.ruleset == "v5"
        assert all(check.status == "pass" for check in check_initial_state(before, before))
        observation = read_observation(render_snapshot(before))
        assert observation is not None
        assert (observation.ruleset, observation.ep, observation.status) == ("v5", 100, "active")
        session.attack()
        assert all(check.status == "pass" for check in _checks(session, before, "attack"))
        snapshot = session.snapshot()
        stored = StoredSession.from_snapshot(snapshot)
        events = [
            StoredEvent(sequence=i, event=event) for i, event in enumerate(snapshot.events, 1)
        ]
        assert rebuild_session(stored, events).snapshot() == snapshot


@pytest.mark.parametrize("action", ["power_strike", "cast_spark", "cast_flame", "cast_meteor"])
@pytest.mark.parametrize("seed", [8, 9])
def test_skill_power_fault_is_detected_for_each_damage_skill(action: str, seed: int) -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id=f"skill-{seed}-{action}",
        seed=seed,
        ruleset="v4",
        profile=resolve_profile("skill_power_anomaly"),
        recorder=recorder,
    )
    before = _view(session)
    if action == "power_strike":
        session.power_strike()
    else:
        session.cast_spell(action)
    checks = _checks(session, before, action)
    assert any(check.rule_id == R_SKILL_POWER and check.status == "fail" for check in checks)
    assert recorder.for_session(session.session_id)


def test_skill_fault_is_detected_against_each_new_enemy() -> None:
    found: dict[str, int] = {}
    for seed in range(1000):
        enemy = create_combat_session(seed).snapshot().enemy_type
        if enemy in {"skeleton", "wolf", "orc", "wraith", "dragon"}:
            found.setdefault(enemy, seed)
    assert len(found) == 5
    for enemy, seed in found.items():
        recorder = TriggerRecorder()
        session = LabCombatSession(
            session_id=f"fault-{enemy}",
            seed=seed,
            ruleset="v4",
            profile=resolve_profile("skill_power_anomaly"),
            recorder=recorder,
        )
        before = _view(session)
        session.cast_spell("cast_spark")
        checks = _checks(session, before, "cast_spark")
        assert any(check.rule_id == R_SKILL_POWER and check.status == "fail" for check in checks)
        assert recorder.for_session(session.session_id)


def test_seeded_skill_fault_activates_on_second_spell() -> None:
    recorder = TriggerRecorder()
    session = LabCombatSession(
        session_id="seeded-skill",
        seed=0,
        ruleset="v4",
        profile=resolve_profile("skill_power_anomaly"),
        recorder=recorder,
        timing_mode="seeded",
    )
    before = _view(session)
    session.cast_spell("cast_spark")
    assert all(check.status == "pass" for check in _checks(session, before, "cast_spark"))
    assert not recorder.for_session(session.session_id)
    before = _view(session)
    session.cast_spell("cast_spark")
    assert any(
        check.rule_id == R_SKILL_POWER and check.status == "fail"
        for check in _checks(session, before, "cast_spark")
    )
    assert recorder.for_session(session.session_id)[0].opportunity == 2


def test_new_enemy_skill_fault_flows_through_api_repository_and_oracle() -> None:
    recorder = TriggerRecorder()
    repository = InMemorySessionRepository()
    factory = make_session_factory(resolve_profile("skill_power_anomaly"), recorder)
    created = create_session(CreateSessionRequest(seed=3), repository, factory)
    before = SnapshotView.model_validate(created.model_dump())
    assert before.enemy_type == "skeleton"
    assert all(check.status == "pass" for check in check_initial_state(before, before))
    plan = derive_expectation(before, "cast_flame")
    assert plan.expected_status == 200
    response = perform_action(created.session_id, ActionRequest(action="cast_flame"), repository)
    after = SnapshotView.model_validate(response.model_dump())
    observed = HttpObservation(
        method="POST", path="/actions", status_code=200, body=after.model_dump()
    )
    checks = check_step(1, plan, before, observed, after)
    assert any(check.rule_id == R_SKILL_POWER and check.status == "fail" for check in checks)
    assert repository.get(created.session_id).snapshot() == response
    assert recorder.for_session(created.session_id)


def test_spells_have_no_cooldown_and_ep_is_spent() -> None:
    session = create_combat_session(0)
    before = _view(session)
    session.cast_spell("cast_spark")
    assert session.snapshot().player.ep == 80
    assert all(check.status == "pass" for check in _checks(session, before, "cast_spark"))
    if session.snapshot().status == "active":
        before = _view(session)
        session.cast_spell("cast_spark")
        assert session.snapshot().player.ep == 60
        assert all(check.status == "pass" for check in _checks(session, before, "cast_spark"))


def test_magic_is_usable_during_power_strike_cooldown() -> None:
    session = create_combat_session(2)
    session.power_strike()
    before = _view(session)
    assert before.player.power_strike_cooldown == 2
    session.cast_spell("cast_spark")
    assert session.snapshot().player.power_strike_cooldown == 1
    assert session.snapshot().player.ep == 80
    assert all(check.status == "pass" for check in _checks(session, before, "cast_spark"))


@pytest.mark.parametrize(
    ("action", "cost", "damage_range"),
    [
        ("cast_spark", 20, (28, 34)),
        ("cast_flame", 40, (42, 50)),
        ("cast_meteor", 70, (60, 70)),
    ],
)
def test_damage_magic_has_distinct_ep_cost_and_power(
    action: str, cost: int, damage_range: tuple[int, int]
) -> None:
    session = create_combat_session(8)
    before = _view(session)
    session.cast_spell(action)
    after = session.snapshot()
    spell = after.events[0]
    assert after.player.ep == 100 - cost
    assert damage_range[0] <= spell.value <= damage_range[1]
    assert all(check.status == "pass" for check in _checks(session, before, action))


def test_magic_heal_consumes_ep_and_obeys_hp_cap() -> None:
    session = create_combat_session(0)
    before = _view(session)
    session.cast_spell("cast_heal")
    assert session.snapshot().player.ep == 70
    assert all(check.status == "pass" for check in _checks(session, before, "cast_heal"))


def test_magic_heal_power_grows_with_level() -> None:
    session = create_combat_session(2, battle_count=2)
    while session.snapshot().status == "active":
        session.attack()
    assert session.snapshot().player.level == 2
    session.next_battle()
    before = _view(session)
    session.cast_spell("cast_heal")
    event = session.snapshot().events[len(before.events)]
    assert event.value == 35
    assert session.snapshot().player.ep == before.player.ep - 30
    assert all(check.status == "pass" for check in _checks(session, before, "cast_heal"))


def test_insufficient_ep_rejects_without_state_or_rng_change() -> None:
    session = create_combat_session(2)
    session.cast_spell("cast_meteor")
    before = session.snapshot()
    assert before.status == "active"
    expectation = derive_expectation(_view(session), "cast_flame")
    assert (expectation.expected_status, expectation.expected_code) == (409, "not_enough_ep")
    with pytest.raises(InvalidCombatAction) as exc:
        session.cast_spell("cast_flame")
    assert exc.value.code == "not_enough_ep"
    assert session.snapshot() == before


def test_level_up_refills_and_expands_ep_and_magic_and_replays() -> None:
    session = create_combat_session(2)
    session.cast_spell("cast_spark")
    assert session.snapshot().player.ep == 80
    while session.snapshot().status == "active":
        before = _view(session)
        session.attack()
        assert all(check.status == "pass" for check in _checks(session, before, "attack"))
    original = session.snapshot()
    assert original.status == "won"
    assert original.player.level == 2
    assert original.player.exp == 15
    assert original.player.max_ep == 100 + 50 * (original.player.level - 1)
    assert original.player.ep == original.player.max_ep
    assert original.player.max_ep == 150
    assert original.player.magic == 10 + 5 * (original.player.level - 1)
    stored = StoredSession.from_snapshot(original)
    events = [StoredEvent(sequence=i, event=event) for i, event in enumerate(original.events, 1)]
    assert rebuild_session(stored, events).snapshot() == original


def test_v5_experience_threshold_grows_and_v4_session_keeps_old_threshold() -> None:
    session = create_combat_session(2, battle_count=2)
    while session.snapshot().status == "active":
        session.attack()
    first = session.snapshot()
    assert (first.player.level, first.player.exp, first.player.max_ep, first.player.magic) == (
        2,
        15,
        150,
        15,
    )
    session.next_battle()
    while session.snapshot().status == "active":
        session.attack()
    second = session.snapshot()
    assert (second.player.level, second.player.exp, second.player.max_ep, second.player.magic) == (
        3,
        15,
        200,
        20,
    )
    stored = StoredSession.from_snapshot(second)
    events = [StoredEvent(sequence=i, event=event) for i, event in enumerate(second.events, 1)]
    assert rebuild_session(stored, events).snapshot() == second

    legacy = CombatSession("legacy-v4", 2, ruleset="v4")
    while legacy.snapshot().status == "active":
        legacy.attack()
    old = legacy.snapshot()
    assert (old.player.level, old.player.exp, old.player.max_ep, old.player.magic) == (
        3,
        0,
        200,
        20,
    )
    old_stored = StoredSession.from_snapshot(old)
    old_events = [StoredEvent(sequence=i, event=event) for i, event in enumerate(old.events, 1)]
    assert rebuild_session(old_stored, old_events).snapshot() == old


def test_legacy_v3_seed_still_selects_old_enemy_and_replays() -> None:
    session = CombatSession("legacy-v3", 12, ruleset="v3")
    assert session.snapshot().enemy_type == "goblin"
    session.attack()
    original = session.snapshot()
    stored = StoredSession.from_snapshot(original)
    events = [StoredEvent(sequence=i, event=event) for i, event in enumerate(original.events, 1)]
    assert rebuild_session(stored, events).snapshot() == original
