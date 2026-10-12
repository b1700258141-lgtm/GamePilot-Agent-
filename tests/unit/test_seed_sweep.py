"""固定跨种子清单及缺陷触发/独立规则判定的分离。"""

import pytest

from gamepilot.benchmark.seed_sweep import SEEDS, TRACE_ACTIONS, _scenarios, _verdict
from gamepilot.lab import FAULT_POTION_NOT_CONSUMED, PROFILE_NORMAL
from gamepilot.testing.models import CaseReport, ReplayCaseReport, RuleCheck, Scenario
from gamepilot.testing.rules import R_POTION_DECREMENTS


def _case(status: str, *, target_hit: bool = False) -> CaseReport:
    scenario = Scenario(case_id="attack-potion-seed-0000", description="固定脚本", seed=0)
    return CaseReport(
        case_id=scenario.case_id,
        description=scenario.description,
        seed=scenario.seed,
        status=status,
        scenario=scenario,
        started_at="2026-10-12T00:00:00Z",
        finished_at="2026-10-12T00:00:01Z",
        duration_ms=1,
        checks=(
            [RuleCheck(rule_id=R_POTION_DECREMENTS, status="fail", expected="1", actual="2")]
            if target_hit
            else []
        ),
    )


def _replay(case: CaseReport, outcome: str = "match") -> ReplayCaseReport:
    return ReplayCaseReport(case_id=case.case_id, outcome=outcome, execution=case)


def test_seed_sweep_manifest_is_fixed_and_unique() -> None:
    scenarios = _scenarios()
    assert SEEDS == tuple(range(32))
    assert len(scenarios) == 32 * len(TRACE_ACTIONS)
    assert len({scenario.case_id for scenario in scenarios}) == len(scenarios)
    assert {scenario.seed for scenario in scenarios} == set(SEEDS)
    assert {tuple(step.action for step in scenario.steps) for scenario in scenarios} == set(
        TRACE_ACTIONS.values()
    )


@pytest.mark.parametrize(
    ("status", "target_hit", "trigger", "outcome", "expected"),
    [
        ("pass", False, None, "match", "ok"),
        ("fail", True, FAULT_POTION_NOT_CONSUMED, "match", "ok"),
        ("pass", False, FAULT_POTION_NOT_CONSUMED, "match", "deviation"),
        ("fail", False, FAULT_POTION_NOT_CONSUMED, "match", "deviation"),
        ("fail", True, None, "match", "deviation"),
        ("fail", True, "wrong_fault", "match", "deviation"),
        ("fail", True, FAULT_POTION_NOT_CONSUMED, "mismatch", "deviation"),
        ("error", False, None, "not_comparable", "execution_error"),
    ],
)
def test_sweep_verdict_requires_trigger_and_oracle_agreement(
    status: str, target_hit: bool, trigger: str | None, outcome: str, expected: str
) -> None:
    case = _case(status, target_hit=target_hit)
    cell = _verdict(FAULT_POTION_NOT_CONSUMED, case, _replay(case, outcome), trigger)
    assert cell.verdict == expected
    assert cell.triggered is (trigger is not None)


def test_normal_profile_rejects_rule_failure_and_trigger() -> None:
    passed = _case("pass")
    failed = _case("fail", target_hit=True)
    assert _verdict(PROFILE_NORMAL, passed, _replay(passed), None).verdict == "ok"
    assert _verdict(PROFILE_NORMAL, failed, _replay(failed), None).verdict == "deviation"
    assert _verdict(PROFILE_NORMAL, passed, _replay(passed), "unexpected").verdict == "deviation"
