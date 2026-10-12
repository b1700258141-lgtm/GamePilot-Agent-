"""固定清单的无模型批量回归：先完整预检，再顺序执行。"""

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gamepilot.testing.client import GameClient
from gamepilot.testing.replay import ReplayInputError, require_complete_fields
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso

from .logic import DefectInputError, load_artifact, verify_defect
from .models import DefectArtifact

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class BatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ManifestItem(BatchModel):
    artifact: str
    fingerprint: str


class RegressionManifest(BatchModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1, max_length=100)
    items: list[ManifestItem] = Field(min_length=1, max_length=100)


class BatchRow(BatchModel):
    artifact: str
    fingerprint: str
    rule_id: str
    status: Literal["passed", "failed", "error", "skipped"]
    outcome: str | None = None
    run_report: str | None = None
    error_kind: str | None = None


class BatchSummary(BatchModel):
    planned: int
    executed: int
    skipped: int
    passed: int
    failed: int
    errored: int
    exit_code: Literal[0, 1, 2]


class BatchReport(BatchModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    manifest_name: str
    manifest_path: str
    manifest_sha256: str
    base_url: str
    started_at: str
    finished_at: str
    summary: BatchSummary
    items: list[BatchRow]


def load_manifest(path: str | Path) -> tuple[RegressionManifest, list[DefectArtifact], str]:
    """在发出任何 HTTP 请求前校验整个清单及其所有缺陷产物。"""
    manifest_path = Path(path)
    try:
        content = manifest_path.read_bytes()
        raw = json.loads(content)
    except (OSError, ValueError) as exc:
        raise DefectInputError(f"回归清单无法读取：{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise DefectInputError("回归清单版本不受支持")
    try:
        manifest = RegressionManifest.model_validate(raw)
        require_complete_fields(raw, manifest.model_dump(mode="json"))
    except (ValidationError, ReplayInputError) as exc:
        raise DefectInputError("回归清单缺少字段或结构不合法") from exc

    root = manifest_path.parent.resolve()
    fingerprints: set[str] = set()
    paths: set[Path] = set()
    artifacts: list[DefectArtifact] = []
    for item in manifest.items:
        relative = Path(item.artifact)
        windows = PureWindowsPath(item.artifact)
        if (
            not _FINGERPRINT.fullmatch(item.fingerprint)
            or not item.artifact
            or relative.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in relative.parts
            or ".." in windows.parts
            or "://" in item.artifact
            or "\\" in item.artifact
            or "\x00" in item.artifact
        ):
            raise DefectInputError("回归清单包含非法指纹或产物路径")
        try:
            resolved = (root / relative).resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise DefectInputError("回归产物路径越出清单目录或不是文件")
        except (OSError, RuntimeError, ValueError) as exc:
            raise DefectInputError(f"回归产物路径无法读取：{type(exc).__name__}") from exc
        if resolved in paths or item.fingerprint in fingerprints:
            raise DefectInputError("回归清单包含重复路径或指纹")
        artifact = load_artifact(resolved)
        if artifact.fingerprint != item.fingerprint:
            raise DefectInputError("回归清单指纹与缺陷产物不一致")
        paths.add(resolved)
        fingerprints.add(item.fingerprint)
        artifacts.append(artifact)
    return manifest, artifacts, hashlib.sha256(content).hexdigest()


async def run_batch(
    manifest_path: str | Path,
    client: GameClient,
    output_dir: str | Path,
) -> tuple[BatchReport, Path]:
    """规则失败继续，基础设施或证据写入错误则停止并记录剩余跳过项。"""
    manifest, artifacts, digest = load_manifest(manifest_path)
    started = utc_now_iso()
    rows: list[BatchRow] = []
    for item, artifact in zip(manifest.items, artifacts, strict=True):
        try:
            code, report_path, _case, outcome = await verify_defect(
                artifact, client, Path(output_dir) / "runs"
            )
            rows.append(
                BatchRow(
                    artifact=item.artifact,
                    fingerprint=item.fingerprint,
                    rule_id=artifact.rule_id,
                    status="passed" if code == 0 else "failed" if code == 1 else "error",
                    outcome=outcome,
                    run_report=str(report_path.resolve()),
                    error_kind="execution_error" if code == 2 else None,
                )
            )
        except ReportWriteError:
            rows.append(
                BatchRow(
                    artifact=item.artifact,
                    fingerprint=item.fingerprint,
                    rule_id=artifact.rule_id,
                    status="error",
                    outcome="report_write_error",
                    error_kind="ReportWriteError",
                )
            )
            code = 2
        except Exception as exc:  # noqa: BLE001 - 批次不能因单项异常丢失固定分母
            rows.append(
                BatchRow(
                    artifact=item.artifact,
                    fingerprint=item.fingerprint,
                    rule_id=artifact.rule_id,
                    status="error",
                    outcome="unexpected_error",
                    error_kind=type(exc).__name__,
                )
            )
            code = 2
        if code == 2:
            break
    for item, artifact in zip(manifest.items[len(rows) :], artifacts[len(rows) :], strict=True):
        rows.append(
            BatchRow(
                artifact=item.artifact,
                fingerprint=item.fingerprint,
                rule_id=artifact.rule_id,
                status="skipped",
            )
        )
    counts = {
        status: sum(row.status == status for row in rows)
        for status in ("passed", "failed", "error", "skipped")
    }
    exit_code = 2 if counts["error"] else 1 if counts["failed"] else 0
    report = BatchReport(
        run_id=new_run_id(),
        manifest_name=manifest.name,
        manifest_path=str(Path(manifest_path).resolve()),
        manifest_sha256=digest,
        base_url=client.base_url,
        started_at=started,
        finished_at=utc_now_iso(),
        summary=BatchSummary(
            planned=len(rows),
            executed=len(rows) - counts["skipped"],
            skipped=counts["skipped"],
            passed=counts["passed"],
            failed=counts["failed"],
            errored=counts["error"],
            exit_code=exit_code,
        ),
        items=rows,
    )
    path = Path(output_dir) / f"batch-{report.run_id}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入批量报告：{type(exc).__name__}") from exc
    return report, path
