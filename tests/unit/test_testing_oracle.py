"""判定器单元测试：用构造的固定观测验证规则命中，且邻近正常样本不误报。

这里不发请求、不启动应用：观测是手工构造的固定数据，因此
「治疗超过上限」「药水未扣除」「致死后仍然反击」这些缺陷样本
可以在**不把真实游戏改成缺陷模式**的前提下被精确验证。

缺陷样本的构造方式与任务单一致——只构造测试数据：
每个用例只引入一处错误，其余字段保持自洽，这样断言可以精确到
「命中哪条 rule_id」，而不是「反正是失败了」。
"""

from gamepilot.testing.models import (
    ActionStep,
    CombatantView,
    EventView,
    HttpObservation,
    PlayerView,
    RuleCheck,
    SnapshotView,
)
from gamepilot.testing.oracle import check_step

SEED = 42


def _event(
    *,
    actor: str = "player",
    kind: str = "attack",
    value: int = 20,
    player_hp: int = 100,
    slime_hp: int = 40,
    potions: int | None = None,
    turn: int = 1,
) -> EventView:
    return EventView(
        turn=turn,
        actor=actor,
        kind=kind,
        value=value,
        player_hp=player_hp,
        slime_hp=slime_hp,
        potions=potions,
    )


def _snapshot(
    *,
    status: str = "active",
    turn: int = 0,
    player_hp: int = 100,
    potions: int = 2,
    slime_hp: int = 60,
    events: tuple[EventView, ...] = (),
) -> SnapshotView:
    return SnapshotView(
        session_id="sess-fixed",
        seed=SEED,
        status=status,
        turn=turn,
        player=PlayerView(hp=player_hp, max_hp=100, potions=potions),
        slime=CombatantView(hp=slime_hp, max_hp=60),
        events=list(events),
    )


def _observation() -> HttpObservation:
    return HttpObservation(method="POST", path="/api/v1/game-sessions/x/actions", status_code=200)


def _failed(checks: list[RuleCheck]) -> set[str]:
    return {check.rule_id for check in checks if check.status == "fail"}


def _attack_step(before: SnapshotView, after: SnapshotView) -> list[RuleCheck]:
    return check_step(1, ActionStep(action="attack"), before, _observation(), after)


def _potion_step(before: SnapshotView, after: SnapshotView) -> list[RuleCheck]:
    return check_step(1, ActionStep(action="use_potion"), before, _observation(), after)


# --------------------------------------------------------------- 正常邻近样本


