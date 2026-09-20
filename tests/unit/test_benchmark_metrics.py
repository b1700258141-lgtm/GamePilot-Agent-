"""指标口径的反例测试：漏检、误报、执行错误、重复命中与不可比较。

基线证据集按固定清单构造「全部符合预期」的形态，然后**只改一处**，
验证该处是否让正确的指标变化、且不让其他指标跟着乱动。
分母始终来自清单，因此任何「没跑到」都不会让分母变小。
"""

from collections.abc import Iterable

from gamepilot.benchmark.manifest import COMBINATIONS, Combination
from gamepilot.benchmark.metrics import METRIC_IDS, build_metrics
from gamepilot.benchmark.models import CombinationEvidence, MetricResult
from gamepilot.lab import PROFILE_NORMAL
from gamepilot.testing.rules import R_INVARIANTS, R_POTION_CAP

Overrides = dict[tuple[str, str], dict[str, object]]
HEAL_CASE = ("potion_overheal", "attack-then-potion")
NORMAL_CASE = (PROFILE_NORMAL, "attack-then-potion")
VARIANT_PASS_CASE = ("potion_overheal", "initial-state")


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
        "target_rules_missed": [],
        "other_failing_rules": [],
        "triggered": row.expect_trigger,
        "trigger_detail": "触发" if row.expect_trigger else None,
        "unexpected_trigger": False,
        "replay_run_id": "replay",
        "replay_path": "replays/x.json",
        "replay_outcome": "match",
        "replay_status": "fail" if row.expect_fail else "pass",
        "replay_differences": 0,
        "matrix_ok": True,
        "notes": [],
    }
    data.update(overrides)
    return CombinationEvidence(**data)


def _reference(changed: Overrides | None = None) -> list[CombinationEvidence]:
    """按清单生成基线证据；changed 只允许改动指定的组合。"""
    overrides = changed or {}
    return [_evidence(row, **overrides.get((row.profile, row.case_id), {})) for row in COMBINATIONS]


def _metric(metrics: Iterable[MetricResult], metric_id: str) -> MetricResult:
    return next(item for item in metrics if item.metric_id == metric_id)


def _details(metric: MetricResult) -> str:
    return " / ".join(metric.details)


def test_metric_ids_are_the_documented_seven() -> None:
    assert METRIC_IDS == (
        "defect_coverage",
        "trigger_accuracy",
        "target_detection",
        "normal_false_positive",
        "variant_false_positive",
        "execution_errors",
        "replay_consistency",
    )
    assert [item.metric_id for item in build_metrics(_reference())] == list(METRIC_IDS)


def test_reference_matrix_passes_every_metric_with_expected_denominators() -> None:
    metrics = build_metrics(_reference())

    assert [(m.metric_id, m.numerator, m.denominator, m.passed) for m in metrics] == [
        ("defect_coverage", 3, 3, True),
        ("trigger_accuracy", 9, 9, True),
        ("target_detection", 9, 9, True),
        ("normal_false_positive", 0, 8, True),
        ("variant_false_positive", 0, 15, True),
        ("execution_errors", 0, 32, True),
        ("replay_consistency", 32, 32, True),
    ]


# ------------------------------------------------------------------------- 漏检


def test_missed_target_rule_lowers_detection_but_keeps_trigger_accuracy() -> None:
    """靶场确实触发了，但 oracle 没命中目标规则：这是漏检，不是「没触发」。"""
    metrics = build_metrics(
        _reference(
            {
                HEAL_CASE: {
                    "case_status": "pass",
                    "failing_rules": [],
                    "target_rules_hit": [],
                    "target_rules_missed": [R_POTION_CAP],
                }
            }
        )
    )

    detection = _metric(metrics, "target_detection")
    assert (detection.numerator, detection.denominator, detection.passed) == (8, 9, False)
    assert R_POTION_CAP in _details(detection)

    # 触发记录仍在，触发正确率不下降。
    assert _metric(metrics, "trigger_accuracy").numerator == 9
    # 指定触发轨迹少了一条，缺陷覆盖下降。
    assert _metric(metrics, "defect_coverage").numerator == 2


def test_missing_execution_evidence_is_counted_as_not_detected() -> None:
    """没有执行证据既不算发现，也不从分母里删掉。"""
    metrics = build_metrics(_reference({HEAL_CASE: {"triggered": False, "trigger_detail": None}}))

    assert _metric(metrics, "trigger_accuracy").numerator == 8
    assert _metric(metrics, "defect_coverage").numerator == 2


