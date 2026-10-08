"""Agent 评测的分阶段错误必须主导最终退出码。"""

from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')

from gamepilot.agent.models import AgentRunReport, BudgetSpec, TokenUsage
from gamepilot.agent.provider import FakeProvider, ModelReply, ScriptedProvider
from gamepilot.benchmark import agent_eval
from gamepilot.benchmark.agent_eval import EXIT_DEVIATION, EXIT_EXECUTION_ERROR, run_agent_eval
from gamepilot.benchmark.agent_models import AgentPricing
from gamepilot.testing.models import ReplayDifference, ReplaySummary
from gamepilot.testing.reporting import ReportWriteError


@pytest.fixture
def one_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常优先级不需要重复跑满 12 格；保留一格真实闭环即可。"""
    monkeypatch.setattr(agent_eval, "AGENT_COMBINATIONS", agent_eval.AGENT_COMBINATIONS[:1])


@pytest.mark.parametrize("error_kind", ["timeout", "rate_limit", "server_error"])
@pytest.mark.anyio
async def test_model_error_is_an_agent_stage_execution_error(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    error_kind: str,
) -> None:
    def factory(_goal_id: str) -> FakeProvider:
        return FakeProvider(
            lambda _messages: ModelReply(
                error_kind=error_kind,
                error_detail=f"测试注入的模型错误：{error_kind}",
            )
        )

    report, _path = await run_agent_eval(tmp_path, provider_factory=factory)

    assert (report.summary.status, report.summary.exit_code) == (
        "execution_error",
        EXIT_EXECUTION_ERROR,
    )
    assert report.summary.execution_errors == 1
    assert report.summary.agent_execution_errors == 1
    assert report.summary.replay_execution_errors == 0
    assert report.cells[0].stop_reason == "model_error"
    assert report.cells[0].exit_code == EXIT_EXECUTION_ERROR


@pytest.mark.anyio
async def test_fail_fast_stops_paid_cells_but_keeps_auditable_fixed_denominators(
    tmp_path: Path,
) -> None:
    factory_calls: list[str] = []

    def factory(goal_id: str) -> FakeProvider:
        factory_calls.append(goal_id)
        return FakeProvider(
            lambda _messages: ModelReply(
                error_kind="rate_limit",
                error_detail="测试注入的首格限流",
            )
        )

    report, summary_path = await run_agent_eval(
        tmp_path,
        provider_factory=factory,
        fail_fast=True,
    )

    summary = report.summary
    assert len(factory_calls) == 1, "首格执行错误后不得再创建后续付费供应商调用"
    assert (summary.cells_planned, summary.cells_executed, summary.cells_skipped) == (12, 1, 11)
    assert summary.aborted_early is True
    assert summary.abort_after_cell == "normal × full-health"
    assert "rate_limit" in (summary.abort_reason or "")
    assert summary.goals_incomplete == 12
    assert summary.usage_unknown_cells == 12
    assert summary.replay_not_executed >= 11
    assert (summary.status, summary.exit_code) == ("execution_error", EXIT_EXECUTION_ERROR)

    metrics = {metric.metric_id: metric for metric in report.metrics}
    assert metrics["normal_false_positive"].denominator == 3
    assert metrics["variant_untriggered_false_positive"].denominator == 6
    assert metrics["execution_errors"].denominator == 12
    assert metrics["replay_mismatch"].denominator == 12
    assert metrics["goal_coverage"].denominator == 12
    assert AgentRunReport.model_validate_json(
        (summary_path.parent / report.cells[0].agent_report_path).read_text(encoding="utf-8")
    )


@pytest.mark.anyio
async def test_fail_fast_does_not_abort_on_a_valid_game_defect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    designated = next(row for row in agent_eval.AGENT_COMBINATIONS if row.is_designated)
    normal = next(row for row in agent_eval.AGENT_COMBINATIONS if row.fault_id is None)
    monkeypatch.setattr(agent_eval, "AGENT_COMBINATIONS", (designated, normal))

    report, _ = await run_agent_eval(tmp_path, fail_fast=True)

    assert (report.summary.cells_planned, report.summary.cells_executed) == (2, 2)
    assert report.summary.cells_skipped == 0
    assert report.summary.aborted_early is False
    assert report.summary.abort_after_cell is None
    assert report.cells[0].stop_reason == "rule_failure"
    assert report.cells[0].exit_code == EXIT_DEVIATION


@pytest.mark.anyio
async def test_eighth_cell_format_exhaustion_preserves_usage_and_stops_matrix(
    tmp_path: Path,
) -> None:
    """复现 Gate C 的第 8 格耗尽；保持清单不变，验证落盘证据及固定分母。"""
    factory_calls: list[str] = []

    def factory(goal_id: str) -> FakeProvider:
        factory_calls.append(goal_id)
        if len(factory_calls) == 8:
            return ScriptedProvider(
                [
                    ModelReply(
                        usage=TokenUsage.of(prompt, 512),
                        stop_reason="max_tokens",
                        response_block_types=["thinking"],
                    )
                    for prompt in (158, 216)
                ]
            )
        return agent_eval.offline_provider_factory(goal_id)

    report, path = await run_agent_eval(
        tmp_path,
        provider_factory=factory,
        budget=BudgetSpec(max_model_calls=6, max_action_attempts=6, max_format_retries=1),
        fail_fast=True,
    )
    summary = report.summary
    assert len(factory_calls) == 8
    assert (summary.cells_planned, summary.cells_executed, summary.cells_skipped) == (12, 8, 4)
    assert summary.exit_code == EXIT_EXECUTION_ERROR
    assert summary.abort_after_cell == "potion_not_consumed × healing"
    assert "agent" in summary.abort_reason
    assert summary.usage.available is False
    assert summary.cost.amount is None
    cell = report.cells[-1]
    assert cell.replay_outcome == "match"
    saved = AgentRunReport.model_validate_json(
        (path.parent / cell.agent_report_path).read_text(encoding="utf-8")
    )
    assert saved.schema_version == "1.3"
    assert saved.summary.stop_reason == "model_format_error"
    assert saved.summary.stop_detail in summary.abort_reason
    assert saved.summary.exit_code == EXIT_EXECUTION_ERROR
    assert saved.summary.actions_attempted == 0
    assert (saved.summary.model_calls, saved.summary.format_retries) == (2, 1)
    assert saved.summary.usage == TokenUsage.of(374, 1024)
    assert [c.response_block_types for c in saved.model_calls] == [["thinking"], ["thinking"]]
    assert all(c.response_text is None and not c.tool_calls for c in saved.model_calls)


@pytest.mark.anyio
async def test_custom_budget_and_pricing_reach_the_cell_report(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
) -> None:
    budget = BudgetSpec(
        max_action_attempts=7,
        max_model_calls=9,
        max_format_retries=1,
        model_timeout_seconds=17.0,
        http_timeout_seconds=3.0,
        total_timeout_seconds=99.0,
        max_output_tokens=321,
    )
    pricing = AgentPricing(
        currency="TEST",
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )

    report, summary_path = await run_agent_eval(tmp_path, budget=budget, pricing=pricing)

    assert report.budget == budget.model_dump()
    assert report.budget_seconds == 99.0
    assert report.pricing == pricing
    assert (report.summary.usage_known_cells, report.summary.usage_unknown_cells) == (0, 1)
    assert report.summary.cost.amount is None
    agent_path = summary_path.parent / report.cells[0].agent_report_path
    agent = AgentRunReport.model_validate_json(agent_path.read_text(encoding="utf-8"))
    assert agent.budget == budget
    assert agent.summary.cost.currency == "TEST"
    assert agent.summary.cost.amount is None


def _fail_nth_create(monkeypatch: pytest.MonkeyPatch, call_number: int) -> None:
    """在实际 ASGI 靶场的第 N 次创建会话时注入 503。"""
    original = agent_eval.create_lab_app

    def build(profile: str, *, recorder=None):
        app = original(profile, recorder=recorder)
        calls = 0

        @app.middleware("http")
        async def fail_create(request, call_next):
            nonlocal calls
            if request.method == "POST" and request.url.path == "/api/v1/game-sessions":
                calls += 1
                if calls == call_number:
                    return JSONResponse({"error": "injected"}, status_code=503)
            return await call_next(request)

        return app

    monkeypatch.setattr(agent_eval, "create_lab_app", build)


def _force_replay_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """保留真实重跑，只把比较结果注入为一个普通 mismatch。"""
    original = agent_eval.replay_report

    async def mismatch(*args: object, **kwargs: object):
        replay = await original(*args, **kwargs)  # type: ignore[arg-type]
        case = replay.cases[0].model_copy(
            update={
                "outcome": "mismatch",
                "differences": [
                    ReplayDifference(
                        field="injected",
                        recorded="recorded",
                        replayed="replayed",
                    )
                ],
            }
        )
        return replay.model_copy(update={"cases": [case], "summary": ReplaySummary(mismatched=1)})

    monkeypatch.setattr(agent_eval, "replay_report", mismatch)


@pytest.mark.anyio
async def test_real_replay_503_is_not_comparable_and_exits_2(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_nth_create(monkeypatch, 2)
    report, _path = await run_agent_eval(tmp_path)

    cell = report.cells[0]
    assert (cell.replay_outcome, cell.replay_status) == ("not_comparable", "error")
    assert report.summary.replay_comparable == 0
    assert report.summary.replay_not_comparable == 1
    assert report.summary.replay_execution_errors == 1
    assert (report.summary.status, report.summary.exit_code) == (
        "execution_error",
        EXIT_EXECUTION_ERROR,
    )


@pytest.mark.parametrize("with_control_error", [False, True])
@pytest.mark.anyio
async def test_replay_mismatch_is_exit_1_unless_an_execution_error_also_occurs(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    with_control_error: bool,
) -> None:
    _force_replay_mismatch(monkeypatch)
    if with_control_error:
        _fail_nth_create(monkeypatch, 3)

    report, _path = await run_agent_eval(tmp_path)

    assert report.cells[0].replay_outcome == "mismatch"
    assert report.summary.replay_mismatches == 1
    assert report.summary.replay_comparable == 1
    if with_control_error:
        assert (report.summary.status, report.summary.exit_code) == (
            "execution_error",
            EXIT_EXECUTION_ERROR,
        )
    else:
        assert (report.summary.status, report.summary.exit_code) == (
            "deviation",
            EXIT_DEVIATION,
        )


@pytest.mark.anyio
async def test_replay_report_write_error_is_preserved_and_exits_2(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = agent_eval.write_report

    def fail_replay_write(report: object, output_dir: str | Path) -> Path:
        if Path(output_dir).name == "replay":
            raise ReportWriteError("测试注入的重跑报告写入失败")
        return original(report, output_dir)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_eval, "write_report", fail_replay_write)
    report, _path = await run_agent_eval(tmp_path)

    assert (report.summary.status, report.summary.exit_code) == (
        "execution_error",
        EXIT_EXECUTION_ERROR,
    )
    assert report.summary.execution_errors == 1
    assert report.summary.replay_execution_errors == 1
    assert report.cells[0].replay_error == "测试注入的重跑报告写入失败"
    assert report.cells[0].replay_path == ""


@pytest.mark.parametrize("stage", ["run", "agent"])
@pytest.mark.anyio
async def test_primary_report_write_errors_keep_a_summary_and_exit_2(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    if stage == "run":
        original = agent_eval.write_report

        def fail_run_write(report: object, output_dir: str | Path) -> Path:
            if Path(output_dir).name == "run":
                raise ReportWriteError("测试注入的运行报告写入失败")
            return original(report, output_dir)  # type: ignore[arg-type]

        monkeypatch.setattr(agent_eval, "write_report", fail_run_write)
    else:

        def fail_agent_write(_report: object, _output_dir: str | Path) -> Path:
            raise ReportWriteError("测试注入的 Agent 报告写入失败")

        monkeypatch.setattr(agent_eval, "write_agent_report", fail_agent_write)

    report, _path = await run_agent_eval(tmp_path)
    cell = report.cells[0]

    assert (report.summary.status, report.summary.exit_code) == (
        "execution_error",
        EXIT_EXECUTION_ERROR,
    )
    assert report.summary.agent_execution_errors == 1
    assert report.summary.execution_errors == 1
    if stage == "run":
        assert cell.run_report_error == "测试注入的运行报告写入失败"
        assert cell.run_report_path is None
    else:
        assert cell.agent_report_error == "测试注入的 Agent 报告写入失败"
        assert cell.agent_report_path == ""


@pytest.mark.anyio
async def test_control_case_error_is_preserved_and_exits_2(
    tmp_path: Path,
    one_cell: None,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_nth_create(monkeypatch, 3)
    report, _path = await run_agent_eval(tmp_path)

    assert (report.summary.status, report.summary.exit_code) == (
        "execution_error",
        EXIT_EXECUTION_ERROR,
    )
    assert report.summary.execution_errors == 1
    assert report.summary.control_execution_errors == 1
    assert report.cells[0].control_case_status == "error"
    assert report.cells[0].control_case_error
    assert "server_error" in report.cells[0].control_case_error
