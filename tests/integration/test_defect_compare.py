"""回归对比必须匹配实际证据，不能靠编辑汇总制造修复结果。"""

import json
from pathlib import Path

import httpx
import pytest

from gamepilot.defects.batch import run_batch
from gamepilot.defects.compare import compare_batches, load_batch
from gamepilot.defects.demo import run_demo
from gamepilot.defects.logic import DefectInputError
from gamepilot.lab.app import create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.replay import ReplayInputError
from gamepilot.testing.reporting import ReportWriteError


async def _reports(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    code, path = await run_demo(tmp_path)
    assert code == 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    return path.parent, {
        row["profile"]: path.parent / row["batch_report"] for row in raw["targets"]
    }


@pytest.mark.anyio
async def test_comparison_tracks_passing_failing_and_new_failures(tmp_path: Path) -> None:
    _root, paths = await _reports(tmp_path)
    normal, faulty = paths["normal"], paths["potion_not_consumed"]
    for before, after, transition, count, code in (
        (normal, normal, "still_passing", 4, 0),
        (faulty, normal, "now_passing", 2, 0),
        (normal, faulty, "new_failure", 2, 1),
        (faulty, faulty, "still_failing", 2, 1),
    ):
        report, path = compare_batches(before, after, tmp_path / "comparisons")
        assert report["summary"][transition] == count
        assert report["summary"]["exit_code"] == code
        assert report["summary"]["planned"] == 4
        assert json.loads(path.read_text(encoding="utf-8")) == report


@pytest.mark.anyio
async def test_execution_errors_and_skips_never_become_fixed(tmp_path: Path) -> None:
    root, paths = await _reports(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as http:
        _batch, error_path = await run_batch(
            root / "manifest.json", GameClient(http, base_url="http://broken"), root / "errors"
        )
    for before, after in ((paths["normal"], error_path), (error_path, paths["normal"])):
        report, _path = compare_batches(before, after, tmp_path / "comparisons")
        assert report["summary"]["inconclusive"] == 4
        assert report["summary"]["now_passing"] == 0
        assert report["summary"]["exit_code"] == 2


@pytest.mark.anyio
async def test_forged_summary_missing_evidence_and_wrong_references_rejected(
    tmp_path: Path,
) -> None:
    _root, paths = await _reports(tmp_path)
    original = json.loads(paths["normal"].read_text(encoding="utf-8"))
    for corruption in ("summary", "outcome", "missing", "duplicate", "reference", "digest"):
        raw = json.loads(json.dumps(original))
        if corruption == "summary":
            raw["summary"]["passed"] = 0
        elif corruption == "outcome":
            raw["items"][0]["outcome"] = "target_reproduced"
        elif corruption == "missing":
            raw["items"][0]["run_report"] = None
        elif corruption == "duplicate":
            raw["items"][1] = raw["items"][0]
        elif corruption == "reference":
            raw["items"][0]["run_report"] = raw["items"][1]["run_report"]
        else:
            raw["manifest_sha256"] = "0" * 64
        bad = tmp_path / f"{corruption}.json"
        bad.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises((DefectInputError, ReplayInputError)):
            compare_batches(paths["normal"], bad, tmp_path / "invalid-output")
    assert not (tmp_path / "invalid-output").exists()
    raw = json.loads(json.dumps(original))
    del raw["items"][0]["error_kind"]
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DefectInputError):
        load_batch(bad)


@pytest.mark.anyio
async def test_reordered_set_compares_but_changed_set_does_not(tmp_path: Path) -> None:
    root, paths = await _reports(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["items"].reverse()
    for size in (4, 3):
        manifest["items"] = manifest["items"][:size]
        new_manifest = root / f"manifest-{size}.json"
        new_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_lab_app("normal"))
        ) as http:
            _report, after = await run_batch(
                new_manifest, GameClient(http, base_url="http://normal"), root / f"after-{size}"
            )
        if size == 4:
            report, _path = compare_batches(paths["normal"], after, root / "comparisons")
            assert report["summary"]["still_passing"] == 4
        else:
            with pytest.raises(DefectInputError, match="指纹集合不同"):
                compare_batches(paths["normal"], after, root / "comparisons")


@pytest.mark.anyio
async def test_comparison_output_is_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, paths = await _reports(tmp_path)
    monkeypatch.setattr("gamepilot.defects.compare.new_run_id", lambda: "20261009T150000Z-abcdef")
    _report, path = compare_batches(paths["normal"], paths["normal"], tmp_path / "out")
    saved = path.read_bytes()
    with pytest.raises(ReportWriteError):
        compare_batches(paths["normal"], paths["normal"], tmp_path / "out")
    assert path.read_bytes() == saved
