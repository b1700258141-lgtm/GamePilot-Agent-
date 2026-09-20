"""缺陷会话的领域级验证：真实状态变异、触发记录条件与 normal 等价性。

这些用例直接驱动领域对象（不经 HTTP），验证的是「缺陷本身就是游戏状态
的变异」：生命值、药水数量与事件历史都真的被改变，而不是响应被改写。
"""

from gamepilot.domain.combat import (
    PLAYER_MAX_HP,
    POTION_HEAL,
    CombatSession,
    new_session_identity,
)
from gamepilot.lab import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_NORMAL,
    LabCombatSession,
    TriggerRecorder,
    make_session_factory,
    resolve_profile,
)

EventRow = tuple[int, str, str, int, int, int, int | None]


def _trace(session: CombatSession) -> list[EventRow]:
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


def _lab_session(profile_id: str, seed: int, recorder: TriggerRecorder) -> LabCombatSession:
    session_id, resolved_seed = new_session_identity(seed)
    return LabCombatSession(
        session_id=session_id,
        seed=resolved_seed,
        profile=resolve_profile(profile_id),
        recorder=recorder,
    )


# ------------------------------------------------------------ 越界治疗缺陷


def test_potion_overheal_really_raises_hp_above_max() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_OVERHEAL, 42, recorder)
    session.attack()  # seed 42：反击 20，玩家生命 80

    session.use_potion()

    # 缺失 20 却治疗 25：喝药事件里的生命值真的越过了上限。
    assert _trace(session)[2] == (2, "player", "potion", POTION_HEAL, 105, 41, 1)
    # 后续反击从越界后的 105 继续扣，说明状态是真的被改了。
    snapshot = session.snapshot()
    assert snapshot.player.hp == 105 - 28
    assert snapshot.player.hp > PLAYER_MAX_HP - 28


def test_potion_overheal_records_only_a_real_deviation() -> None:
    """缺失生命值等于治疗量时缺陷值与正常值相同，不算偏离，也不记录。"""
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_OVERHEAL, 24, recorder)
    session.attack()  # seed 24：反击 25，玩家生命 75（缺失正好 25）

    session.use_potion()

    assert _trace(session)[2] == (2, "player", "potion", POTION_HEAL, 100, 36, 1)
    assert session.snapshot().player.hp == 74  # 与 normal 完全一致
    assert recorder.get(session.session_id) is None
    assert len(recorder) == 0


def test_potion_overheal_records_trigger_when_missing_hp_is_smaller() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_OVERHEAL, 42, recorder)
    session.attack()

    session.use_potion()

    trigger = recorder.get(session.session_id)
    assert trigger is not None
    assert trigger.fault_id == FAULT_POTION_OVERHEAL
    assert "20" in trigger.detail and str(POTION_HEAL) in trigger.detail


def test_potion_overheal_does_not_touch_potions_or_retaliation() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_OVERHEAL, 42, recorder)
    session.attack()

    session.use_potion()

    snapshot = session.snapshot()
    assert snapshot.player.potions == 1
    assert [event.kind for event in snapshot.events] == [
        "attack",
        "retaliate",
        "potion",
        "retaliate",
    ]


# ---------------------------------------------------------- 药水不消耗缺陷


def test_potion_not_consumed_keeps_the_potion_count() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_NOT_CONSUMED, 42, recorder)
    session.attack()

    session.use_potion()

    # 治疗本身仍然正确（喝药事件里补到 100），只是药水数量没有减少。
    assert _trace(session)[2] == (2, "player", "potion", 20, 100, 41, 2)
    snapshot = session.snapshot()
    assert snapshot.player.potions == 2
    assert snapshot.player.hp == 100 - 28  # 反击照常发生
    trigger = recorder.get(session.session_id)
    assert trigger is not None
    assert trigger.fault_id == FAULT_POTION_NOT_CONSUMED


def test_potion_not_consumed_still_allows_repeated_use() -> None:
    """药水数量不减少会让玩家可以反复喝药，这是缺陷的可见后果。"""
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_POTION_NOT_CONSUMED, 42, recorder)
    session.attack()

    session.use_potion()  # 生命 100、药水仍为 2，随后反击 28 -> 72
    session.use_potion()  # 生命 97，随后反击 27 -> 70

    snapshot = session.snapshot()
    assert snapshot.player.hp == 70
    assert snapshot.player.potions == 2
    assert [event.kind for event in snapshot.events].count("potion") == 2
    # 两次偏离只留下一条触发记录：记录条数不等于发现次数。
    assert len(recorder) == 1


# ---------------------------------------------------------- 死后反击缺陷


def test_retaliate_after_death_deals_real_damage_after_the_kill() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_RETALIATE_AFTER_DEATH, 42, recorder)

    for _ in range(3):
        session.attack()

    events = _trace(session)
    assert events[-1] == (3, "slime", "retaliate", 24, 29, 0, None)
    snapshot = session.snapshot()
    # 敌人已经死亡，却仍然造成了真实伤害；胜负状态保持 won（玩家没有被打死）。
    assert snapshot.slime.hp == 0
    assert snapshot.player.hp == 53 - 24
    assert snapshot.status.value == "won"
    trigger = recorder.get(session.session_id)
    assert trigger is not None
    assert trigger.fault_id == FAULT_RETALIATE_AFTER_DEATH


def test_retaliate_after_death_not_recorded_when_enemy_survives() -> None:
    recorder = TriggerRecorder()
    session = _lab_session(FAULT_RETALIATE_AFTER_DEATH, 42, recorder)

    session.attack()  # 敌人未死：反击本来就会发生，不是缺陷

    assert [event.kind for event in session.snapshot().events] == ["attack", "retaliate"]
    assert recorder.get(session.session_id) is None


def test_retaliate_after_death_keeps_other_trajectories_unchanged() -> None:
    """除致死回合外，变体的轨迹必须与 normal 逐字段一致。"""
    recorder = TriggerRecorder()
    faulted = _lab_session(FAULT_RETALIATE_AFTER_DEATH, 42, recorder)
    normal = CombatSession(session_id="normal", seed=42)

    faulted.attack()
    faulted.attack()
    normal.attack()
    normal.attack()

    assert _trace(faulted) == _trace(normal)


# ------------------------------------------------------------- normal 等价性


def test_normal_profile_session_matches_plain_combat_session() -> None:
    recorder = TriggerRecorder()
    lab = _lab_session(PROFILE_NORMAL, 42, recorder)
    plain = CombatSession(session_id="plain", seed=42)

    for session in (lab, plain):
        session.attack()
        session.use_potion()
        session.attack()

    assert _trace(lab) == _trace(plain)
    assert lab.snapshot().model_dump(exclude={"session_id"}) == plain.snapshot().model_dump(
        exclude={"session_id"}
    )
    assert len(recorder) == 0


def test_session_factory_binds_profile_and_recorder() -> None:
    recorder = TriggerRecorder()
    factory = make_session_factory(resolve_profile(FAULT_POTION_OVERHEAL), recorder)

    session = factory(42)

    assert isinstance(session, LabCombatSession)
    assert session.seed == 42
    assert session.fault_profile.fault_id == FAULT_POTION_OVERHEAL
    session.attack()
    session.use_potion()
    assert recorder.get(session.session_id) is not None


def test_session_factory_generates_seed_when_missing() -> None:
    factory = make_session_factory(resolve_profile(PROFILE_NORMAL), TriggerRecorder())

    first, second = factory(None), factory(None)

    assert first.session_id != second.session_id
    assert 0 <= first.seed < 2**31