# ------------------------------------------------------- 非目标 fail 与重复命中


def test_extra_non_target_failures_do_not_inflate_detection() -> None:
    """同一缺陷同时命中多条规则时只算一次发现。"""
    overrides: Overrides = {
        (row.profile, row.case_id): {"other_failing_rules": [R_INVARIANTS]}
        for row in COMBINATIONS
        if row.expect_fail
    }
    metrics = build_metrics(_reference(overrides))

    assert _metric(metrics, "target_detection").numerator == 9
    assert _metric(metrics, "defect_coverage").numerator == 3
    assert "按去重只计一次发现" in _details(_metric(metrics, "target_detection"))


def test_variant_failure_on_a_passing_combination_is_a_false_positive() -> None:
    metrics = build_metrics(
        _reference(
            {
                VARIANT_PASS_CASE: {
                    "case_status": "fail",
                    "failing_rules": [R_INVARIANTS],
                }
            }
        )
    )

    false_positive = _metric(metrics, "variant_false_positive")
    assert (false_positive.numerator, false_positive.denominator, false_positive.passed) == (
        1,
        15,
        False,
    )
    # 预期通过的组合上没有缺陷，因此触发正确率与检测率的分子都不受影响。
    assert _metric(metrics, "target_detection").numerator == 9


def test_unexpected_trigger_on_a_passing_combination_is_a_false_positive() -> None:
    metrics = build_metrics(
        _reference(
            {
                VARIANT_PASS_CASE: {
                    "triggered": True,
                    "trigger_detail": "意外触发",
                    "unexpected_trigger": True,
                }
            }
        )
    )

    assert _metric(metrics, "variant_false_positive").numerator == 1
    assert "意外触发" in _details(_metric(metrics, "variant_false_positive"))


def test_normal_failure_is_a_false_positive_and_not_a_detection() -> None:
    metrics = build_metrics(
        _reference({NORMAL_CASE: {"case_status": "fail", "failing_rules": [R_INVARIANTS]}})
    )

    false_positive = _metric(metrics, "normal_false_positive")
    assert (false_positive.numerator, false_positive.denominator, false_positive.passed) == (
        1,
        8,
        False,
    )
    # normal 组合不在目标失败集合里，检测率与触发率的分子都不变。
    assert _metric(metrics, "target_detection").numerator == 9
    assert _metric(metrics, "trigger_accuracy").numerator == 9


# --------------------------------------------------------- 执行错误与不可比较


def test_execution_error_is_counted_separately_and_kept_in_denominators() -> None:
    metrics = build_metrics(
        _reference(
            {
                HEAL_CASE: {
                    "case_status": "error",
                    "failing_rules": [],
                    "target_rules_hit": [],
                    "target_rules_missed": [R_POTION_CAP],
                    "triggered": False,
                    "trigger_detail": None,
                    "replay_outcome": "not_comparable",
                    "replay_differences": 0,
                    "notes": ["执行错误：连接失败"],
                }
            }
        )
    )

    errors = _metric(metrics, "execution_errors")
    assert (errors.numerator, errors.denominator, errors.passed) == (1, 32, False)
    # 执行错误不算发现缺陷，也不从别的指标分母里消失。
    assert _metric(metrics, "target_detection").numerator == 8
    assert _metric(metrics, "target_detection").denominator == 9
    assert _metric(metrics, "defect_coverage").numerator == 2
    assert _metric(metrics, "defect_coverage").denominator == 3


def test_not_comparable_replay_stays_in_the_denominator() -> None:
    metrics = build_metrics(
        _reference({HEAL_CASE: {"replay_outcome": "not_comparable", "replay_differences": 3}})
    )

    consistency = _metric(metrics, "replay_consistency")
    assert (consistency.numerator, consistency.denominator, consistency.passed) == (31, 32, False)
    assert "not_comparable" in _details(consistency)
    # 缺陷结论本身仍然成立：重跑不可比较不等于缺陷没被检出。
    assert _metric(metrics, "target_detection").numerator == 9


def test_mismatched_replay_is_not_counted_as_consistent() -> None:
    metrics = build_metrics(
        _reference({HEAL_CASE: {"replay_outcome": "mismatch", "replay_differences": 2}})
    )

    consistency = _metric(metrics, "replay_consistency")
    assert consistency.numerator == 31
    assert consistency.passed is False
    assert _metric(metrics, "defect_coverage").numerator == 3