def test_normal_attack_with_retaliation_passes_every_rule() -> None:
    """伤害取下界、反击取下界：区间是闭区间，不应误报。"""
    before = _snapshot()
    attack = _event(value=18, player_hp=100, slime_hp=42, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=42)
    after = _snapshot(turn=1, player_hp=80, slime_hp=42, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == set()


def test_normal_killing_blow_without_retaliation_passes_every_rule() -> None:
    """敌人被击杀时不反击是正确行为，不能被当成「少了反击」报错。"""
    before = _snapshot(slime_hp=20)
    attack = _event(value=20, player_hp=100, slime_hp=0, potions=2)
    after = _snapshot(status="won", turn=1, player_hp=100, slime_hp=0, events=(attack,))

    assert _failed(_attack_step(before, after)) == set()


def test_normal_potion_heals_by_cap_then_retaliates() -> None:
    """治疗量被上限截断（只回 20）是正确行为；反击按治疗后的 HP 计算。"""
    before = _snapshot(player_hp=80)
    potion = _event(kind="potion", value=20, player_hp=100, slime_hp=60, potions=1)
    retaliate = _event(actor="slime", kind="retaliate", value=35, player_hp=65, slime_hp=60)
    after = _snapshot(turn=1, player_hp=65, potions=1, slime_hp=60, events=(potion, retaliate))

    assert _failed(_potion_step(before, after)) == set()


# ----------------------------------------------------------------- 缺陷样本


def test_healing_beyond_max_hp_hits_potion_cap() -> None:
    """缺陷：治疗量算错导致 HP 超过上限。"""
    before = _snapshot(player_hp=80)
    potion = _event(kind="potion", value=25, player_hp=105, slime_hp=60, potions=1)
    retaliate = _event(actor="slime", kind="retaliate", value=30, player_hp=75, slime_hp=60)
    after = _snapshot(turn=1, player_hp=75, potions=1, slime_hp=60, events=(potion, retaliate))

    failed = _failed(_potion_step(before, after))
    assert "R-POTION-CAP" in failed
    assert failed == {"R-POTION-HEAL", "R-POTION-CAP", "R-INVARIANTS"}


def test_potion_not_decremented_hits_potion_decrements() -> None:
    """缺陷：喝药成功但药水数量没有减少。"""
    before = _snapshot(player_hp=80, potions=2)
    potion = _event(kind="potion", value=20, player_hp=100, slime_hp=60, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=30, player_hp=70, slime_hp=60)
    after = _snapshot(turn=1, player_hp=70, potions=2, slime_hp=60, events=(potion, retaliate))

    assert _failed(_potion_step(before, after)) == {"R-POTION-DECREMENTS"}


def test_retaliation_after_killing_blow_hits_no_retaliate_on_kill() -> None:
    """缺陷：致死攻击之后敌人仍然反击。"""
    before = _snapshot(slime_hp=20)
    attack = _event(value=20, player_hp=100, slime_hp=0, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=25, player_hp=75, slime_hp=0)
    after = _snapshot(status="won", turn=1, player_hp=75, slime_hp=0, events=(attack, retaliate))

    failed = _failed(_attack_step(before, after))
    assert "R-NO-RETALIATE-ON-KILL" in failed
    assert failed == {"R-NO-RETALIATE-ON-KILL", "R-EVENT-COUNT"}


def test_attack_damage_above_range_hits_attack_damage() -> None:
    """缺陷：伤害超出 18～25；邻近的合法值不报错（见上界样本）。"""
    before = _snapshot()
    attack = _event(value=26, player_hp=100, slime_hp=34, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=34)
    after = _snapshot(turn=1, player_hp=80, slime_hp=34, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-ATTACK-DAMAGE"}


def test_retaliation_above_range_hits_retaliate_once() -> None:
    """缺陷：反击伤害超出 20～35。"""
    before = _snapshot()
    attack = _event(value=20, player_hp=100, slime_hp=40, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=36, player_hp=64, slime_hp=40)
    after = _snapshot(turn=1, player_hp=64, slime_hp=40, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-RETALIATE-ONCE"}


def test_missing_retaliation_while_enemy_alive_hits_retaliate_once() -> None:
    """缺陷：敌人存活却没有反击。"""
    before = _snapshot()
    attack = _event(value=20, player_hp=100, slime_hp=40, potions=2)
    after = _snapshot(turn=1, player_hp=100, slime_hp=40, events=(attack,))

    assert _failed(_attack_step(before, after)) == {"R-EVENT-COUNT", "R-RETALIATE-ONCE"}


def test_damage_not_applied_to_hp_hits_attack_hp_apply() -> None:
    """缺陷：事件里的伤害与扣血不一致。"""
    before = _snapshot()
    attack = _event(value=20, player_hp=100, slime_hp=45, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=45)
    after = _snapshot(turn=1, player_hp=80, slime_hp=45, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-ATTACK-HP-APPLY"}


def test_turn_not_incremented_hits_turn_increment() -> None:
    """缺陷：成功动作没有推进回合。"""
    before = _snapshot(turn=3)
    attack = _event(turn=3, value=20, player_hp=100, slime_hp=40, potions=2)
    retaliate = _event(turn=3, actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=40)
    after = _snapshot(turn=3, player_hp=80, slime_hp=40, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-TURN-INCREMENT"}


def test_history_rewritten_hits_history_prefix() -> None:
    """缺陷：新响应改写了旧事件（历史必须原样保留并追加）。"""
    older = _event(turn=1, value=18, player_hp=100, slime_hp=42, potions=2)
    before = _snapshot(turn=1, player_hp=100, slime_hp=42, events=(older,))
    rewritten = _event(turn=1, value=99, player_hp=100, slime_hp=42, potions=2)
    attack = _event(turn=2, value=20, player_hp=100, slime_hp=22, potions=2)
    retaliate = _event(turn=2, actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=22)
    after = _snapshot(turn=2, player_hp=80, slime_hp=22, events=(rewritten, attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-HISTORY-PREFIX"}


def test_status_inconsistent_with_hp_hits_status_consistent() -> None:
    """缺陷：敌人满血却宣布胜利。"""
    before = _snapshot()
    attack = _event(value=20, player_hp=100, slime_hp=40, potions=2)
    retaliate = _event(actor="slime", kind="retaliate", value=20, player_hp=80, slime_hp=40)
    after = _snapshot(status="won", turn=1, player_hp=80, slime_hp=40, events=(attack, retaliate))

    assert _failed(_attack_step(before, after)) == {"R-STATUS-CONSISTENT"}
