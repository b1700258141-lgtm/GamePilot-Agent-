"""两次独立运行的严格证据核对与可复现性对比。"""

import asyncio
import json
from pathlib import Path

import pytest

from gamepilot.benchmark.seed_compare import (
    SweepComparison,
    SweepInputError,
    _comparison_exit_code,
    _drift,
    _reference,
    compare_sweeps,
    load_sweep,
)
from gamepilot.benchmark.seed_sweep import run_seed_sweep
from gamepilot.lab import PROFILE_NORMAL


@pytest.fixture(scope="module")
def independent_runs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("independent-seed-sweeps")
    _, before = asyncio.run(run_seed_sweep(root / "before"))
    _, after = asyncio.run(run_seed_sweep(root / "after"))
    return before, after


def test_two_independent_runs_match_and_write_evidence(
    independent_runs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, after = independent_runs
    comparison, output = compare_sweeps(before, after, tmp_path)
    assert (comparison.planned, comparison.compared, comparison.drift_count) == (256, 256, 0)
    assert comparison.source_exit_codes == [0, 0]
    assert comparison.status == "match" and comparison.exit_code == 0
    assert SweepComparison.model_validate_json(output.read_text(encoding="utf-8")) == comparison


def test_same_run_is_not_independent(independent_runs: tuple[Path, Path], tmp_path: Path) -> None:
    before, _ = independent_runs
    with pytest.raises(SweepInputError, match="独立"):
        compare_sweeps(before, before, tmp_path)


def test_changed_summary_count_is_rejected(
    independent_runs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, _ = independent_runs
    raw = json.loads(before.read_text(encoding="utf-8"))
    raw["trigger_counts"]["potion_overheal"] += 1
    copied = tmp_path / before.parent.name / before.name
    copied.parent.mkdir()
    copied.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SweepInputError, match="计数"):
        load_sweep(copied)


def test_changed_run_file_is_rejected_by_digest(
    independent_runs: tuple[Path, Path], tmp_path: Path
) -> None:
    before, _ = independent_runs
    raw = json.loads(before.read_text(encoding="utf-8"))
    root = tmp_path / before.parent.name
    root.mkdir()
    (root / before.name).write_bytes(before.read_bytes())
    for field in ("run_reports", "replay_reports"):
        relative = Path(raw[field][PROFILE_NORMAL])
        source = before.parent / relative
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes() + (b" " if field == "run_reports" else b""))
    with pytest.raises(SweepInputError, match="指纹"):
        load_sweep(root / before.name)


def test_game_state_drift_is_found_even_when_cell_labels_match(
    independent_runs: tuple[Path, Path],
) -> None:
    before_path, after_path = independent_runs
    before, before_cases, _ = load_sweep(before_path)
    after, after_cases, _ = load_sweep(after_path)
    key = (PROFILE_NORMAL, "attack-potion-seed-0000")
    changed = after_cases[key].model_copy(deep=True)
    assert changed.final_snapshot is not None
    changed.final_snapshot.player.hp -= 1
    after_cases[key] = changed
    drift = _drift(before, after, before_cases, after_cases)
    assert len(drift) == 1
    assert drift[0].profile == PROFILE_NORMAL
    assert "run.final_snapshot" in drift[0].fields


def test_input_path_and_execution_error_precedence(tmp_path: Path) -> None:
    with pytest.raises(SweepInputError, match="安全"):
        _reference(tmp_path, "../outside.json")
    assert _comparison_exit_code([0, 0], 0) == 0
    assert _comparison_exit_code([0, 0], 1) == 1
    assert _comparison_exit_code([1, 0], 0) == 1
    assert _comparison_exit_code([2, 1], 3) == 2
