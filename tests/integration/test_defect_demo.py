"""演示必须从新证据得到回归结果，不能把缺失或错误结果当作成功。"""

import json
from pathlib import Path

import pytest

from gamepilot.defects import demo
from gamepilot.defects.batch import load_manifest
from gamepilot.testing.reporting import ReportWriteError


@pytest.mark.anyio
async def test_demo_generates_complete_chain_and_repeat_preserves_evidence(tmp_path: Path) -> None:
    code, path = await demo.run_demo(tmp_path)
    assert code == 0
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["model_calls"] == 0
    assert len(raw["exports"]) == 4
    assert len(raw["targets"]) == 5
    assert all(target["expectation_met"] for target in raw["targets"])
    assert [target["batch_exit_code"] for target in raw["targets"]] == [0, 1, 1, 1, 1]
    manifest, artifacts, _digest = load_manifest(path.parent / raw["manifest"])
    assert len(manifest.items) == len(artifacts) == 4
    for target in raw["targets"]:
        batch = json.loads((path.parent / target["batch_report"]).read_text(encoding="utf-8"))
        assert batch["summary"]["executed"] == 4
        assert all(Path(row["run_report"]).is_file() for row in batch["items"])
    saved = {p: p.read_bytes() for p in path.parent.rglob("*.json")}
    second_code, second = await demo.run_demo(tmp_path)
    assert second_code == 0 and second.parent != path.parent
    assert all(p.read_bytes() == data for p, data in saved.items())


@pytest.mark.anyio
async def test_demo_stops_on_evidence_error_and_redacts_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_profile: str) -> None:
        raise RuntimeError("secret provider key")

    monkeypatch.setattr(demo, "create_lab_app", broken)
    code, path = await demo.run_demo(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert code == 2 and raw["error_kind"] == "RuntimeError"
    assert raw["exports"] == raw["targets"] == []
    assert raw["planned_exports"] == 4 and raw["planned_targets"] == 5
    assert "secret provider key" not in path.read_text(encoding="utf-8")


@pytest.mark.anyio
@pytest.mark.parametrize("execution_error", [False, True])
async def test_demo_rejects_missing_fault_and_directory_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution_error: bool
) -> None:
    original = demo.run_batch

    async def hide_fault(*args: object):
        report, path = await original(*args)
        for row in report.items:
            if row.outcome == "target_reproduced":
                row.outcome = "other_rule_failure"
        if execution_error:
            report.summary.exit_code = 2
        return report, path

    monkeypatch.setattr(demo, "run_batch", hide_fault)
    monkeypatch.setattr(demo, "new_run_id", lambda: "20261009T120000Z-abcdef")
    code, path = await demo.run_demo(tmp_path)
    assert code == (2 if execution_error else 1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["targets"]) == (1 if execution_error else 5)
    saved = path.read_bytes()
    with pytest.raises(ReportWriteError):
        await demo.run_demo(tmp_path)
    assert path.read_bytes() == saved


@pytest.mark.anyio
async def test_demo_summary_write_failure_cannot_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = demo._write

    def fail_summary(path: Path, content: str) -> None:
        if path.name == "demo-summary.json":
            raise ReportWriteError("write failed")
        original(path, content)

    monkeypatch.setattr(demo, "_write", fail_summary)
    with pytest.raises(ReportWriteError):
        await demo.run_demo(tmp_path)
    assert len(list(tmp_path.rglob("manifest.json"))) == 1
    assert not list(tmp_path.rglob("demo-summary.json"))
