"""严格读取两次独立 seed sweep 证据，并比较游戏轨迹与评测结论。"""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from gamepilot.lab import PROFILES
from gamepilot.testing.models import CaseReport
from gamepilot.testing.replay import (
    ReplayInputError,
    compare_case,
    load_replay_report,
    load_report,
    require_complete_fields,
)
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso
from gamepilot.testing.rules import RULES_VERSION

from .seed_sweep import (
    BASE_URL,
    SEED_SWEEP_VERSION,
    SEEDS,
    SUITE,
    TARGET_RULES,
    TRACE_ACTIONS,
    SweepReport,
    _scenarios,
    _verdict,
)

SEED_COMPARE_VERSION = "1.0.0"


class SweepInputError(ValueError):
    """跨运行证据缺失、被改动或不符合固定清单。"""


class CompareModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DriftCell(CompareModel):
    profile: str
    seed: int
    trace: str
    fields: list[str]


class SweepComparison(CompareModel):
    version: str
    run_id: str
    created_at: str
    before_summary: str
    after_summary: str
    before_sha256: str
    after_sha256: str
    before_run_id: str
    after_run_id: str
    planned: int
    compared: int
    drift_count: int
    source_exit_codes: list[int]
    status: str
    exit_code: int
    drift_cells: list[DriftCell]


def _reference(root: Path, value: str) -> Path:
    """报告引用只能指向该摘要目录内的常规文件。"""
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not parts
        or any(part in ("", ".", "..") for part in parts)
        or ":" in parts[0]
        or normalized.startswith("/")
    ):
        raise SweepInputError("跨种子报告引用不是安全的相对路径")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise SweepInputError("跨种子报告引用越界或文件不存在")
    return path


def _counts(report: SweepReport) -> dict[str, object]:
    cells = report.cells
    triggers = {
        profile: sum(cell.profile == profile and cell.triggered for cell in cells)
        for profile in PROFILES
    }
    detections = {
        profile: sum(
            cell.profile == profile and cell.triggered and cell.target_hit for cell in cells
        )
        for profile in PROFILES
    }
    run_errors = sum(cell.case_status == "error" for cell in cells)
    replay_errors = sum(
        cell.replay_status == "error" or cell.replay_outcome == "not_comparable" for cell in cells
    )
    deviations = sum(cell.verdict == "deviation" for cell in cells)
    missing = any(triggers[profile] == 0 for profile in TARGET_RULES)
    return {
        "planned": len(PROFILES) * len(_scenarios()),
        "executed": len(cells),
        "run_errors": run_errors,
        "replay_errors": replay_errors,
        "deviations": deviations,
        "replay_matches": sum(cell.replay_outcome == "match" for cell in cells),
        "trigger_counts": triggers,
        "detection_counts": detections,
        "exit_code": 2 if run_errors or replay_errors else 1 if deviations or missing else 0,
    }


