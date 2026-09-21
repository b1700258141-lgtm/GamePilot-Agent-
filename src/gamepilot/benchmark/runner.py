"""批量评测执行：组装四个隔离应用，跑固定矩阵并做同 profile 重跑。

约定：

- 四个 profile 各自一个独立应用实例与独立内存仓储，normal 与变体互不影响；
- normal 与变体走**同一套** GameClient、runner、oracle 与 replay：
  评测不做任何特殊判定，也不改写 oracle 去迎合缺陷；
- 每个组合的原始报告与重跑报告都落盘并保留，文件名由生成的 run_id 决定，
  已存在的文件不会被覆盖；
- 报告里的地址是进程内 ASGI 应用的占位地址（`benchmark.invalid`），
  不声称使用了真实网络；profile 与缺陷的对应关系只记录在评测摘要里。

报告布局（`<output-dir>/<benchmark run_id>/` 下）：

```text
benchmark.json                              评测摘要
runs/<profile>/<case_id>/<run_id>.json      原始运行报告（1 个场景）
replays/<profile>/<case_id>/<run_id>.json   同 profile 重跑报告
replays/negative/<case_id>/<run_id>.json    负向验证重跑报告（不计入 32 次）
```
"""

import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from gamepilot.lab import PROFILE_CATALOG, PROFILE_NORMAL, PROFILES, TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import (
    CaseReport,
    CaseStatus,
    ReplayCaseReport,
    ReplayReport,
    RunReport,
)
from gamepilot.testing.replay import ReplayInputError, load_report, replay_report
from gamepilot.testing.rules import (
    DEFAULT_TIMEOUT_SECONDS,
    RULES_SOURCE,
    RULES_VERSION,
    SCHEMA_VERSION,
)
from gamepilot.testing.runner import (
    ReportWriteError,
    current_environment,
    new_run_id,
    run_case,
    single_case_report,
    utc_now_iso,
    write_report,
)

from .manifest import (
    BASELINE_SCENARIO_BY_ID,
    BENCHMARK_VERSION,
    COMBINATIONS,
    MANIFEST_SOURCE,
    NEGATIVE_VALIDATION_SOURCE,
    SUITE,
    Combination,
)
from .metrics import build_metrics
from .models import (
    BenchmarkReport,
    BenchmarkSummary,
    CombinationEvidence,
    MetricResult,
    MetricStatus,
    NegativeValidation,
)

# 进程内 ASGI 应用的占位地址：`.invalid` 是保留给「不可解析」的顶级域，
# 用它明确表示这些报告不是真实网络请求的产物。
BENCHMARK_BASE_URL = "http://benchmark.invalid"

EXIT_OK = 0
EXIT_DEVIATION = 1
EXIT_EXECUTION_ERROR = 2


class BenchmarkExecutionError(Exception):
    """评测自身无法继续（例如刚写出的报告无法重新读取）。"""


def _single_case_report(case: CaseReport, client: GameClient, run_id: str) -> RunReport:
    """把单个场景的证据包成一份可重跑的运行报告。"""
    return single_case_report(case, client, run_id, SUITE)


def _failing_rules(case: CaseReport) -> list[str]:
    """该场景所有 status=fail 的规则编号，去重后排序。

    同一个缺陷可能同时触发多条规则（例如治疗超限还会让事件 HP 越界），
    去重是为了不让「命中多条」被算成多次发现。
    """
    return sorted({check.rule_id for check in case.checks if check.status == "fail"})


def _matrix_verdict(combination: Combination, case: CaseReport, triggered: bool) -> list[str]:
    """按清单逐条核对，返回所有不符项的说明（空列表表示符合预期）。"""
    notes: list[str] = []
    if case.status == "error":
        notes.append(f"执行错误：{case.error or '未知原因'}")
    elif combination.expect_fail and case.status != "fail":
        notes.append(f"预期失败，实际 {case.status}")
    elif not combination.expect_fail and case.status != "pass":
        notes.append(f"预期通过，实际 {case.status}")

    failing = set(_failing_rules(case))
    missed = sorted(set(combination.target_rules) - failing)
    if missed:
        notes.append(f"漏检目标规则：{', '.join(missed)}")
    if combination.expect_trigger and not triggered:
        notes.append("靶场没有记录到真实触发")
    if not combination.expect_trigger and triggered:
        notes.append("出现未预期的缺陷触发")
    return notes


def _replay_execution(result: ReplayCaseReport | None) -> tuple[CaseStatus, str | None]:
    """保留新执行的状态，不把比较差异或原运行错误误当成新执行失败。"""
    if result is None:
        return "error", "重放报告缺少场景执行结果"
    execution = result.execution
    error = (execution.error or "重放执行失败，未提供原因") if execution.status == "error" else None
    return execution.status, error


