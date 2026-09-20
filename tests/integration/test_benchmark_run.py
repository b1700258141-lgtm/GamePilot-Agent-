"""评测端到端：真实跑完 32 个组合、同 profile 重跑、负向验证与退出码。

这里不替换任何执行路径：四个靶场应用是真实 ASGI 应用，请求经由真实路由
与真实仓储，判定走的是与线上同一套 oracle。因此「评测通过」这一结论
连同它的分母一起被验证：32 个组合、33 次重跑、7 项指标，一个都不能少。

退出码同样是对外行为：0 只在全部符合清单时出现；
报告写不进去或跑不出结论时必须返回 2，不能因为「缺陷被检出」就报成功。
"""

import asyncio
import json
from pathlib import Path

import pytest

from gamepilot.benchmark import cli
from gamepilot.benchmark.manifest import (
    BENCHMARK_VERSION,
    COMBINATIONS,
    MANIFEST_SOURCE,
    NEGATIVE_VALIDATION_SOURCE,
    SUITE,
)
from gamepilot.benchmark.models import BenchmarkReport, BenchmarkSummary, MetricStatus
from gamepilot.benchmark.runner import (
    BENCHMARK_BASE_URL,
    EXIT_DEVIATION,
    EXIT_EXECUTION_ERROR,
    EXIT_OK,
    BenchmarkExecutionError,
    _write_summary,
    run_benchmark,
)
from gamepilot.lab import PROFILE_NORMAL, PROFILES
from gamepilot.testing.models import ReplayReport
from gamepilot.testing.replay import load_report
from gamepilot.testing.rules import (
    RULES_SOURCE,
    RULES_VERSION,
    SCHEMA_VERSION,
)
from gamepilot.testing.runner import ReportWriteError, current_environment

RUN_COUNT = len(COMBINATIONS)
FAILING_COUNT = sum(1 for row in COMBINATIONS if row.expect_fail)
EXPECTED_METRICS = [
    ("defect_coverage", 3, 3),
    ("trigger_accuracy", 9, 9),
    ("target_detection", 9, 9),
    ("normal_false_positive", 0, 8),
    ("variant_false_positive", 0, 15),
    ("execution_errors", 0, RUN_COUNT),
    ("replay_consistency", RUN_COUNT, RUN_COUNT),
]


@pytest.fixture(scope="module")
def benchmark_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[BenchmarkReport, Path]:
    """真实跑一次完整评测，报告与目录供本模块的多个用例核对。

    这是进程内的真实运行（四个隔离应用 + 真实 HTTP 路由），
    只是不经过命令行入口；入口本身由下面的 CLI 用例单独验证。
    """
    output_dir = tmp_path_factory.mktemp("benchmarks")
    report = asyncio.run(run_benchmark(output_dir))
    return report, Path(output_dir) / report.run_id


# ----------------------------------------------------------------- 整体结论


