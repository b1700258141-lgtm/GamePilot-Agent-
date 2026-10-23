"""固定单格 canary 的离线证据、付费保护与执行错误。"""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from gamepilot.benchmark import agent_seed_canary as canary
from gamepilot.benchmark.agent_seed_verify import CanaryEvidenceError, verify_agent_seed_canary
from gamepilot.benchmark.cli import main


def test_offline_canary_keeps_paired_evidence(tmp_path: Path) -> None:
    report, summary = asyncio.run(canary.run_agent_seed_canary(tmp_path))
    assert report.exit_code == 0 and report.status == "ok"
    assert report.seed == 2 and report.reachability.reachable
    assert report.reachability.witness_actions == ["attack", "attack", "power_strike"]
    assert report.completion_token_upper_bound == 3072
    assert report.provider["is_test_double"] is True
    evidence = report.evidence
    assert evidence is not None
    assert evidence.triggered and evidence.detected
    assert evidence.replay_outcome == "match"
    assert evidence.control_case_status == "fail"
    assert evidence.spend.model_calls <= 6
    assert evidence.spend.cost.amount is None
    for relative in (
        evidence.agent_report_path,
        evidence.run_report_path,
        evidence.replay_path,
        evidence.control_report_path,
    ):
        assert relative and (summary.parent / relative).is_file()
    assert canary.CanaryReport.model_validate_json(summary.read_text(encoding="utf-8")) == report


def test_real_provider_without_paid_is_rejected_before_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "unpaid"
    code = main(
        [
            "agent-seed-canary",
            "--provider",
            "anthropic-compatible",
            "--output-dir",
            str(output),
        ]
    )
    assert code == 2
    assert not output.exists()


def test_offline_paid_flag_is_rejected_before_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "invalid"
    code = main(["agent-seed-canary", "--paid", "--output-dir", str(output)])
    assert code == 2
    assert not output.exists()


def test_real_provider_without_key_is_rejected_before_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GAMEPILOT_MISSING_CANARY_TEST_KEY", raising=False)
    output = tmp_path / "missing-key"
    code = main(
        [
            "agent-seed-canary",
            "--provider",
            "anthropic-compatible",
            "--paid",
            "--api-key-env",
            "GAMEPILOT_MISSING_CANARY_TEST_KEY",
            "--output-dir",
            str(output),
        ]
    )
    assert code == 2
    assert not output.exists()


def test_unexpected_failure_records_unknown_spend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated provider failure")

    monkeypatch.setattr(canary, "_run_cell", fail)
    report, summary = asyncio.run(canary.run_agent_seed_canary(tmp_path))
    assert report.status == "execution_error" and report.exit_code == 2
    assert report.error_kind == "OSError" and report.evidence is None
    assert "simulated provider failure" not in summary.read_text(encoding="utf-8")


def test_read_only_verifier_accepts_offline_evidence(tmp_path: Path) -> None:
    report, summary = asyncio.run(canary.run_agent_seed_canary(tmp_path))
    assert verify_agent_seed_canary(summary) == report
    assert main(["agent-seed-canary-verify", "--report", str(summary)]) == 0


def test_read_only_verifier_rejects_cross_file_tampering(tmp_path: Path) -> None:
    _, summary = asyncio.run(canary.run_agent_seed_canary(tmp_path / "original"))
    for name, mutate in (
        ("seed", lambda raw: raw.update(seed=3)),
        (
            "escape",
            lambda raw: raw["evidence"].update(run_report_path="../../outside.json"),
        ),
        (
            "usage",
            lambda raw: raw["evidence"]["spend"]["usage"].update(total_tokens=999999),
        ),
    ):
        copied = tmp_path / name
        shutil.copytree(summary.parent, copied)
        target = copied / summary.name
        raw = json.loads(target.read_text(encoding="utf-8"))
        mutate(raw)
        target.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(CanaryEvidenceError):
            verify_agent_seed_canary(target)
        assert main(["agent-seed-canary-verify", "--report", str(target)]) == 2

    copied = tmp_path / "rule"
    shutil.copytree(summary.parent, copied)
    source = json.loads((copied / summary.name).read_text(encoding="utf-8"))
    run_path = copied / source["evidence"]["run_report_path"]
    run = json.loads(run_path.read_text(encoding="utf-8"))
    failed = next(check for check in run["cases"][0]["checks"] if check["status"] == "fail")
    failed["rule_id"] = "R-ALTERED"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    with pytest.raises(CanaryEvidenceError):
        verify_agent_seed_canary(copied / summary.name)
