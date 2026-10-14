"""新版战斗公开规则与独立判定器的关键回归。"""

import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gamepilot.domain.combat import CombatSession
from gamepilot.domain.errors import InvalidCombatAction
from gamepilot.lab.profiles import resolve_profile
from gamepilot.lab.recorder import TriggerRecorder
from gamepilot.lab.session import LabCombatSession
from gamepilot.persistence.recovery import StoredEvent, StoredSession, rebuild_session
from gamepilot.testing.models import ActionStep, HttpObservation, SnapshotView
from gamepilot.testing.oracle import check_step
from gamepilot.testing.planning import derive_expectation
from gamepilot.testing.rules import (
    R_LOOT_ITEM,
    R_LOOT_PRESENT,
    R_LOOT_RANGE,
    R_NO_ACTION_AFTER_PLAYER_DEATH,
    R_POTION_CAP,
    R_POTION_DECREMENTS,
)
from gamepilot.web_runs import AgentWebRunManager


def view(session: CombatSession) -> SnapshotView:
    return SnapshotView.model_validate(session.snapshot().model_dump())


def failures(before: SnapshotView, after: SnapshotView, action: str) -> set[str]:
    observed = HttpObservation(method="POST", path="/test", status_code=200)
    return {
        check.rule_id
        for check in check_step(1, ActionStep(action=action), before, observed, after)
        if check.status == "fail"
    }


class BattleV2Tests(unittest.TestCase):
    def lab(self, seed: int, profile: str) -> LabCombatSession:
        return LabCombatSession(
            session_id=f"lab-{seed}",
            seed=seed,
            profile=resolve_profile(profile),
            recorder=TriggerRecorder(),
        )

    def test_fixed_potion_power_is_capped_and_consumed_at_full_hp(self) -> None:
        session = CombatSession("full", 42)
        before = view(session)
        self.assertEqual(200, derive_expectation(before, "use_potion").expected_status)
        session.use_potion()
        after = view(session)
        self.assertEqual(25, after.events[0].value)
        self.assertEqual(100, after.events[0].player_hp)
        self.assertEqual(2, after.events[0].potions)
        self.assertEqual(set(), failures(before, after, "use_potion"))

        # 90 HP 时固定效力仍为 25，实际回血只到 100。
        session = CombatSession("near-cap", 42)
        session._player.hp = 90
        before = view(session)
        session.use_potion()
        after = view(session)
        self.assertEqual(25, after.events[0].value)
        self.assertEqual(100, after.events[0].player_hp)
        self.assertEqual(set(), failures(before, after, "use_potion"))

    def test_potion_faults_fail_independent_rules(self) -> None:
        for profile, rule in (
            ("potion_overheal", R_POTION_CAP),
            ("potion_not_consumed", R_POTION_DECREMENTS),
        ):
            with self.subTest(profile=profile):
                session = self.lab(42, profile)
                before = view(session)
                session.use_potion()
                after = view(session)
                self.assertIn(rule, failures(before, after, "use_potion"))
                self.assertEqual(25, after.events[0].value)

    def test_enemy_distribution_and_win_balance(self) -> None:
        totals: Counter[str] = Counter()
        wins: Counter[str] = Counter()
        for seed in range(1000):
            session = CombatSession(str(seed), seed)
            enemy = session.snapshot().enemy_type
            totals[enemy] += 1
            session.use_potion()
            for _ in range(19):
                snapshot = session.snapshot()
                if snapshot.status.value != "active":
                    break
                if snapshot.player.hp < snapshot.player.max_hp and snapshot.player.potions:
                    session.use_potion()
                else:
                    session.attack()
            wins[enemy] += session.snapshot().status.value == "won"
        self.assertTrue(550 <= totals["slime"] <= 650)
        self.assertTrue(250 <= totals["goblin"] <= 350)
        self.assertTrue(70 <= totals["troll"] <= 130)
        self.assertEqual(totals["slime"], wins["slime"])
        self.assertGreater(wins["goblin"] / totals["goblin"], 0.75)
        self.assertEqual(0, wins["troll"])

    def test_loot_fault_variants(self) -> None:
        for seed, rule in ((0, R_LOOT_PRESENT), (1, R_LOOT_RANGE), (2, R_LOOT_ITEM)):
            with self.subTest(seed=seed):
                session = self.lab(seed, "loot_anomaly")
                self.assertEqual("slime", session.snapshot().enemy_type)
                while session.snapshot().status.value == "active":
                    before = view(session)
                    session.attack()
                after = view(session)
                self.assertIn(rule, failures(before, after, "attack"))

    def test_goblin_drops_ten_to_twenty_gold(self) -> None:
        session = CombatSession("goblin", 12, ruleset="v2")
        self.assertEqual("goblin", session.snapshot().enemy_type)
        session.use_potion()
        while session.snapshot().status.value == "active":
            before = view(session)
            if before.player.hp < before.player.max_hp and before.player.potions:
                session.use_potion()
                continue
            session.attack()
        after = view(session)
        self.assertEqual("won", after.status)
        drop = after.events[-1]
        self.assertEqual("drop", drop.kind)
        self.assertEqual("gold", drop.item)
        self.assertTrue(10 <= drop.value <= 20)
        self.assertEqual(drop.value, after.player.gold)
        self.assertEqual(set(), failures(before, after, "attack"))

    def test_player_death_followed_by_action_is_detected(self) -> None:
        session = self.lab(8, "retaliate_after_death")
        while session.snapshot().status.value == "active":
            session.attack()
        before = view(session)
        self.assertEqual("lost", before.status)
        self.assertEqual(409, derive_expectation(before, "attack").expected_status)
        session.attack()
        after = view(session)
        self.assertIn(R_NO_ACTION_AFTER_PLAYER_DEATH, failures(before, after, "attack"))

    def test_old_saved_battle_still_replays_with_v1_rules(self) -> None:
        session = CombatSession("legacy", 42, ruleset="v1")
        self.assertEqual(2, session.snapshot().player.potions)
        with self.assertRaises(InvalidCombatAction):
            session.use_potion()
        for _ in range(3):
            session.attack()
        original = session.snapshot()
        self.assertEqual("won", original.status.value)
        self.assertFalse(any(event.kind == "drop" for event in original.events))
        stored = StoredSession.from_snapshot(original)
        events = [
            StoredEvent(sequence=index, event=event)
            for index, event in enumerate(original.events, start=1)
        ]
        self.assertEqual(original, rebuild_session(stored, events).snapshot())