def _evidence(
    combination: Combination,
    case: CaseReport,
    run_report: RunReport,
    *,
    report_path: Path,
    root: Path,
    trigger_detail: str | None,
    replay: ReplayReport,
    replay_path: Path,
) -> CombinationEvidence:
    failing = _failing_rules(case)
    triggered = trigger_detail is not None
    notes = _matrix_verdict(combination, case, triggered)
    replayed = replay.cases[0] if replay.cases else None
    replay_status, replay_error = _replay_execution(replayed)
    return CombinationEvidence(
        profile=combination.profile,
        case_id=combination.case_id,
        fault_id=combination.fault_id,
        expect_trigger=combination.expect_trigger,
        expect_fail=combination.expect_fail,
        target_rules=list(combination.target_rules),
        run_id=run_report.run_id,
        report_path=str(report_path.relative_to(root)),
        case_status=case.status,
        case_error=case.error,
        failing_rules=failing,
        target_rules_hit=sorted(set(failing) & set(combination.target_rules)),
        target_rules_missed=sorted(set(combination.target_rules) - set(failing)),
        other_failing_rules=sorted(set(failing) - set(combination.target_rules)),
        triggered=triggered,
        trigger_detail=trigger_detail,
        unexpected_trigger=triggered and not combination.expect_trigger,
        replay_run_id=replay.run_id,
        replay_path=str(replay_path.relative_to(root)),
        replay_outcome=replayed.outcome if replayed else "not_comparable",
        replay_status=replay_status,
        replay_error=replay_error,
        replay_differences=len(replayed.differences) if replayed else 0,
        matrix_ok=not notes,
        notes=notes,
    )


async def _run_combination(
    combination: Combination,
    *,
    client: GameClient,
    recorder: TriggerRecorder,
    root: Path,
) -> CombinationEvidence:
    """执行一个组合：原始运行 → 落盘 → 读回 → 同 profile 重跑 → 落盘。"""
    scenario = BASELINE_SCENARIO_BY_ID[combination.case_id]
    case = await run_case(client, scenario)

    run_report = _single_case_report(case, client, new_run_id())
    report_path = write_report(
        run_report, root / "runs" / combination.profile / combination.case_id
    )

    # 用刚写出的文件（而不是内存对象）做重跑输入：证据链必须能被读回来。
    try:
        reloaded = load_report(report_path)
    except ReplayInputError as exc:  # pragma: no cover - 只有在写入或校验实现出错时才会走到
        raise BenchmarkExecutionError(f"刚写出的报告无法重新读取：{exc}") from exc
    replay = await replay_report(client, reloaded, source_report=str(report_path))
    replay_path = write_report(replay, root / "replays" / combination.profile / combination.case_id)

    trigger = recorder.get(case.session_id) if case.session_id else None
    return _evidence(
        combination,
        case,
        run_report,
        report_path=report_path,
        root=root,
        trigger_detail=trigger.detail if trigger else None,
        replay=replay,
        replay_path=replay_path,
    )


async def _run_negative_validation(
    *, source_report_path: Path, normal_client: GameClient, root: Path
) -> NegativeValidation:
    """把一个缺陷轨迹放到 normal 上重放：动作相同，结论必须不同。"""
    profile, case_id = NEGATIVE_VALIDATION_SOURCE
    report = load_report(source_report_path)
    replay = await replay_report(normal_client, report, source_report=str(source_report_path))
    replay_path = write_report(replay, root / "replays" / "negative" / case_id)

    result = replay.cases[0] if replay.cases else None
    execution_status, execution_error = _replay_execution(result)
    recorded_actions = [
        (action.index, action.action, action.status_code)
        for action in report.cases[0].executed_actions
    ]
    replayed_actions = (
        [(action.index, action.action, action.status_code) for action in result.replayed_actions]
        if result
        else []
    )
    actions_identical = recorded_actions == replayed_actions
    outcome = result.outcome if result else "not_comparable"
    passed = execution_status != "error" and actions_identical and outcome == "mismatch"
    if execution_status == "error":
        note = f"负向验证执行错误：{execution_error}"
    elif passed:
        note = "动作序列与原运行完全相同，但状态与规则结论不同：缺陷没有被复现，比对器能分辨这一点"
    elif not actions_identical:
        note = "重放的动作序列与原运行不同，无法据此判断缺陷是否被复现"
    else:
        note = f"重放结果为 {outcome}，与期望的 mismatch 不一致"
    return NegativeValidation(
        source_profile=profile,
        source_case_id=case_id,
        report_path=str(source_report_path.relative_to(root)),
        replay_path=str(replay_path.relative_to(root)),
        replay_outcome=outcome,
        execution_status=execution_status,
        execution_error=execution_error,
        expected_outcome="mismatch",
        actions_identical=actions_identical,
        differences=len(result.differences) if result else 0,
        passed=passed,
        note=note,
    )


