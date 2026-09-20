"""固定评测清单：矩阵规模、9/23 划分、目标规则与指定触发轨迹。

清单是先于运行写死的标准答案。这里逐项核对它的形状，
防止有人在结果不符时改清单去迎合实现（例如删掉某一格）。
"""

import pytest

from gamepilot.benchmark import manifest
from gamepilot.lab import FAULT_IDS, PROFILE_NORMAL, PROFILES
from gamepilot.testing.rules import RULE_CATALOG
from gamepilot.testing.scenarios import BASELINE_SCENARIOS, SUITE_BASELINE

PROFILE_FAIL_COUNTS = {
    PROFILE_NORMAL: 0,
    "potion_overheal": 4,
    "potion_not_consumed": 4,
    "retaliate_after_death": 1,
}


def test_matrix_is_four_profiles_by_eight_scenarios() -> None:
    assert len(PROFILES) == 4
    assert len(BASELINE_SCENARIOS) == 8
    assert len(manifest.COMBINATIONS) == 32


def test_matrix_covers_every_profile_and_case_exactly_once() -> None:
    keys = [(row.profile, row.case_id) for row in manifest.COMBINATIONS]

    assert len(set(keys)) == len(keys)
    assert {profile for profile, _ in keys} == set(PROFILES)
    assert {case_id for _, case_id in keys} == set(manifest.BASELINE_CASE_IDS)


def test_expected_split_is_nine_failing_and_twenty_three_passing() -> None:
    failing = [row for row in manifest.COMBINATIONS if row.expect_fail]

    assert len(failing) == 9
    assert len(manifest.COMBINATIONS) - len(failing) == 23


@pytest.mark.parametrize(("profile", "expected"), PROFILE_FAIL_COUNTS.items())
def test_failures_per_profile_match_the_task_spec(profile: str, expected: int) -> None:
    failures = [row for row in manifest.COMBINATIONS if row.profile == profile and row.expect_fail]

    assert len(failures) == expected


def test_expectation_flags_agree_with_target_rules() -> None:
    for row in manifest.COMBINATIONS:
        assert row.expect_fail == bool(row.target_rules)
        assert row.expect_trigger == bool(row.target_rules)


def test_fault_id_is_present_exactly_for_the_variants() -> None:
    assert {row.fault_id for row in manifest.COMBINATIONS if row.profile == PROFILE_NORMAL} == {
        None
    }
    assert {row.fault_id for row in manifest.COMBINATIONS if row.profile != PROFILE_NORMAL} == set(
        FAULT_IDS
    )


def test_target_rules_are_known_rule_ids() -> None:
    target_rules = {rule for row in manifest.COMBINATIONS for rule in row.target_rules}

    assert target_rules
    assert target_rules <= set(RULE_CATALOG)


def test_designated_triggers_are_failing_combinations_of_distinct_faults() -> None:
    index = {(row.profile, row.case_id): row for row in manifest.COMBINATIONS}

    assert len(manifest.DESIGNATED_TRIGGERS) == 3
    faults = set()
    for key in manifest.DESIGNATED_TRIGGERS:
        row = index[key]
        assert row.expect_fail
        assert row.target_rules
        faults.add(row.fault_id)
    # 三个缺陷各有一条最小触发轨迹，缺一不可。
    assert faults == set(FAULT_IDS)


def test_negative_validation_picks_a_designated_trigger() -> None:
    assert manifest.NEGATIVE_VALIDATION_SOURCE in manifest.DESIGNATED_TRIGGERS


def test_suite_and_scenario_lookup_are_consistent() -> None:
    assert manifest.SUITE == SUITE_BASELINE
    assert set(manifest.BASELINE_SCENARIO_BY_ID) == set(manifest.BASELINE_CASE_IDS)
    for scenario in BASELINE_SCENARIOS:
        assert manifest.BASELINE_SCENARIO_BY_ID[scenario.case_id] is scenario


def test_manifest_declares_a_pinned_version_and_source() -> None:
    assert manifest.BENCHMARK_VERSION == "1.1.0"
    assert manifest.MANIFEST_SOURCE