def load_sweep(path: str | Path) -> tuple[SweepReport, dict[tuple[str, str], CaseReport], str]:
    """摘要、文件指纹、原运行、重放和每格结论必须互相吻合。"""
    summary_path = Path(path)
    try:
        content = summary_path.read_bytes()
        raw = json.loads(content)
        report = SweepReport.model_validate(raw)
        require_complete_fields(raw, report.model_dump(mode="json"))
    except (OSError, ValueError, ValidationError, ReplayInputError) as exc:
        raise SweepInputError(f"跨种子摘要无法读取或结构不合法：{type(exc).__name__}") from exc
    if (
        report.version != SEED_SWEEP_VERSION
        or report.rules_version != RULES_VERSION
        or report.seeds != list(SEEDS)
        or report.traces != list(TRACE_ACTIONS)
        or report.profiles != list(PROFILES)
        or report.run_id != summary_path.parent.name
    ):
        raise SweepInputError("跨种子摘要版本或固定清单不一致，请用当前版本重新生成")
    expected = _counts(report)
    if any(getattr(report, key) != value for key, value in expected.items()):
        raise SweepInputError("跨种子摘要计数与逐格结论不一致")
    scenarios = _scenarios()
    expected_keys = [(profile, scenario.case_id) for profile in PROFILES for scenario in scenarios]
    if [(cell.profile, cell.case_id) for cell in report.cells] != expected_keys:
        raise SweepInputError("跨种子摘要逐格顺序或标识不符合固定清单")
    if set(report.run_reports) != set(PROFILES) or set(report.replay_reports) != set(PROFILES):
        raise SweepInputError("跨种子摘要缺少 profile 报告引用")
    references = set(report.run_reports.values()) | set(report.replay_reports.values())
    if len(references) != 2 * len(PROFILES) or set(report.report_sha256) != references:
        raise SweepInputError("跨种子报告引用或指纹集合不完整")

    root = summary_path.parent
    cases: dict[tuple[str, str], CaseReport] = {}
    for profile in PROFILES:
        run_path = _reference(root, report.run_reports[profile])
        replay_path = _reference(root, report.replay_reports[profile])
        for reference, file in (
            (report.run_reports[profile], run_path),
            (report.replay_reports[profile], replay_path),
        ):
            try:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
            except OSError as exc:
                raise SweepInputError("跨种子报告引用无法读取") from exc
            if digest != report.report_sha256[reference]:
                raise SweepInputError("跨种子报告内容与摘要指纹不一致")
        try:
            run = load_report(run_path)
            replay = load_replay_report(replay_path)
        except ReplayInputError as exc:
            raise SweepInputError(f"跨种子运行或重放报告不合法：{exc}") from exc
        if (
            run.suite != SUITE
            or run.base_url != BASE_URL
            or replay.base_url != BASE_URL
            or replay.source_run_id != run.run_id
            or replay.source_report.replace("\\", "/").rsplit("/", 1)[-1] != run_path.name
            or len(run.cases) != len(scenarios)
            or len(replay.cases) != len(scenarios)
        ):
            raise SweepInputError("跨种子运行与重放报告身份或数量不一致")
        offset = PROFILES.index(profile) * len(scenarios)
        for index, (scenario, case, replayed) in enumerate(
            zip(scenarios, run.cases, replay.cases, strict=True)
        ):
            cell = report.cells[offset + index]
            if case.scenario != scenario or replayed.case_id != case.case_id:
                raise SweepInputError("跨种子报告的场景或重放标识不符合清单")
            differences = (
                []
                if case.status == "error" or replayed.execution.status == "error"
                else compare_case(case, replayed.execution)
            )
            actual_outcome = (
                "not_comparable"
                if case.status == "error" or replayed.execution.status == "error"
                else "mismatch"
                if differences
                else "match"
            )
            if replayed.outcome != actual_outcome or replayed.differences != differences:
                raise SweepInputError("跨种子重放结论与实际轨迹不一致")
            recomputed = _verdict(profile, case, replayed, cell.trigger_fault_id)
            if recomputed != cell:
                raise SweepInputError("跨种子逐格结论与运行证据不一致")
            cases[(profile, case.case_id)] = case
    return report, cases, hashlib.sha256(content).hexdigest()


def _drift(
    before: SweepReport,
    after: SweepReport,
    before_cases: dict[tuple[str, str], CaseReport],
    after_cases: dict[tuple[str, str], CaseReport],
) -> list[DriftCell]:
    differences: list[DriftCell] = []
    for left, right in zip(before.cells, after.cells, strict=True):
        key = (left.profile, left.case_id)
        fields = [
            f"cell.{name}"
            for name in type(left).model_fields
            if getattr(left, name) != getattr(right, name)
        ]
        fields.extend(
            f"run.{item.field}" for item in compare_case(before_cases[key], after_cases[key])
        )
        if fields:
            differences.append(
                DriftCell(
                    profile=left.profile, seed=left.seed, trace=left.trace, fields=fields[:12]
                )
            )
    return differences


def _comparison_exit_code(source_codes: list[int], drift_count: int) -> int:
    """来源执行错误主导 2；已确认偏差或跨运行漂移为 1。"""
    return 2 if 2 in source_codes else 1 if drift_count or 1 in source_codes else 0


def compare_sweeps(
    before_path: str | Path, after_path: str | Path, output_dir: str | Path
) -> tuple[SweepComparison, Path]:
    """不同运行的 256 格逐字段对照；输入错误优先于偏差。"""
    before, before_cases, before_digest = load_sweep(before_path)
    after, after_cases, after_digest = load_sweep(after_path)
    if before.run_id == after.run_id or before_digest == after_digest:
        raise SweepInputError("必须提供两次独立生成的跨种子运行")
    differences = _drift(before, after, before_cases, after_cases)
    source_codes = [before.exit_code, after.exit_code]
    exit_code = _comparison_exit_code(source_codes, len(differences))
    result = SweepComparison(
        version=SEED_COMPARE_VERSION,
        run_id=new_run_id(),
        created_at=utc_now_iso(),
        before_summary=str(Path(before_path).resolve()),
        after_summary=str(Path(after_path).resolve()),
        before_sha256=before_digest,
        after_sha256=after_digest,
        before_run_id=before.run_id,
        after_run_id=after.run_id,
        planned=len(PROFILES) * len(_scenarios()),
        compared=len(before.cells),
        drift_count=len(differences),
        source_exit_codes=source_codes,
        status="execution_error" if exit_code == 2 else "drift" if exit_code == 1 else "match",
        exit_code=exit_code,
        drift_cells=differences,
    )
    output = Path(output_dir) / result.run_id / "seed-comparison.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=False)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入跨运行对比：{type(exc).__name__}") from exc
    return result, output
