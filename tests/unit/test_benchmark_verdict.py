"""逐组合判定与退出码：清单核对、规则去重与「执行错误优先」。

这些用例直接测判定函数（模块内的私有工具）。它们是退出码的唯一来源，
反例（漏检、意外触发、执行错误）必须逐个验证，否则评测可能在
「没跑出结论」时返回 0。
"""

from collections.abc import Iterable

from gamepilot.benchmark.manifest import COMBINATIONS, Combination
from gamepilot.benchmark.models import CombinationEvidence, MetricResult, NegativeValidation
from gamepilot.benchmark.runner import (
    EXIT_DEVIATION,
    EXIT_EXECUTION_ERROR,
    EXIT_OK,
    _exit_code,
    _failing_rules,
    _matrix_verdict,
    _summary,
)
from gamepilot.lab import PROFILE_NORMAL
from gamepilot.testing.models import CaseReport, RuleCheck, Scenario
from gamepilot.testing.rules import R_INVARIANTS, R_POTION_CAP

HEAL_CASE = ("potion_overheal", "attack-then-potion")
NORMAL_CASE = (PROFILE_NORMAL, "attack-then-potion")


def _rule(rule_id: str, status: str) -> RuleCheck:
    return RuleCheck(rule_id=rule_id, status=status, expected="期望值", actual="实际值")


def _case(
    checks: Iterable[RuleCheck], status: str = "fail", error: str | None = None
) -> CaseReport:
    scenario = Scenario(case_id="case", description="说明", seed=42)
    return CaseReport(
        case_id=scenario.case_id,
        description=scenario.description,
        seed=scenario.seed,
        status=status,
        scenario=scenario,
        started_at="2026-09-18T00:00:00Z",
        finished_at="2026-09-18T00:00:01Z",
        duration_ms=1.0,
        checks=list(checks),
        error=error,
    )


def _combination(key: tuple[str, str]) -> Combination:
    return next(row for row in COMBINATIONS if (row.profile, row.case_id) == key)


def _evidence(row: Combination, **overrides: object) -> CombinationEvidence:
    data: dict[str, object] = {
        "profile": row.profile,
        "case_id": row.case_id,
        "fault_id": row.fault_id,
        "expect_trigger": row.expect_trigger,
        "expect_fail": row.expect_fail,
        "target_rules": list(row.target_rules),
        "run_id": "run",
        "report_path": "runs/x.json",
        "case_status": "fail" if row.expect_fail else "pass",
        "failing_rules": list(row.target_rules),
        "target_rules_hit": list(row.target_rules),
        "triggered": row.expect_trigger,
        "trigger_detail": "触发" if row.expect_trigger else None,
        "replay_run_id": "replay",
        "replay_path": "replays/x.json",
        "replay_outcome": "match",
        "replay_status": "fail" if row.expect_fail else "pass",
        "matrix_ok": True,
    }
    data.update(overrides)
    return CombinationEvidence(**data)


def _negative(passed: bool) -> NegativeValidation:
    return NegativeValidation(
        source_profile="potion_overheal",
        source_case_id="attack-then-potion",
        report_path="runs/source.json",
        replay_path="replays/negative.json",
        replay_outcome="mismatch" if passed else "match",
        execution_status="pass",
        passed=passed,
    )


def _metric(passed: bool, metric_id: str = "some_metric") -> MetricResult:
    return MetricResult(
        metric_id=metric_id,
        description="说明",
        numerator=int(passed),
        denominator=1,
        target="1/1",
        passed=passed,
    )


def _reference(**overrides: dict[str, object]) -> list[CombinationEvidence]:
    return [
        _evidence(row, **overrides.get(f"{row.profile}|{row.case_id}", {})) for row in COMBINATIONS
    ]


# ------------------------------------------------------------------ 规则去重


def test_failing_rules_are_deduplicated_and_only_include_failures() -> None:
    case = _case(
        [
            _rule(R_POTION_CAP, "fail"),
            _rule(R_POTION_CAP, "fail"),
            _rule(R_INVARIANTS, "fail"),
            _rule("R-POTION-HEAL", "pass"),
        ]
    )

    assert _failing_rules(case) == sorted([R_POTION_CAP, R_INVARIANTS])


def test_error_checks_are_not_counted_as_failures() -> None:
    """执行错误不属于规则失败，不能混进「命中了哪条规则」。"""
    assert _failing_rules(_case([_rule(R_POTION_CAP, "error")], status="error")) == []


# -------------------------------------------------------------------- 清单核对