def test_benchmark_matches_the_whole_manifest(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, _ = benchmark_run
    summary = report.summary

    assert (report.mode, report.suite) == ("benchmark", SUITE)
    assert report.benchmark_version == BENCHMARK_VERSION
    assert report.manifest_source == MANIFEST_SOURCE
    assert (report.rules_version, report.schema_version, report.rules_source) == (
        RULES_VERSION,
        SCHEMA_VERSION,
        RULES_SOURCE,
    )
    assert report.base_url == BENCHMARK_BASE_URL
    assert report.profiles == list(PROFILES)
    assert (summary.combinations_planned, summary.combinations_executed) == (RUN_COUNT, RUN_COUNT)
    assert (summary.as_expected, summary.deviations, summary.execution_errors) == (RUN_COUNT, 0, 0)
    assert (summary.status, summary.exit_code) == ("ok", EXIT_OK)
    assert (summary.metrics_passed, summary.metrics_total) == (
        len(EXPECTED_METRICS),
        len(EXPECTED_METRICS),
    )
    assert summary.negative_validation_passed is True


def test_every_combination_is_covered_exactly_once(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, _ = benchmark_run
    keys = [(item.profile, item.case_id) for item in report.combinations]

    assert sorted(keys) == sorted((row.profile, row.case_id) for row in COMBINATIONS)
    assert all(item.matrix_ok for item in report.combinations)
    assert all(not item.notes for item in report.combinations)


def test_faults_are_really_triggered_and_normal_is_not(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """触发来自靶场的真实偏离记录，且只出现在该故障自己的目标格上。"""
    report, _ = benchmark_run
    triggered = [item for item in report.combinations if item.triggered]

    assert len(triggered) == FAILING_COUNT
    assert all(item.expect_trigger for item in triggered)
    assert all(item.trigger_detail for item in triggered)
    assert not any(item.unexpected_trigger for item in report.combinations)
    assert not [
        item for item in report.combinations if item.profile == PROFILE_NORMAL and item.triggered
    ]


def test_each_expected_failure_hits_its_target_rules(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, _ = benchmark_run

    for item in report.combinations:
        if item.expect_fail:
            assert item.case_status == "fail", item.case_id
            assert item.target_rules_hit == sorted(item.target_rules)
            assert not item.target_rules_missed
        else:
            assert item.case_status == "pass", item.case_id
            assert not item.failing_rules
            assert not item.target_rules


def test_same_profile_replays_all_match(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """32 个组合各自同 profile 重跑一次，全部逐字段一致。"""
    report, _ = benchmark_run

    assert len(report.combinations) == RUN_COUNT
    assert {item.replay_outcome for item in report.combinations} == {"match"}
    assert {item.replay_differences for item in report.combinations} == {0}
    assert all(item.replay_run_id for item in report.combinations)
    assert all(item.replay_run_id != item.run_id for item in report.combinations)


def test_metrics_report_exact_numerators_and_denominators(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, _ = benchmark_run

    assert [
        (metric.metric_id, metric.numerator, metric.denominator, metric.passed)
        for metric in report.metrics
    ] == [(metric_id, num, den, True) for metric_id, num, den in EXPECTED_METRICS]


# ------------------------------------------------------------- 负向验证（1 次）


def test_negative_validation_replays_a_defect_trace_on_normal(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """同一份动作序列放到 normal 上必须比对出差异，否则判定器分辨不出缺陷。"""
    report, root = benchmark_run
    negative = report.negative_validation

    assert negative is not None
    assert (negative.source_profile, negative.source_case_id) == NEGATIVE_VALIDATION_SOURCE
    assert negative.expected_outcome == "mismatch"
    assert negative.replay_outcome == "mismatch"
    assert negative.actions_identical is True
    assert negative.differences > 0
    assert negative.passed is True
    # 负向验证的来源就是那份真实的缺陷报告。
    source = json.loads((root / negative.report_path).read_text(encoding="utf-8"))
    failing = {
        check["rule_id"] for check in source["cases"][0]["checks"] if check["status"] == "fail"
    }
    assert "R-POTION-CAP" in failing


def test_negative_validation_is_not_counted_in_the_matrix(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """负向验证是第 33 次重跑，不属于 32 个组合中的任何一个。"""
    report, root = benchmark_run
    negative = report.negative_validation
    assert negative is not None

    assert Path(negative.replay_path).parts[:2] == ("replays", "negative")
    assert Path(negative.replay_path) not in {
        Path(item.replay_path) for item in report.combinations
    }
    assert len(list((root / "replays").glob("*/*/*.json"))) == RUN_COUNT + 1


# --------------------------------------------------------------- 落盘与不覆盖


def test_report_layout_and_counts_on_disk(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, root = benchmark_run

    assert root.name == report.run_id
    runs = sorted((root / "runs").glob("*/*/*.json"))
    replays = sorted((root / "replays").glob("*/*/*.json"))
    assert len(runs) == RUN_COUNT
    assert len(replays) == RUN_COUNT + 1
    # 每个组合的原始报告与重跑报告都能被读回，且来自同一个组合。
    for item in report.combinations:
        run_report = load_report(root / item.report_path)
        replay_report = ReplayReport.model_validate_json(
            (root / item.replay_path).read_text(encoding="utf-8")
        )
        assert (run_report.mode, run_report.run_id) == ("run", item.run_id)
        assert (replay_report.mode, replay_report.run_id) == ("replay", item.replay_run_id)
        assert run_report.cases[0].case_id == item.case_id
        assert [case.case_id for case in replay_report.cases] == [item.case_id]


def test_run_reports_do_not_leak_profile_or_trigger_information(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """执行器报告里只有观测，没有缺陷标签、标准答案或触发说明。"""
    report, root = benchmark_run

    for item in report.combinations:
        text = (root / item.report_path).read_text(encoding="utf-8")
        assert item.profile not in text
        assert "fault" not in text
        assert "profile" not in text
        if item.trigger_detail:
            assert item.trigger_detail not in text


def test_profile_mapping_only_exists_in_the_benchmark_summary(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    report, root = benchmark_run

    assert set(report.profile_map) == set(PROFILES)
    assert all(report.profile_map[profile] for profile in PROFILES)
    text = (root / "benchmark.json").read_text(encoding="utf-8")
    for profile in PROFILES:
        assert profile in text
    # 摘要本身是中文可读的（不写成 \uXXXX 转义，便于人工核对）。
    assert "\\u" not in text
    assert report.profile_map[PROFILE_NORMAL] in text


def test_benchmark_summary_is_not_overwritten(
    benchmark_run: tuple[BenchmarkReport, Path],
) -> None:
    """摘要已存在时必须失败：不能悄悄覆盖上一次的证据。"""
    report, root = benchmark_run
    path = root / "benchmark.json"
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ReportWriteError):
        _write_summary(report, root)

    assert path.read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ 命令行入口


def test_cli_run_exits_0_and_prints_the_manifest_result(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(["run", "--output-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert f"符合清单 {RUN_COUNT}，偏差 0，原始运行执行错误 0" in output
    assert "逐组合结果（T=真实触发，U=意外触发，!=不符清单）：" in output
    assert "负向验证（不计入 32 次重跑）：通过" in output
    assert f"退出码 {EXIT_OK}" in output

    root = next(item for item in tmp_path.iterdir() if item.is_dir())
    assert (root / "benchmark.json").exists()
    summary = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))["summary"]
    assert summary["exit_code"] == EXIT_OK
    assert summary["as_expected"] == RUN_COUNT


def test_cli_run_exits_2_when_the_summary_cannot_be_written(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告写不进去属于执行错误：即使缺陷全部检出也不能报 0。"""
    delivered: list[str] = []

    async def _failing_run(output_dir: str | Path, *, timeout: float = 1.0) -> BenchmarkReport:
        delivered.append(str(output_dir))
        raise ReportWriteError("无法写入评测摘要 benchmark.json")

    monkeypatch.setattr(cli, "run_benchmark", _failing_run)

    code = cli.main(["run", "--output-dir", str(tmp_path)])

    assert code == EXIT_EXECUTION_ERROR
    assert delivered == [str(tmp_path)]
    assert "报告写入失败" in capsys.readouterr().err


def test_cli_run_exits_2_when_the_run_cannot_be_completed(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _broken(output_dir: str | Path, *, timeout: float = 1.0) -> BenchmarkReport:
        raise BenchmarkExecutionError("刚写出的报告无法重新读取")

    monkeypatch.setattr(cli, "run_benchmark", _broken)

    assert cli.main(["run", "--output-dir", str(tmp_path)]) == EXIT_EXECUTION_ERROR
    assert "评测执行失败" in capsys.readouterr().err


def test_cli_run_exits_2_for_an_unexpected_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _explode(output_dir: str | Path, *, timeout: float = 1.0) -> BenchmarkReport:
        raise RuntimeError("评测中途炸了")

    monkeypatch.setattr(cli, "run_benchmark", _explode)

    assert cli.main(["run", "--output-dir", str(tmp_path)]) == EXIT_EXECUTION_ERROR
    assert "评测异常终止" in capsys.readouterr().err


def test_cli_exit_code_follows_the_report_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要说 1 就必须退出 1：入口不加工、不改写执行结论。"""
    delivered: list[str] = []

    async def _deviation(output_dir: str | Path, *, timeout: float = 1.0) -> BenchmarkReport:
        delivered.append(str(output_dir))
        return _stub_report("deviation", EXIT_DEVIATION)

    monkeypatch.setattr(cli, "run_benchmark", _deviation)

    code = cli.main(["run", "--output-dir", str(tmp_path)])

    assert (code, delivered) == (EXIT_DEVIATION, [str(tmp_path)])
    output = capsys.readouterr().out
    assert "存在偏差" in output
    assert f"退出码 {EXIT_DEVIATION}" in output


def test_cli_run_exits_2_when_the_output_directory_is_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    output_file = tmp_path / "existing-file"
    output_file.write_text("keep", encoding="utf-8")

    code = cli.main(["run", "--output-dir", str(output_file)])

    assert code == EXIT_EXECUTION_ERROR
    assert "报告写入失败" in capsys.readouterr().err
    assert output_file.read_text(encoding="utf-8") == "keep"


def _stub_report(status: MetricStatus, exit_code: int) -> BenchmarkReport:
    """最小可用摘要：只用于验证入口如何转发执行结论。"""
    return BenchmarkReport(
        benchmark_version=BENCHMARK_VERSION,
        manifest_source=MANIFEST_SOURCE,
        suite=SUITE,
        run_id="stub-run",
        started_at="2026-09-20T00:00:00Z",
        finished_at="2026-09-20T00:00:01Z",
        duration_ms=1.0,
        timeout_seconds=1.0,
        base_url=BENCHMARK_BASE_URL,
        profiles=list(PROFILES),
        profile_map={profile: "（桩）" for profile in PROFILES},
        target_rules={profile: [] for profile in PROFILES},
        rules_version=RULES_VERSION,
        schema_version=SCHEMA_VERSION,
        rules_source=RULES_SOURCE,
        environment=current_environment(),
        summary=BenchmarkSummary(
            combinations_planned=RUN_COUNT,
            combinations_executed=RUN_COUNT,
            as_expected=RUN_COUNT - 1,
            deviations=1,
            execution_errors=0,
            replay_execution_errors=0,
            negative_execution_errors=0,
            metrics_passed=1,
            metrics_total=1,
            negative_validation_passed=True,
            status=status,
            exit_code=exit_code,
        ),
    )
