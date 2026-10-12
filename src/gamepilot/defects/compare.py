"""离线比较两次回归；核对清单、逐项证据与分母，不自动关闭缺陷。"""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from gamepilot.testing.replay import ReplayInputError, load_report, require_complete_fields
from gamepilot.testing.reporting import ReportWriteError, new_run_id

from .batch import BatchReport, BatchRow, load_manifest
from .logic import DefectInputError, classify_verification
from .models import DefectArtifact


def load_batch(path: str | Path) -> tuple[BatchReport, dict[str, DefectArtifact], str]:
    """严格读取批量报告；成功/失败必须有对应的单项运行证据。"""
    try:
        content = Path(path).read_bytes()
        raw = json.loads(content)
        report = BatchReport.model_validate(raw)
        require_complete_fields(raw, report.model_dump(mode="json"))
    except (OSError, ValueError, ValidationError, ReplayInputError) as exc:
        raise DefectInputError(f"批量报告无法读取或结构不合法：{type(exc).__name__}") from exc
    if not Path(report.manifest_path).is_absolute() or "://" in report.manifest_path:
        raise DefectInputError("批量报告清单引用必须是本地绝对路径")
    manifest, artifacts, digest = load_manifest(report.manifest_path)
    if digest != report.manifest_sha256 or manifest.name != report.manifest_name:
        raise DefectInputError("批量报告清单指纹或名称不一致")
    if len(report.items) != len(manifest.items):
        raise DefectInputError("批量报告与清单项数不一致")
    stopped = False
    for row, item, artifact in zip(report.items, manifest.items, artifacts, strict=True):
        if (row.artifact, row.fingerprint, row.rule_id) != (
            item.artifact,
            item.fingerprint,
            artifact.rule_id,
        ):
            raise DefectInputError("批量报告条目与清单不一致")
        if stopped:
            if row.status != "skipped" or any(
                value is not None for value in (row.outcome, row.run_report, row.error_kind)
            ):
                raise DefectInputError("错误后的剩余项必须是无执行证据的 skipped")
            continue
        if row.status == "skipped":
            raise DefectInputError("无前置执行错误却出现 skipped")
        _validate_row(row, artifact, report.base_url)
        stopped = row.status == "error"
    counts = {
        state: sum(row.status == state for row in report.items)
        for state in ("passed", "failed", "error", "skipped")
    }
    expected = {
        "planned": len(report.items),
        "executed": len(report.items) - counts["skipped"],
        "skipped": counts["skipped"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errored": counts["error"],
        "exit_code": 2 if counts["error"] else 1 if counts["failed"] else 0,
    }
    if report.summary.model_dump() != expected:
        raise DefectInputError("批量汇总与逐项结果不一致")
    return report, {a.fingerprint: a for a in artifacts}, hashlib.sha256(content).hexdigest()


def _validate_row(row: BatchRow, artifact: DefectArtifact, base_url: str) -> None:
    if row.run_report is None:
        allowed = row.status == "error" and (
            (row.outcome == "report_write_error" and row.error_kind == "ReportWriteError")
            or (row.outcome == "unexpected_error" and bool(row.error_kind))
        )
        if not allowed:
            raise DefectInputError("批量条目缺少运行证据")
        return
    if not Path(row.run_report).is_absolute() or "://" in row.run_report:
        raise DefectInputError("运行证据引用必须是本地绝对路径")
    run = load_report(row.run_report)
    if (
        run.suite != "defect-regression"
        or run.base_url != base_url
        or len(run.cases) != 1
        or run.cases[0].scenario != artifact.regression
        or run.rules_version != artifact.rules_version
    ):
        raise DefectInputError("运行证据与回归规格不一致")
    code, outcome = classify_verification(artifact, run.cases[0])
    status = "passed" if code == 0 else "failed" if code == 1 else "error"
    if (row.status, row.outcome, row.error_kind) != (
        status,
        outcome,
        "execution_error" if code == 2 else None,
    ):
        raise DefectInputError("批量结论与运行证据不一致")


def compare_batches(
    before_path: str | Path, after_path: str | Path, output_dir: str | Path
) -> tuple[dict, Path]:
    """相同回归集合按指纹比较；错误、跳过和目标未检查均为不可判定。"""
    before, before_artifacts, before_digest = load_batch(before_path)
    after, after_artifacts, after_digest = load_batch(after_path)
    if before_artifacts.keys() != after_artifacts.keys():
        raise DefectInputError("两次回归的缺陷指纹集合不同，不能比较")
    previous = {row.fingerprint: row for row in before.items}
    rows = []
    for current in after.items:
        fingerprint = current.fingerprint
        if before_artifacts[fingerprint].regression != after_artifacts[fingerprint].regression:
            raise DefectInputError("相同指纹的回归规格不同，不能比较")
        old = previous[fingerprint]
        unknown = any(
            row.status in {"error", "skipped"} or row.outcome == "target_not_exercised"
            for row in (old, current)
        )
        transition = (
            "inconclusive"
            if unknown
            else {
                ("passed", "passed"): "still_passing",
                ("failed", "passed"): "now_passing",
                ("passed", "failed"): "new_failure",
                ("failed", "failed"): "still_failing",
            }[(old.status, current.status)]
        )
        rows.append(
            {
                "fingerprint": fingerprint,
                "rule_id": current.rule_id,
                "before_outcome": old.outcome,
                "after_outcome": current.outcome,
                "transition": transition,
            }
        )
    counts = {
        state: sum(row["transition"] == state for row in rows)
        for state in (
            "still_passing",
            "now_passing",
            "new_failure",
            "still_failing",
            "inconclusive",
        )
    }
    code = (
        2
        if counts["inconclusive"]
        else 1
        if (counts["new_failure"] or counts["still_failing"])
        else 0
    )
    report = {
        "schema_version": "1.0",
        "run_id": new_run_id(),
        "before": {"path": str(Path(before_path).resolve()), "sha256": before_digest},
        "after": {"path": str(Path(after_path).resolve()), "sha256": after_digest},
        "summary": {"planned": len(rows), **counts, "exit_code": code},
        "items": rows,
    }
    path = Path(output_dir) / f"comparison-{report['run_id']}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise ReportWriteError(f"无法写入对比报告：{type(exc).__name__}") from exc
    return report, path
