"""v2 固定评测清单：5 profile × 8 baseline 场景的预期结果。

清单是**先于运行写死的标准答案**，不在运行后根据 oracle 输出反向生成：
如果实际结果与清单不符，评测报告要如实列出差异，而不是反过来改清单去迎合实现。

矩阵按 `docs/BATTLE_RULES_V2.md` 定义：5 个 profile × 8 个 baseline 场景
共 40 个组合，其中 13 个预期触发缺陷，normal 的 8 个是误报对照。

「预期触发」与「预期失败」在本矩阵中等价：变体上只要缺陷真的被触发，
对应的目标规则就应当失败；反过来，未被触发的组合必须与 normal 完全一致。
"""

from dataclasses import dataclass

from gamepilot.lab import (
    FAULT_LOOT_ANOMALY,
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_CATALOG,
    PROFILES,
)
from gamepilot.testing.rules import (
    R_LOOT_PRESENT,
    R_NO_ACTION_AFTER_PLAYER_DEATH,
    R_NO_RETALIATE_ON_KILL,
    R_POTION_CAP,
    R_POTION_DECREMENTS,
)
from gamepilot.testing.scenarios import BASELINE_SCENARIOS, SUITE_BASELINE

BENCHMARK_VERSION = "2.0.0"
MANIFEST_SOURCE = "docs/BATTLE_RULES_V2.md#缺陷靶场"

BASELINE_CASE_IDS: tuple[str, ...] = tuple(scenario.case_id for scenario in BASELINE_SCENARIOS)

# 组合按 case_id 取场景定义：清单只引用既有场景，不复制也不改写场景内容。
BASELINE_SCENARIO_BY_ID = {scenario.case_id: scenario for scenario in BASELINE_SCENARIOS}

# 每个缺陷在哪些场景上预期触发（其余组合一律预期通过）。
# 目标规则集合是该场景**必须命中**的规则编号；命中多条时按编号去重计数。
EXPECTED_TRIGGERS: dict[str, dict[str, tuple[str, ...]]] = {
    FAULT_POTION_OVERHEAL: {
        "potion-at-full-hp-capped": (R_POTION_CAP,),
        "attack-then-potion": (R_POTION_CAP,),
        "potion-exhaustion": (R_POTION_CAP,),
        "rejected-action-keeps-rng": (R_POTION_CAP,),
        "reproducible-duplicate-run": (R_POTION_CAP,),
    },
    FAULT_POTION_NOT_CONSUMED: {
        "potion-at-full-hp-capped": (R_POTION_DECREMENTS,),
        "attack-then-potion": (R_POTION_DECREMENTS,),
        "potion-exhaustion": (R_POTION_DECREMENTS,),
        "rejected-action-keeps-rng": (R_POTION_DECREMENTS,),
        "reproducible-duplicate-run": (R_POTION_DECREMENTS,),
    },
    FAULT_RETALIATE_AFTER_DEATH: {
        "win-then-rejected": (R_NO_RETALIATE_ON_KILL,),
        "lose-then-rejected": (R_NO_ACTION_AFTER_PLAYER_DEATH,),
    },
    FAULT_LOOT_ANOMALY: {
        "win-then-rejected": (R_LOOT_PRESENT,),
    },
}


@dataclass(frozen=True)
class Combination:
    """清单里的一格：某个 profile 上某个场景的预期结果。"""

    profile: str
    case_id: str
    expect_trigger: bool
    target_rules: tuple[str, ...]

    @property
    def fault_id(self) -> str | None:
        """本组合所属的缺陷编号（normal 列为 None）。"""
        return PROFILE_CATALOG[self.profile].fault_id

    @property
    def expect_fail(self) -> bool:
        """本组合是否预期被 oracle 判定为失败。

        当前矩阵里「预期触发」与「预期失败」等价：目标规则集合非空即为预期失败。
        """
        return bool(self.target_rules)


def _build_combinations() -> tuple[Combination, ...]:
    rows: list[Combination] = []
    for profile in PROFILES:
        expected = EXPECTED_TRIGGERS.get(profile, {})
        for case_id in BASELINE_CASE_IDS:
            target_rules = expected.get(case_id, ())
            rows.append(
                Combination(
                    profile=profile,
                    case_id=case_id,
                    expect_trigger=bool(target_rules),
                    target_rules=target_rules,
                )
            )
    return tuple(rows)


COMBINATIONS: tuple[Combination, ...] = _build_combinations()

# 每种缺陷的一条指定触发轨迹。
DESIGNATED_TRIGGERS: tuple[tuple[str, str], ...] = (
    (FAULT_POTION_OVERHEAL, "attack-then-potion"),
    (FAULT_POTION_NOT_CONSUMED, "attack-then-potion"),
    (FAULT_RETALIATE_AFTER_DEATH, "win-then-rejected"),
    (FAULT_LOOT_ANOMALY, "win-then-rejected"),
)

# 负向验证：把一个缺陷轨迹放到 normal 上重放，必须比对出差异。
# 这一项不计入固定矩阵的同 profile 重跑，它证明「动作相同」不等于「缺陷复现」。
NEGATIVE_VALIDATION_SOURCE: tuple[str, str] = (FAULT_POTION_OVERHEAL, "attack-then-potion")

SUITE = SUITE_BASELINE
