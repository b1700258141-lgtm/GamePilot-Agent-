"""穿过 HTTP 观测、真实 replay、落盘与 CLI 的分阶段错误回归。"""

from pathlib import Path

import httpx
import pytest

from gamepilot.benchmark import cli, runner
from gamepilot.benchmark.models import BenchmarkReport
from gamepilot.lab import create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import ReplayReport, RunReport
from gamepilot.testing.replay import load_report


def _error_response(request: httpx.Request, fault: str) -> httpx.Response:
    if fault == "timeout":
        raise httpx.ReadTimeout("injected timeout", request=request)
    return httpx.Response(503, json={"detail": "injected unavailable"})


def _inject_replays(monkeypatch: pytest.MonkeyPatch, injections: dict[int, str]) -> None:
    """只替换指定重放的 HTTP 目标；不伪造执行报告、比较结果或总状态。"""
    original = runner.replay_report
    calls = 0

    async def replay(client: GameClient, report: RunReport, *, source_report: str) -> ReplayReport:
        nonlocal calls
        calls += 1
        fault = injections.get(calls)
        if fault is None:
            return await original(client, report, source_report=source_report)
        transport = (
            httpx.ASGITransport(app=create_lab_app("potion_overheal"))
            if fault == "overheal"
            else httpx.MockTransport(lambda request: _error_response(request, fault))
        )
        async with httpx.AsyncClient(transport=transport) as http:
            injected = GameClient(http, base_url=client.base_url, timeout=client.timeout)
            return await original(injected, report, source_report=source_report)

    monkeypatch.setattr(runner, "replay_report", replay)


def _run_cli(output_dir: Path, expected_exit: int) -> tuple[BenchmarkReport, Path]:
    assert cli.main(["run", "--output-dir", str(output_dir)]) == expected_exit
    path = next(output_dir.glob("*/benchmark.json"))
    report = BenchmarkReport.model_validate_json(path.read_text(encoding="utf-8"))
    assert report.summary.exit_code == expected_exit
    # 固定分母不会因额外重放的错误而变化。
    metrics = {metric.metric_id: metric for metric in report.metrics}
    assert metrics["execution_errors"].denominator == 32
    assert metrics["replay_consistency"].denominator == 32
    assert metrics["target_detection"].denominator == 9
    return report, path.parent


@pytest.mark.parametrize("call", [1, 33], ids=["same-profile", "negative"])
@pytest.mark.parametrize("fault", ["503", "timeout"])
def test_replay_execution_error_exits_2_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    call: int,
    fault: str,
) -> None:
    _inject_replays(monkeypatch, {call: fault})
    report, root = _run_cli(tmp_path, 2)
    assert report.summary.status == "execution_error"
    assert report.summary.execution_errors == 0
    assert report.summary.replay_execution_errors == int(call == 1)
    assert report.summary.negative_execution_errors == int(call == 33)
    assert all(item.case_status != "error" for item in report.combinations)
    negative = report.negative_validation
    assert negative is not None
    if call == 1:
        item = report.combinations[0]
        assert item.replay_status == "error"
        error = item.replay_error
        path = item.replay_path
        assert negative.passed
    else:
        assert negative.execution_status == "error"
        assert negative.replay_outcome == "not_comparable"
        assert not negative.passed
        error = negative.execution_error
        path = negative.replay_path
        # 七个固定指标都达标，也不能掩盖额外的负向验证执行错误。
        assert all(metric.passed for metric in report.metrics)
    assert error and ("server_error" if fault == "503" else "timeout") in error
    replay = ReplayReport.model_validate_json((root / path).read_text(encoding="utf-8"))
    assert replay.cases[0].outcome == "not_comparable"
    execution = replay.cases[0].execution
    assert execution.status == "error" and execution.error == error
    observation = execution.create_observation
    assert observation is not None
    assert observation.status_code == (503 if fault == "503" else None)
    output = capsys.readouterr().out
    assert path in output
    assert error in output
    assert "退出码 2" in output


@pytest.mark.parametrize("error_call", [1, 33], ids=["same-profile", "negative"])
def test_replay_error_dominates_a_real_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_call: int,
) -> None:
    # 第三次重放为 normal/attack-then-potion；换到真实缺陷靶场会得到 mismatch。
    _inject_replays(monkeypatch, {error_call: "503", 3: "overheal"})
    report, _ = _run_cli(tmp_path, 2)
    assert report.combinations[2].replay_outcome == "mismatch"
    assert report.combinations[2].replay_status == "fail"
    assert report.combinations[2].replay_error is None
    assert report.summary.status == "execution_error"


@pytest.mark.parametrize("call", [3, 33], ids=["same-profile-mismatch", "negative-match"])
def test_comparison_deviation_without_execution_error_still_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: int,
) -> None:
    _inject_replays(monkeypatch, {call: "overheal"})
    report, _ = _run_cli(tmp_path, 1)
    assert report.summary.status == "deviation"
    assert report.summary.execution_errors == 0
    assert report.summary.replay_execution_errors == 0
    assert report.summary.negative_execution_errors == 0
    if call == 3:
        assert report.combinations[2].replay_outcome == "mismatch"
    else:
        negative = report.negative_validation
        assert negative is not None
        assert negative.replay_outcome == "match"
        assert negative.execution_status == "fail"
        assert negative.execution_error is None
        assert not negative.passed


def test_original_error_is_not_counted_again_when_replay_execution_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = runner.run_case
    calls = 0

    async def run_case(client, scenario):
        nonlocal calls
        calls += 1
        if calls != 1:
            return await original(client, scenario)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: _error_response(request, "503"))
        ) as http:
            return await original(GameClient(http, base_url=client.base_url), scenario)

    monkeypatch.setattr(runner, "run_case", run_case)
    report, root = _run_cli(tmp_path, 2)
    item = report.combinations[0]
    assert item.case_status == "error"
    assert item.case_error and "server_error" in item.case_error
    assert load_report(root / item.report_path).cases[0].error == item.case_error
    # 原运行未知导致不可比较，但新执行实际成功，不应再算一次重放错误。
    assert item.replay_outcome == "not_comparable"
    assert item.replay_status == "pass"
    assert item.replay_error is None
    assert report.summary.execution_errors == 1
    assert report.summary.replay_execution_errors == 0
    assert report.summary.negative_execution_errors == 0
