"""指标口径：分子、分母与达标判定。

口径约定（与任务单第 6 节一致）：

- 分母来自固定清单，不来自「实际跑到的组合数」：执行错误单独计数，
  既不算发现缺陷，也不从分母里删掉；
- 目标检测以「该场景所有 status=fail 的 RuleCheck」为准，并按 rule_id 去重，
  同一缺陷命中多条规则只算一次发现；
- 只命中非目标规则不算发现缺陷（例如缺陷同时触发了 R-INVARIANTS）。
"""

from collections.abc import Iterable, Mapping

from gamepilot.lab import PROFILE_NORMAL

from .manifest import COMBINATIONS, DESIGNATED_TRIGGERS
from .models import CombinationEvidence, MetricResult

_KEY = tuple[str, str]

METRIC_DEFECT_COVERAGE = "defect_coverage"
METRIC_TRIGGER_ACCURACY = "trigger_accuracy"
METRIC_TARGET_DETECTION = "target_detection"
METRIC_NORMAL_FALSE_POSITIVE = "normal_false_positive"
METRIC_VARIANT_FALSE_POSITIVE = "variant_false_positive"
METRIC_EXECUTION_ERRORS = "execution_errors"
METRIC_REPLAY_CONSISTENCY = "replay_consistency"

# 报告里的指标顺序；漏掉任何一项都应该在自检里被发现。
METRIC_IDS: tuple[str, ...] = (
    METRIC_DEFECT_COVERAGE,
    METRIC_TRIGGER_ACCURACY,
    METRIC_TARGET_DETECTION,
    METRIC_NORMAL_FALSE_POSITIVE,
    METRIC_VARIANT_FALSE_POSITIVE,
    METRIC_EXECUTION_ERRORS,
    METRIC_REPLAY_CONSISTENCY,
)


def _index(evidences: Iterable[CombinationEvidence]) -> dict[_KEY, CombinationEvidence]:
    return {(item.profile, item.case_id): item for item in evidences}


def _planned(*, expect_fail: bool | None = None, profile: str | None = None) -> list[_KEY]:
    return [
        (row.profile, row.case_id)
        for row in COMBINATIONS
        if (expect_fail is None or row.expect_fail is expect_fail)
        and (profile is None or row.profile == profile)
    ]


def _detected(item: CombinationEvidence | None) -> bool:
    """触发成立且目标规则全部命中，才算这次缺陷被检出。"""
    if item is None:
        return False
    return item.triggered and not item.target_rules_missed


def build_metrics(evidences: Iterable[CombinationEvidence]) -> list[MetricResult]:
    """按固定清单计算全部指标。"""
    index = _index(evidences)
    target_fail = _planned(expect_fail=True)
    normal_pass = _planned(expect_fail=False, profile=PROFILE_NORMAL)
    normal_pass_keys = set(normal_pass)
    variant_pass = [key for key in _planned(expect_fail=False) if key not in normal_pass_keys]

    return [
        _defect_coverage(index),
        _trigger_accuracy(index, target_fail),
        _target_detection(index, target_fail),
        _normal_false_positive(index, normal_pass),
        _variant_false_positive(index, variant_pass),
        _execution_errors(index),
        _replay_consistency(index, target_fail),
    ]


def _defect_coverage(index: Mapping[_KEY, CombinationEvidence]) -> MetricResult:
    """缺陷覆盖：指定触发场景中「真实触发且命中目标规则」的不同缺陷数 / 3。"""
    covered: set[str] = set()
    details: list[str] = []
    for key in DESIGNATED_TRIGGERS:
        item = index.get(key)
        if _detected(item) and item is not None and item.fault_id is not None:
            covered.add(item.fault_id)
        else:
            details.append(f"{key[0]} × {key[1]}：未同时满足「触发」与「命中目标规则」")
    return MetricResult(
        metric_id=METRIC_DEFECT_COVERAGE,
        description="三个指定触发场景中，真实触发且命中目标规则的不同缺陷数",
        numerator=len(covered),
        denominator=len(DESIGNATED_TRIGGERS),
        target=f"{len(DESIGNATED_TRIGGERS)}/{len(DESIGNATED_TRIGGERS)}",
        passed=len(covered) == len(DESIGNATED_TRIGGERS),
        details=details,
    )


def _trigger_accuracy(
    index: Mapping[_KEY, CombinationEvidence], target_fail: list[_KEY]
) -> MetricResult:
    """触发正确率：目标失败组合中，lab 记录到真实触发的组合数 / 9。"""
    triggered = [key for key in target_fail if (item := index.get(key)) and item.triggered]
    triggered_keys = set(triggered)
    details = [
        f"{profile} × {case_id}：lab 未记录到触发"
        for profile, case_id in target_fail
        if (profile, case_id) not in triggered_keys
    ]
    return MetricResult(
        metric_id=METRIC_TRIGGER_ACCURACY,
        description="目标失败组合中，靶场记录到真实触发的组合数",
        numerator=len(triggered),
        denominator=len(target_fail),
        target=f"{len(target_fail)}/{len(target_fail)}",
        passed=len(triggered) == len(target_fail),
        details=details,
    )