def test_matching_combination_has_no_notes() -> None:
    row = _combination(HEAL_CASE)

    assert _matrix_verdict(row, _case([_rule(R_POTION_CAP, "fail")]), triggered=True) == []


def test_expected_failure_that_passed_is_reported() -> None:
    notes = _matrix_verdict(_combination(HEAL_CASE), _case([], status="pass"), triggered=True)

    assert any("预期失败，实际 pass" in note for note in notes)


def test_expected_pass_that_failed_is_reported() -> None:
    notes = _matrix_verdict(
        _combination(NORMAL_CASE),
        _case([_rule(R_INVARIANTS, "fail")], status="fail"),
        triggered=False,
    )

    assert any("预期通过，实际 fail" in note for note in notes)


def test_missed_target_rule_is_reported() -> None:
    notes = _matrix_verdict(
        _combination(HEAL_CASE), _case([_rule(R_INVARIANTS, "fail")], status="fail"), triggered=True
    )

    assert any(R_POTION_CAP in note and "漏检" in note for note in notes)


def test_missing_trigger_record_is_reported() -> None:
    notes = _matrix_verdict(
        _combination(HEAL_CASE), _case([_rule(R_POTION_CAP, "fail")]), triggered=False
    )

    assert any("没有记录到真实触发" in note for note in notes)


def test_unexpected_trigger_is_reported() -> None:
    notes = _matrix_verdict(_combination(NORMAL_CASE), _case([], status="pass"), triggered=True)

    assert any("未预期的缺陷触发" in note for note in notes)


def test_execution_error_is_reported_as_such() -> None:
    notes = _matrix_verdict(
        _combination(HEAL_CASE), _case([], status="error", error="连接失败"), triggered=False
    )

    assert any("执行错误：连接失败" in note for note in notes)


# --------------------------------------------------------------------- 退出码


def test_exit_code_is_zero_only_when_everything_matches() -> None:
    assert _exit_code("ok") == EXIT_OK
    assert _exit_code("deviation") == EXIT_DEVIATION
    assert _exit_code("execution_error") == EXIT_EXECUTION_ERROR


def test_summary_reports_full_conformance() -> None:
    summary = _summary(
        _reference(), [_metric(True, "a"), _metric(True, "b")], negative=_negative(True)
    )

    assert (summary.status, summary.exit_code) == ("ok", EXIT_OK)
    assert (summary.combinations_planned, summary.combinations_executed) == (32, 32)
    assert (summary.as_expected, summary.deviations, summary.execution_errors) == (32, 0, 0)
    assert (summary.metrics_passed, summary.metrics_total) == (2, 2)
    assert summary.negative_validation_passed is True


def test_summary_is_a_deviation_when_a_metric_fails() -> None:
    summary = _summary(
        _reference(), [_metric(True, "a"), _metric(False, "b")], negative=_negative(True)
    )

    assert (summary.status, summary.exit_code) == ("deviation", EXIT_DEVIATION)
    assert summary.metrics_passed == 1


def test_summary_is_a_deviation_when_negative_validation_fails() -> None:
    summary = _summary(_reference(), [_metric(True, "a")], negative=_negative(False))

    assert (summary.status, summary.exit_code) == ("deviation", EXIT_DEVIATION)
    assert summary.negative_validation_passed is False


def test_summary_is_a_deviation_when_a_combination_does_not_match() -> None:
    evidences = _reference()
    evidences[0].matrix_ok = False

    summary = _summary(evidences, [_metric(True, "a")], negative=_negative(True))

    assert (summary.status, summary.exit_code) == ("deviation", EXIT_DEVIATION)
    assert (summary.as_expected, summary.deviations) == (31, 1)


def test_execution_error_dominates_every_other_verdict() -> None:
    """执行错误与偏差同时出现时必须返回 2，不能声称「只是漏检」。"""
    evidences = _reference()
    evidences[0].matrix_ok = False
    evidences[1].case_status = "error"
    evidences[1].matrix_ok = False

    summary = _summary(
        evidences, [_metric(False, "a"), _metric(False, "b")], negative=_negative(False)
    )

    assert (summary.status, summary.exit_code) == ("execution_error", EXIT_EXECUTION_ERROR)
    assert (summary.deviations, summary.execution_errors) == (2, 1)
    # 底层分项仍然如实记录，退出码只是更严格的那一个。
    assert summary.metrics_passed == 0
    assert summary.negative_validation_passed is False


def test_summary_counts_only_executed_combinations() -> None:
    summary = _summary(_reference()[:5], [_metric(True, "a")], negative=_negative(True))

    assert summary.combinations_planned == 32
    assert summary.combinations_executed == 5
