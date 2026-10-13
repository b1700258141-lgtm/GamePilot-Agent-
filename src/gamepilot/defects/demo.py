"""可重复生成的离线闭环演示；使用固定脚本和内存靶场，不调用 Agent。"""

import json
from pathlib import Path

import httpx

from gamepilot.lab.app import create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.replay import replay_report
from gamepilot.testing.reporting import (
    ReportWriteError,
    new_run_id,
    single_case_report,
    write_report,
)
from gamepilot.testing.runner import run_case
from gamepilot.testing.scenarios import get_suite

from .batch import ManifestItem, RegressionManifest, run_batch
from .logic import export_defect

DEMO_CASES = (
    ("potion_overheal", "attack-then-potion", "R-POTION-CAP"),
    ("potion_not_consumed", "attack-then-potion", "R-POTION-DECREMENTS"),
    ("retaliate_after_death", "win-then-rejected", "R-NO-RETALIATE-ON-KILL"),
    ("loot_anomaly", "win-then-rejected", "R-LOOT-PRESENT"),
)


def _write(path: Path, content: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(content)
    except OSError as exc:
        raise ReportWriteError(f"演示产物写入失败：{type(exc).__name__}") from exc


async def run_demo(output_dir: str | Path) -> tuple[int, Path]:
    """每次创建独立目录；只有证据链和五个靶场预期均满足时才返回 0。"""
    root = Path(output_dir).resolve() / new_run_id()
    try:
        root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise ReportWriteError(f"演示目录创建失败：{type(exc).__name__}") from exc
    summary = {
        "schema_version": "1.0",
        "mode": "offline-scripted-asgi",
        "model_calls": 0,
        "planned_exports": len(DEMO_CASES),
        "planned_targets": len(DEMO_CASES) + 1,
        "exports": [],
        "targets": [],
        "manifest": None,
        "exit_code": 2,
        "error_kind": None,
    }
    code = 0
    try:
        cases = {case.case_id: case for case in get_suite("baseline")}
        items = []
        for profile, case_id, rule_id in DEMO_CASES:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_lab_app(profile)), trust_env=False
            ) as http:
                client = GameClient(http, base_url="http://offline-demo")
                case = await run_case(client, cases[case_id])
                run = single_case_report(case, client, new_run_id(), "offline-defect-demo")
                run_path = write_report(run, root / "sources" / profile)
                replay = await replay_report(client, run, source_report=str(run_path))
                replay_path = write_report(replay, root / "sources" / profile)
            artifact, artifact_path = export_defect(
                run_path, replay_path, case_id, root / "defects", rule_id=rule_id
            )
            relative = artifact_path.relative_to(root).as_posix()
            items.append(ManifestItem(artifact=relative, fingerprint=artifact.fingerprint))
            summary["exports"].append(
                {"profile": profile, "rule_id": rule_id, "artifact": relative}
            )
        manifest = RegressionManifest(name="offline-known-defects", items=items)
        manifest_path = root / "manifest.json"
        _write(manifest_path, manifest.model_dump_json(indent=2))
        summary["manifest"] = "manifest.json"

        for profile, target_rule in [("normal", None)] + [
            (profile, rule_id) for profile, _case_id, rule_id in DEMO_CASES
        ]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_lab_app(profile)), trust_env=False
            ) as http:
                report, report_path = await run_batch(
                    manifest_path,
                    GameClient(http, base_url="http://offline-demo"),
                    root / "verification" / profile,
                )
            complete = report.summary.executed == len(DEMO_CASES) and report.summary.skipped == 0
            met = complete and (
                report.summary.exit_code == 0
                if target_rule is None
                else report.summary.exit_code == 1
                and any(
                    row.rule_id == target_rule and row.outcome == "target_reproduced"
                    for row in report.items
                )
            )
            summary["targets"].append(
                {
                    "profile": profile,
                    "batch_report": report_path.relative_to(root).as_posix(),
                    "batch_exit_code": report.summary.exit_code,
                    "expectation_met": met,
                }
            )
            if report.summary.exit_code == 2:
                code = 2
                break
            if not met:
                code = 1
    except Exception as exc:  # noqa: BLE001 - 保留部分产物且不泄漏异常文本
        code = 2
        summary["error_kind"] = type(exc).__name__
    summary["exit_code"] = code
    path = root / "demo-summary.json"
    _write(path, json.dumps(summary, ensure_ascii=False, indent=2))
    return code, path
