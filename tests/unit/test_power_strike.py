"""重击玩法的伤害、冷却、拒绝动作与持久化重放。"""

import pytest

from gamepilot.api.routes import perform_action
from gamepilot.api.schemas import ActionRequest
from gamepilot.domain.combat import CombatSession, create_combat_session
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.persistence.recovery import StoredEvent, StoredSession, rebuild_session
from gamepilot.repositories.memory import InMemorySessionRepository


def test_power_strike_cools_down_after_two_successful_combat_actions() -> None:
    session = CombatSession("v3-strike", 12, ruleset="v3")
    before = session.snapshot()
    assert before.enemy_type == "goblin"

    session.power_strike()
    first = session.snapshot()
    strike = first.events[0]
    assert strike.kind == "power_strike"
    assert 30 <= strike.value <= 37
    assert first.slime.hp == before.slime.hp - strike.value
    assert first.player.power_strike_cooldown == 2

    with pytest.raises(InvalidCombatAction) as exc:
        session.power_strike()
    assert exc.value.code == "skill_cooldown"
    assert session.snapshot() == first

    session.attack()
    assert session.snapshot().player.power_strike_cooldown == 1
    session.use_potion()
    assert session.snapshot().player.power_strike_cooldown == 0
    session.power_strike()
    assert session.snapshot().status == "won"


def test_power_strike_cooldown_survives_next_battle_and_replay() -> None:
    session = CombatSession("v3-campaign", 4, ruleset="v3", battle_count=2)
    session.attack()
    session.attack()
    session.power_strike()
    assert session.snapshot().status == "won"
    # 重击击杀后也会冷却；进入下一场本身不减少冷却。
    session.next_battle()
    original = session.snapshot()
    assert original.player.power_strike_cooldown == 2
    stored = StoredSession.from_snapshot(original)
    events = [
        StoredEvent(sequence=index, event=event)
        for index, event in enumerate(original.events, start=1)
    ]
    assert rebuild_session(stored, events).snapshot() == original


def test_legacy_session_rejects_skill_without_changing_state() -> None:
    session = CombatSession("legacy", 4, ruleset="v2")
    before = session.snapshot()
    with pytest.raises(InvalidCombatAction) as exc:
        session.power_strike()
    assert exc.value.code == "skill_unavailable"
    assert session.snapshot() == before


def test_action_route_dispatches_power_strike() -> None:
    repository = InMemorySessionRepository()
    session = create_combat_session(12)
    repository.save(session)
    result = perform_action(session.session_id, ActionRequest(action="power_strike"), repository)
    assert result.events[0].kind == "power_strike"
    assert repository.get(session.session_id).snapshot() == result
