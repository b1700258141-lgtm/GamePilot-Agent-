"""固定回归清单的预检、逐项证据和退出语义。"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gamepilot.defects.batch import BatchReport, load_manifest, run_batch
from gamepilot.defects.logic import DefectInputError, export_defect
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


async def _manifest(tmp_path: Path) -> Path:
    cases = {case.case_id: case for case in get_suite("baseline")}
    entries = []
    for profile, case_id, rule_id in (
        ("potion_overheal", "attack-then-potion", "R-POTION-CAP"),
        ("potion_not_consumed", "attack-then-potion", "R-POTION-DECREMENTS"),
        ("retaliate_after_death", "win-then-rejected", "R-NO-RETALIATE-ON-KILL"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_lab_app(profile))
        ) as http:
            client = GameClient(http, base_url="http://lab.test")
            case = await run_case(client, cases[case_id])
            assert case.status == "fail"
            run = single_case_report(case, client, new_run_id(), "batch-fixture")
            run_path = write_report(run, tmp_path / "source")
            replay = await replay_report(client, run, source_report=str(run_path))
            assert replay.summary.matched == 1
            replay_path = write_report(replay, tmp_path / "source")
        artifact, path = export_defect(
            run_path, replay_path, case_id, tmp_path / "defects", rule_id=rule_id
        )
        entries.append({"artifact": f"defects/{path.name}", "fingerprint": artifact.fingerprint})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "1.0", "name": "known-defects", "items": entries}),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.anyio
async def test_three_confirmed_defects_batch_normal_and_faulty(tmp_path: Path) -> None:
    manifest = await _manifest(tmp_path)
    for profile, expected_code, expected_statuses in (
        ("normal", 0, ["passed", "passed", "passed"]),
        ("potion_not_consumed", 1, ["failed", "failed", "passed"]),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_lab_app(profile))
        ) as http:
            report, path = await run_batch(
                manifest,
                GameClient(http, base_url="http://user:pass@lab.test"),
                tmp_path / profile,
            )
        assert report.summary.exit_code == expected_code
        assert (report.summary.planned, report.summary.executed, report.summary.skipped) == (
            3,
            3,
            0,
        )
        assert [row.status for row in report.items] == expected_statuses
        assert all(Path(row.run_report).is_file() for row in report.items)
        assert BatchReport.model_validate_json(path.read_text(encoding="utf-8")) == report
        assert "user:pass" not in path.read_text(encoding="utf-8")
        if profile != "normal":
            assert report.items[0].outcome == "other_rule_failure"
            assert report.items[1].outcome == "target_reproduced"


@pytest.mark.anyio
async def test_bad_manifest_is_rejected_before_http(tmp_path: Path) -> None:
    manifest = await _manifest(tmp_path)
    original = json.loads(manifest.read_text(encoding="utf-8"))
    requests = 0

    def request(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503)

    cases = []
    missing = json.loads(json.dumps(original))
    del missing["items"][1]["fingerprint"]
    cases.append(missing)
    wrong = json.loads(json.dumps(original))
    wrong["items"][1]["fingerprint"] = "0" * 64
    cases.append(wrong)
    duplicate = json.loads(json.dumps(original))
    duplicate["items"][1] = duplicate["items"][0]
    cases.append(duplicate)
    escaped = json.loads(json.dumps(original))
    escaped["items"][1]["artifact"] = "../outside.json"
    cases.append(escaped)
    absolute = json.loads(json.dumps(original))
    absolute["items"][1]["artifact"] = str((tmp_path / "outside.json").resolve())
    cases.append(absolute)
    cases.append({**original, "items": []})
    cases.append({**original, "items": original["items"] * 34})
    for index, bad in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as http:
            with pytest.raises(DefectInputError):
                await run_batch(
                    path, GameClient(http, base_url="http://lab.test"), tmp_path / "out"
                )
    assert requests == 0
    assert not (tmp_path / "out").exists()


@pytest.mark.anyio
async def test_symlink_escape_rejected_before_http(tmp_path: Path) -> None:
    manifest = await _manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_bytes((tmp_path / raw["items"][0]["artifact"]).read_bytes())
    link = tmp_path / "defects" / "outside-link.json"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("当前系统不允许创建符号链接")
    raw["items"][0]["artifact"] = "defects/outside-link.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DefectInputError, match="越出"):
        load_manifest(manifest)


@pytest.mark.anyio
async def test_server_error_stops_after_one_and_keeps_denominator(tmp_path: Path) -> None:
    manifest = await _manifest(tmp_path)
    broken = FastAPI()

    @broken.post("/api/v1/game-sessions")
    async def unavailable() -> JSONResponse:
        return JSONResponse({"error": "unavailable"}, status_code=503)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=broken)) as http:
        report, path = await run_batch(
            manifest, GameClient(http, base_url="http://lab.test"), tmp_path / "out"
        )
    assert report.summary.model_dump() == {
        "planned": 3,
        "executed": 1,
        "skipped": 2,
        "passed": 0,
        "failed": 0,
        "errored": 1,
        "exit_code": 2,
    }
    assert [row.status for row in report.items] == ["error", "skipped", "skipped"]
    assert Path(report.items[0].run_report).is_file()
    assert path.is_file()


@pytest.mark.anyio
async def test_report_write_failure_stops_and_aggregate_is_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = await _manifest(tmp_path)
    output = tmp_path / "out"
    (output / "runs").parent.mkdir(parents=True)
    (output / "runs").write_text("blocked", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app("normal"))
    ) as http:
        client = GameClient(http, base_url="http://lab.test")
        report, path = await run_batch(manifest, client, output)
        assert report.summary.exit_code == 2
        assert (report.summary.executed, report.summary.skipped) == (1, 2)
        assert report.items[0].outcome == "report_write_error"
        assert path.is_file()

        monkeypatch.setattr("gamepilot.defects.batch.new_run_id", lambda: "20261009T000000Z-abcdef")
        _first, fixed = await run_batch(manifest, client, output)
        saved = fixed.read_bytes()
        with pytest.raises(ReportWriteError):
            await run_batch(manifest, client, output)
        assert fixed.read_bytes() == saved


@pytest.mark.anyio
async def test_unexpected_item_error_is_sanitized_and_skips_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = await _manifest(tmp_path)

    async def broken(*_args: object) -> None:
        raise RuntimeError("secret diagnostic")

    monkeypatch.setattr("gamepilot.defects.batch.verify_defect", broken)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app("normal"))
    ) as http:
        report, path = await run_batch(
            manifest, GameClient(http, base_url="http://lab.test"), tmp_path / "out"
        )
    assert report.summary.exit_code == 2
    assert (report.summary.executed, report.summary.skipped) == (1, 2)
    assert report.items[0].error_kind == "RuntimeError"
    assert "secret diagnostic" not in path.read_text(encoding="utf-8")