class BattleV2AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_skill_detects_all_fault_branches_and_replays(self) -> None:
        cases = (
            ("normal", 0, "pass", None),
            ("potion_overheal", 0, "defect_detected", R_POTION_CAP),
            ("potion_not_consumed", 0, "defect_detected", R_POTION_DECREMENTS),
            ("retaliate_after_death", 0, "defect_detected", "R-NO-RETALIATE-ON-KILL"),
            ("retaliate_after_death", 8, "defect_detected", R_NO_ACTION_AFTER_PLAYER_DEATH),
            ("loot_anomaly", 0, "defect_detected", R_LOOT_PRESENT),
            ("loot_anomaly", 1, "defect_detected", R_LOOT_RANGE),
            ("loot_anomaly", 2, "defect_detected", R_LOOT_ITEM),
        )
        with TemporaryDirectory() as directory:
            with patch("gamepilot.web_runs.WEB_REPORT_ROOT", Path(directory)):
                for profile, seed, verdict, rule in cases:
                    with self.subTest(profile=profile, seed=seed):
                        manager = AgentWebRunManager()
                        started = await manager.start(
                            {
                                "goal_id": "battle-end",
                                "skill_id": "injured-potion-then-attack",
                                "seed": seed,
                                "profile": profile,
                                "provider_mode": "offline",
                                "paid_ack": False,
                            }
                        )
                        assert manager._task is not None
                        await manager._task
                        record = manager.get(started["run_id"])
                        assert record is not None
                        self.assertEqual("completed", record["status"])
                        result = record["result"]
                        self.assertEqual(verdict, result["verdict"])
                        self.assertEqual("match", result["replay_outcome"])
                        if rule is not None:
                            self.assertIn(rule, result["target_rules_hit"])


if __name__ == "__main__":
    unittest.main()
