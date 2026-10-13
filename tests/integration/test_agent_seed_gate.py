"""四 seed 胜利门槛的固定分母、供应商隔离和付费保护。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamepilot.benchmark import agent_seed_gate as gate
from gamepilot.benchmark.cli import main


def test_offline_gate_preserves_eight_paired_cells(tmp_path: Path) -> None:
    report, path = asyncio.run(gate.run_agent_seed_gate(tmp_path))
    assert (report.planned, report.executed, report.skipped) == (8, 8, 0)
    assert report.exit_code == 0 and report.status == "ok"
    assert report.normal_goals_met == 4 and report.normal_false_positives == 0
    assert report.designated_reachable == report.designated_triggered == 4
    assert report.designated_detected == report.replay_matches - 4 == 4
    assert (report.max_model_calls, report.max_output_tokens) == (48, 24576)
    assert report.model_calls_observed is not None and report.model_calls_observed <= 48
    assert report.usage_known_cells == 0 and report.usage_unknown_cells == 8
    assert report.usage.available is False and report.cost.amount is None
    assert [(row.seed, row.profile) for row in report.rows] == [
        (seed, profile) for seed in gate.GATE_SEEDS for profile in gate.GATE_PROFILES
    ]
    for row in report.rows:
        assert row.status == "ok" and row.evidence is not None
        root = path.parent / row.report_root
        for reference in (
            row.evidence.agent_report_path,
            row.evidence.run_report_path,
            row.evidence.replay_path,
            row.evidence.control_report_path,
        ):
            assert reference and (root / reference).is_file()
    assert gate.GateReport.model_validate_json(path.read_text(encoding="utf-8")) == report


def test_gate_stops_after_execution_error_with_fixed_denominator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated sensitive provider failure")

    monkeypatch.setattr(gate, "_run_cell", fail)
    report, path = asyncio.run(gate.run_agent_seed_gate(tmp_path))
    assert (report.executed, report.skipped, report.execution_errors) == (1, 7, 1)
    assert report.status == "execution_error" and report.exit_code == 2
    assert report.rows[0].error_kind == "OSError"
    assert all(row.status == "skipped" for row in report.rows[1:])
    assert report.model_calls_observed is None and not report.usage.available
    assert "simulated sensitive provider failure" not in path.read_text(encoding="utf-8")


def test_gate_continues_after_valid_deviation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = gate._run_cell

    async def alter(*args: object, **kwargs: object):
        evidence = await original(*args, **kwargs)
        if kwargs["seed"] == 0 and evidence.profile == gate.PROFILE_NORMAL:
            evidence.goal_met = False
        return evidence

    monkeypatch.setattr(gate, "_run_cell", alter)
    report, _ = asyncio.run(gate.run_agent_seed_gate(tmp_path))
    assert (report.executed, report.skipped, report.deviations) == (8, 0, 1)
    assert report.status == "deviation" and report.exit_code == 1


def test_unreachable_manifest_is_rejected_before_any_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = gate.search_fault_reachability
    called = False

    def unreachable(*args: object, **kwargs: object):
        return original(*args, **kwargs).model_copy(update={"reachable": False})

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(gate, "search_fault_reachability", unreachable)
    monkeypatch.setattr(gate, "_run_cell", forbidden)
    with pytest.raises(ValueError, match="不可达"):
        asyncio.run(gate.run_agent_seed_gate(tmp_path))
    assert not called and not list(tmp_path.iterdir())


def test_manifest_drift_is_rejected_before_any_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gate, "GATE_COMBINATIONS", gate.GATE_COMBINATIONS[:1])
    with pytest.raises(ValueError, match="清单漂移"):
        asyncio.run(gate.run_agent_seed_gate(tmp_path))
    assert not list(tmp_path.iterdir())


def test_real_model_config_drift_is_rejected_before_any_cell(tmp_path: Path) -> None:
    provider = SimpleNamespace(
        provider_id="anthropic-compatible",
        model="unexpected-model",
        base_url=gate.GATE_BASE_URL,
        is_test_double=False,
        sampling={"thinking": "disabled", "max_retries": 0},
        api_key_env="TEST_KEY",
    )
    with pytest.raises(ValueError, match="真实模型配置"):
        asyncio.run(gate.run_agent_seed_gate(tmp_path, provider_factory=lambda _: provider))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "flags",
    [
        ["--provider", "anthropic-compatible"],
        ["--paid"],
        [
            "--provider",
            "anthropic-compatible",
            "--paid",
            "--api-key-env",
            "GAMEPILOT_MISSING_GATE_TEST_KEY",
        ],
    ],
)
def test_paid_guard_rejects_before_artifacts(
    flags: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GAMEPILOT_MISSING_GATE_TEST_KEY", raising=False)
    output = tmp_path / "rejected"
    assert main(["agent-seed-gate", *flags, "--output-dir", str(output)]) == 2
    assert not output.exists()