def _status(
    evidences: Sequence[CombinationEvidence],
    metrics: Sequence[MetricResult],
    negative: NegativeValidation,
) -> MetricStatus:
    """总状态：执行错误优先，其次矩阵不符、指标未达标或负向验证失败。"""
    if negative.execution_status == "error" or any(
        item.case_status == "error" or item.replay_status == "error" for item in evidences
    ):
        return "execution_error"
    if any(not item.matrix_ok for item in evidences):
        return "deviation"
    if any(not metric.passed for metric in metrics) or not negative.passed:
        return "deviation"
    return "ok"


def _summary(
    evidences: Sequence[CombinationEvidence],
    metrics: Sequence[MetricResult],
    negative: NegativeValidation,
) -> BenchmarkSummary:
    status = _status(evidences, metrics, negative)
    return BenchmarkSummary(
        combinations_planned=len(COMBINATIONS),
        combinations_executed=len(evidences),
        as_expected=sum(1 for item in evidences if item.matrix_ok),
        deviations=sum(1 for item in evidences if not item.matrix_ok),
        execution_errors=sum(1 for item in evidences if item.case_status == "error"),
        replay_execution_errors=sum(1 for item in evidences if item.replay_status == "error"),
        negative_execution_errors=int(negative.execution_status == "error"),
        metrics_passed=sum(1 for metric in metrics if metric.passed),
        metrics_total=len(metrics),
        negative_validation_passed=negative.passed,
        status=status,
        exit_code=_exit_code(status),
    )


def _exit_code(status: MetricStatus) -> int:
    """执行错误优先于评测偏差：没跑出结论时不能声称「只是漏检」。"""
    if status == "execution_error":
        return EXIT_EXECUTION_ERROR
    if status == "deviation":
        return EXIT_DEVIATION
    return EXIT_OK


def _write_summary(report: BenchmarkReport, root: Path) -> Path:
    """写出评测摘要；已存在时失败，不覆盖已有证据。"""
    path = root / "benchmark.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入评测摘要 {path}：{type(exc).__name__}") from exc
    return path


async def run_benchmark(
    output_dir: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> BenchmarkReport:
    """在内存中组装四个隔离应用，跑完固定矩阵、同 profile 重跑与负向验证。"""
    started_at = utc_now_iso()
    started = time.perf_counter()
    # 摘要里的 run_id 与输出目录同名：报告要能被直接定位回它自己的证据目录。
    run_id = new_run_id()
    root = Path(output_dir) / run_id

    evidences: list[CombinationEvidence] = []
    clients: dict[str, GameClient] = {}
    recorders: dict[str, TriggerRecorder] = {}
    async with AsyncExitStack() as stack:
        for profile in PROFILES:
            recorder = TriggerRecorder()
            app = create_lab_app(profile, recorder=recorder)
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=BENCHMARK_BASE_URL,
                    timeout=timeout,
                )
            )
            # 每个 profile 一个客户端、一个触发记录器：profile 之间没有任何共享状态。
            clients[profile] = GameClient(http, base_url=BENCHMARK_BASE_URL, timeout=timeout)
            recorders[profile] = recorder

        for combination in COMBINATIONS:
            evidences.append(
                await _run_combination(
                    combination,
                    client=clients[combination.profile],
                    recorder=recorders[combination.profile],
                    root=root,
                )
            )

        source = next(
            item for item in evidences if (item.profile, item.case_id) == NEGATIVE_VALIDATION_SOURCE
        )
        negative = await _run_negative_validation(
            source_report_path=root / source.report_path,
            normal_client=clients[PROFILE_NORMAL],
            root=root,
        )

    metrics = build_metrics(evidences)
    summary = _summary(evidences, metrics, negative)

    finished_at = utc_now_iso()
    report = BenchmarkReport(
        benchmark_version=BENCHMARK_VERSION,
        manifest_source=MANIFEST_SOURCE,
        suite=SUITE,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        timeout_seconds=timeout,
        base_url=BENCHMARK_BASE_URL,
        profiles=list(PROFILES),
        profile_map={profile: PROFILE_CATALOG[profile].summary for profile in PROFILES},
        target_rules={
            profile: sorted(
                {
                    rule
                    for row in COMBINATIONS
                    if row.profile == profile
                    for rule in row.target_rules
                }
            )
            for profile in PROFILES
        },
        rules_version=RULES_VERSION,
        schema_version=SCHEMA_VERSION,
        rules_source=RULES_SOURCE,
        environment=current_environment(),
        summary=summary,
        negative_validation=negative,
        combinations=evidences,
        metrics=metrics,
    )
    _write_summary(report, root)
    return report


__all__ = [
    "BENCHMARK_BASE_URL",
    "BenchmarkExecutionError",
    "EXIT_DEVIATION",
    "EXIT_EXECUTION_ERROR",
    "EXIT_OK",
    "run_benchmark",
]