def _target_detection(
    index: Mapping[_KEY, CombinationEvidence], target_fail: list[_KEY]
) -> MetricResult:
    """目标检测率：触发成立且 oracle 命中指定规则的组合数 / 9。

    另列漏检（目标规则没被命中）与「额外失败的非目标规则」。
    """
    detected = [key for key in target_fail if _detected(index.get(key))]
    details: list[str] = []
    for key in target_fail:
        item = index.get(key)
        if item is None:
            details.append(f"{key[0]} × {key[1]}：没有执行证据")
            continue
        if item.target_rules_missed:
            details.append(
                f"{key[0]} × {key[1]}：漏检目标规则 {', '.join(item.target_rules_missed)}"
                f"（实际失败规则：{', '.join(item.failing_rules) or '无'}）"
            )
        if item.other_failing_rules:
            details.append(
                f"{key[0]} × {key[1]}：同时命中非目标规则 "
                f"{', '.join(item.other_failing_rules)}（按去重只计一次发现）"
            )
    return MetricResult(
        metric_id=METRIC_TARGET_DETECTION,
        description="目标失败组合中，触发成立且命中指定规则的组合数",
        numerator=len(detected),
        denominator=len(target_fail),
        target=f"{len(target_fail)}/{len(target_fail)}",
        passed=len(detected) == len(target_fail),
        details=details,
    )


def _normal_false_positive(
    index: Mapping[_KEY, CombinationEvidence], normal_pass: list[_KEY]
) -> MetricResult:
    """正常对照误报：normal 上被判失败的场景数 / 8。"""
    failed = [key for key in normal_pass if (item := index.get(key)) and item.case_status == "fail"]
    errored = [
        key for key in normal_pass if (item := index.get(key)) and item.case_status == "error"
    ]
    details = [f"normal × {case_id}：被判定为失败" for _, case_id in failed]
    details.extend(
        f"normal × {case_id}：执行错误（单独计入执行错误指标）" for _, case_id in errored
    )
    return MetricResult(
        metric_id=METRIC_NORMAL_FALSE_POSITIVE,
        description="正常对照（normal）上被判定为失败的场景数",
        numerator=len(failed),
        denominator=len(normal_pass),
        target=f"0/{len(normal_pass)}",
        passed=not failed,
        details=details,
    )


def _variant_false_positive(
    index: Mapping[_KEY, CombinationEvidence], variant_pass: list[_KEY]
) -> MetricResult:
    """未触发变体误报：三个变体的预期通过组合上失败（或意外触发）的数量 / 15。"""
    offenders: list[_KEY] = []
    details: list[str] = []
    for key in variant_pass:
        item = index.get(key)
        if item is None:
            details.append(f"{key[0]} × {key[1]}：没有执行证据")
            continue
        if item.case_status == "fail":
            offenders.append(key)
            details.append(f"{key[0]} × {key[1]}：预期通过但被判定为失败")
        elif item.case_status == "error":
            details.append(f"{key[0]} × {key[1]}：执行错误（单独计入执行错误指标）")
        if item.unexpected_trigger:
            offenders.append(key)
            details.append(f"{key[0]} × {key[1]}：意外触发（矩阵不符）")
    unique = len(set(offenders))
    return MetricResult(
        metric_id=METRIC_VARIANT_FALSE_POSITIVE,
        description="三个变体的预期通过组合中，失败或意外触发的组合数",
        numerator=unique,
        denominator=len(variant_pass),
        target=f"0/{len(variant_pass)}",
        passed=unique == 0,
        details=details,
    )


def _execution_errors(index: Mapping[_KEY, CombinationEvidence]) -> MetricResult:
    """执行错误数：32 个组合中的 error 数。"""
    all_keys = _planned()
    errored = [key for key in all_keys if (item := index.get(key)) and item.case_status == "error"]
    details = [f"{profile} × {case_id}：执行错误" for profile, case_id in errored]
    return MetricResult(
        metric_id=METRIC_EXECUTION_ERRORS,
        description="32 个原始运行的执行错误数（重放与负向验证错误在摘要中单列）",
        numerator=len(errored),
        denominator=len(all_keys),
        target=f"0/{len(all_keys)}",
        passed=not errored,
        details=details,
    )


def _replay_consistency(
    index: Mapping[_KEY, CombinationEvidence], target_fail: list[_KEY]
) -> MetricResult:
    """重跑一致率：同 profile 新会话重跑后逐字段一致的组合数 / 32。

    另列 9 个缺陷组合的复现情况：失败结论也应当被一致复现。
    失败与不可比较都不从分母里剔除。
    """
    all_keys = _planned()
    matched = [
        key for key in all_keys if (item := index.get(key)) and item.replay_outcome == "match"
    ]
    defect_matched = [
        key for key in target_fail if (item := index.get(key)) and item.replay_outcome == "match"
    ]
    details: list[str] = []
    for key in all_keys:
        item = index.get(key)
        if item is None:
            details.append(f"{key[0]} × {key[1]}：没有重跑证据")
        elif item.replay_outcome != "match":
            details.append(
                f"{key[0]} × {key[1]}：重跑 {item.replay_outcome}"
                f"（{item.replay_differences} 处差异）"
            )
    details.append(f"其中缺陷组合复现：{len(defect_matched)}/{len(target_fail)}")
    return MetricResult(
        metric_id=METRIC_REPLAY_CONSISTENCY,
        description="同 profile 新会话重跑后与原报告逐字段一致的组合数",
        numerator=len(matched),
        denominator=len(all_keys),
        target=f"{len(all_keys)}/{len(all_keys)}",
        passed=len(matched) == len(all_keys),
        details=details,
    )
