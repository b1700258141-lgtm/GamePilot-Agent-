"""12 格配对试验端到端：真实靶场 + 真实图 + 离线测试替身，一次跑完、逐格留证。

这里不替换任何执行路径：四个靶场是真实 ASGI 应用，Agent 走真实的 LangGraph
闭环，判定走与线上同一套 oracle，重跑走**既有** replay。因此「12 格通过」
这一结论连同它的分母一起被验证：12 格、3 个预定机会、6 项门槛指标、
48 份逐格证据，一个都不能少。

本次供应商是**测试替身**，所以这些数字只证明工程链路（图、预算、判定、
重跑）正确，不是 Agent 能力成绩——真实模型验收待完成。数据模型里也如实
带着这句说明，本文件把它当成对外结论来验证，而不是当成注释。

整条运行经由命令行入口 `python -m gamepilot.benchmark agent` 完成：
「实际运行命令与退出码」本身就是要交付的证据之一。
"""

import contextlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

# `langgraph` 属于可选的 agent extra（见 pyproject 与 README「游戏测试 Agent」）：
# 没装时整组测试明确跳过，而不是让收集阶段直接报导入错误。
pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')

from gamepilot.agent.models import AGENT_SCHEMA_VERSION, AgentRunReport, BudgetSpec
from gamepilot.agent.provider import AnthropicCompatibleProvider
from gamepilot.agent.report import EXIT_GAME_DEFECT
from gamepilot.benchmark import cli
from gamepilot.benchmark.agent_eval import (
    EXIT_EXECUTION_ERROR,
    EXIT_OK,
    _write_summary,
    acceptance_note,
    offline_provider_factory,
)
from gamepilot.benchmark.agent_manifest import (
    AGENT_BENCHMARK_VERSION,
    AGENT_COMBINATIONS,
    AGENT_EVAL_BUDGET,
    AGENT_GOAL_IDS,
    AGENT_MANIFEST_SOURCE,
    AGENT_SEED,
    DESIGNATED_OPPORTUNITIES,
    GOAL_SCRIPT_COUNTERPART,
    NORMAL_CELLS,
    OPPORTUNITY_TARGET_RULES,
    VARIANT_CELLS,
)
from gamepilot.benchmark.agent_models import AgentEvalReport
from gamepilot.lab import PROFILES
from gamepilot.testing.models import ReplayReport
from gamepilot.testing.replay import load_report
from gamepilot.testing.reporting import ReportWriteError
from gamepilot.testing.rules import RULES_SOURCE, RULES_VERSION, SCHEMA_VERSION

CELL_COUNT = len(AGENT_COMBINATIONS)
DESIGNATED_COUNT = len(DESIGNATED_OPPORTUNITIES)
# 每个格子留下四份证据：Agent 报告、运行报告、同 profile 重跑、脚本对照组。
FILES_PER_CELL = 4

# 门槛指标的分子/分母与目标：分母来自清单，未触发也不缩小分母。
GATE_METRICS = (
    ("designated_opportunities", DESIGNATED_COUNT, DESIGNATED_COUNT),
    ("distinct_defect_coverage", DESIGNATED_COUNT, DESIGNATED_COUNT),
    ("normal_false_positive", 0, 3),
    ("variant_untriggered_false_positive", 0, 6),
    ("execution_errors", 0, CELL_COUNT),
    ("replay_mismatch", 0, CELL_COUNT),
)
# 信息项：只如实列出，不参与门槛，因此不进入 metrics_passed/metrics_total。
INFORMATIONAL_METRICS = (
    ("beyond_designated", 0, CELL_COUNT),
    ("goal_coverage", 9, CELL_COUNT),
    ("goal_incomplete", 3, CELL_COUNT),
)


@dataclass
class EvalRun:
    """一次 12 格试验的全部产物：报告对象、落盘位置与命令行的对外输出。"""

    report: AgentEvalReport
    root: Path
    summary_path: Path
    exit_code: int
    output: str
    output_dir: Path


def _cell_path(root: Path, relative: str) -> Path:
    """逐格证据的相对路径（相对 root 记录）在读取时拼回绝对路径。"""
    return root / Path(relative)


@pytest.fixture(scope="module")
def agent_eval(tmp_path_factory: pytest.TempPathFactory) -> EvalRun:
    """真实跑一次 12 格试验（经命令行入口），供本模块的多个用例核对。

    只跑一次：一次试验已经覆盖了图、预算、判定、重跑与落盘五条路径，
    重复跑不会增加证据，只会让套件变慢。
    """
    output_dir = tmp_path_factory.mktemp("agent-benchmarks")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exit_code = cli.main(["agent", "--output-dir", str(output_dir)])
    summary_path = next(output_dir.rglob("agent-benchmark.json"))
    return EvalRun(
        report=AgentEvalReport.model_validate_json(summary_path.read_text(encoding="utf-8")),
        root=summary_path.parent,
        summary_path=summary_path,
        exit_code=exit_code,
        output=captured.getvalue(),
        output_dir=Path(output_dir),
    )


# ------------------------------------------------------------------ 清单与预算


def test_the_eval_covers_the_manifest_exactly_once(agent_eval: EvalRun) -> None:
    report = agent_eval.report
    summary = report.summary

    assert (report.mode, report.agent_benchmark_version) == (
        "agent-benchmark",
        AGENT_BENCHMARK_VERSION,
    )
    assert report.manifest_source == AGENT_MANIFEST_SOURCE
    assert report.seed == AGENT_SEED
    assert report.profiles == list(PROFILES)
    assert report.goal_ids == list(AGENT_GOAL_IDS)
    assert (report.rules_version, report.schema_version, report.rules_source) == (
        RULES_VERSION,
        SCHEMA_VERSION,
        RULES_SOURCE,
    )
    assert (summary.cells_planned, summary.cells_executed) == (CELL_COUNT, CELL_COUNT)

    keys = [(cell.profile, cell.goal_id) for cell in report.cells]
    assert sorted(keys) == sorted((row.profile, row.goal_id) for row in AGENT_COMBINATIONS)
    assert len(set(keys)) == CELL_COUNT, "同一格不得重复执行"
    for cell in report.cells:
        assert cell.goal == report.goal_texts[cell.goal_id]
        assert cell.control_case_id == GOAL_SCRIPT_COUNTERPART[cell.goal_id]


def test_the_eval_runs_on_the_default_budget(agent_eval: EvalRun) -> None:
    """评测用的是默认预算：付费与提高上限都必须由调用方显式给出，不能是评测的默认。"""
    report = agent_eval.report

    assert AGENT_EVAL_BUDGET == BudgetSpec()
    assert report.budget == BudgetSpec().model_dump()
    assert report.budget_seconds == AGENT_EVAL_BUDGET.total_timeout_seconds
    assert (len(NORMAL_CELLS), len(VARIANT_CELLS)) == (3, CELL_COUNT - 3)


def test_default_offline_usage_is_unknown_instead_of_zero(agent_eval: EvalRun) -> None:
    summary = agent_eval.report.summary

    assert (summary.usage_known_cells, summary.usage_unknown_cells) == (0, CELL_COUNT)
    assert summary.usage.available is False
    assert summary.usage.total_tokens is None
    assert summary.cost.amount is None
    assert agent_eval.report.pricing is None
    assert f"未知 {CELL_COUNT}/{CELL_COUNT}" in agent_eval.output


# -------------------------------------------------------------------- 门槛指标


def test_the_gate_metrics_pass_with_the_manifest_denominators(agent_eval: EvalRun) -> None:
    report = agent_eval.report
    gates = [metric for metric in report.metrics if metric.required]

    assert [
        (metric.metric_id, metric.numerator, metric.denominator, metric.status) for metric in gates
    ] == [(metric_id, num, den, "ok") for metric_id, num, den in GATE_METRICS]
    assert (report.summary.metrics_passed, report.summary.metrics_total) == (
        len(GATE_METRICS),
        len(GATE_METRICS),
    )
    assert (report.summary.status, report.summary.exit_code) == ("ok", EXIT_OK)
    assert agent_eval.exit_code == EXIT_OK


def test_informational_metrics_are_listed_but_not_counted(agent_eval: EvalRun) -> None:
    """信息项如实列出却不参与门槛：分子可以是 3、9 或 0，都不影响结论。"""
    report = agent_eval.report
    informational = [metric for metric in report.metrics if not metric.required]

    assert [
        (metric.metric_id, metric.numerator, metric.denominator) for metric in informational
    ] == list(INFORMATIONAL_METRICS)
    assert {metric.metric_id for metric in informational} == {
        "beyond_designated",
        "goal_coverage",
        "goal_incomplete",
    }
    assert len(report.metrics) == len(GATE_METRICS) + len(INFORMATIONAL_METRICS)


# ------------------------------------------------------------------ 供应商与结论


def test_the_provider_is_a_test_double_and_real_acceptance_is_pending(
    agent_eval: EvalRun,
) -> None:
    """这些数字只证明工程链路正确：报告必须自己说清楚，不能靠读者猜。"""
    report = agent_eval.report
    provider = report.provider

    assert report.summary.provider_is_test_double is True
    assert (provider["provider_id"], provider["is_test_double"]) == ("fake", True)
    assert provider["providers_seen"] == ["fake"], "12 格必须来自同一种供应商配置"
    assert provider["base_url"] is None
    # 离线试验不读密钥：这里连变量名都没有，更不会有值。
    assert provider["api_key_env"] is None

    note = report.summary.acceptance_note
    assert "测试替身" in note
    assert "真实模型验收待完成" in note
    # 证据文本里不应出现任何形似密钥的内容。
    assert "sk-" not in agent_eval.summary_path.read_text(encoding="utf-8")


def test_the_cli_prints_the_evidence_and_the_pending_acceptance(agent_eval: EvalRun) -> None:
    output = agent_eval.output

    assert "供应商：fake" in output
    assert "（测试替身=True）" in output
    assert f"清单来源：{AGENT_MANIFEST_SOURCE}" in output
    assert f"格数：计划 {CELL_COUNT}，执行 {CELL_COUNT}，目标达成 9" in output
    assert "目标未完成 3，执行错误 0" in output
    assert "预定机会：3/3，去重缺陷 3，normal 误报 0，重跑一致 12/12 可比" in output
    assert "逐格结果（T=真实触发，D=清单内检出，+=清单外额外检出）：" in output
    assert "Agent 评测结论：符合预期（门槛指标 6/6，退出码 0）" in output
    assert "真实模型验收待完成" in output
    assert f"摘要：{agent_eval.summary_path}" in output


# ---------------------------------------------------------------- 预定机会与检出


def test_each_designated_opportunity_is_triggered_and_hits_its_target_rules(
    agent_eval: EvalRun,
) -> None:
    """三个预定机会必须**真实触发**且命中各自的目标规则，缺一不算检出。"""
    report = agent_eval.report
    designated = [cell for cell in report.cells if cell.is_designated]

    assert len(designated) == DESIGNATED_COUNT
    assert {(cell.fault_id, cell.goal_id) for cell in designated} == set(DESIGNATED_OPPORTUNITIES)
    for cell in designated:
        key = (cell.fault_id, cell.goal_id)
        assert cell.triggered, f"{key} 未被靶场真实触发"
        assert cell.trigger_detail
        assert cell.target_rules == list(OPPORTUNITY_TARGET_RULES[key])
        assert cell.target_rules_missed == []
        assert cell.target_rules_hit == sorted(OPPORTUNITY_TARGET_RULES[key])
        assert cell.detected is True
        # 触发即代表这一格不可能「达成」：违规被判出来了。
        assert (cell.stop_reason, cell.exit_code, cell.case_status) == (
            "rule_failure",
            EXIT_GAME_DEFECT,
            "fail",
        )
        assert cell.goal_met is False and cell.completed is False

    assert (report.summary.designated_detected, report.summary.distinct_defects_detected) == (
        DESIGNATED_COUNT,
        DESIGNATED_COUNT,
    )


def test_the_script_counterpart_confirms_the_same_defect(agent_eval: EvalRun) -> None:
    """配对试验的对照半边：同一 profile 上，脚本跑出同样的目标规则失败。

    没有这条，Agent 的「检出」就可能只是模型自己制造的假象：
    脚本对照组用的是同一套 oracle、同一份会话参数，只是动作由脚本给定。
    """
    report = agent_eval.report

    for cell in report.cells:
        if cell.triggered:
            assert cell.control_case_status == "fail", f"{cell.profile} 的脚本对照没判失败"
            assert set(cell.target_rules_hit) <= set(cell.control_failing_rules), (
                f"{cell.profile} 的脚本对照没有命中 Agent 命中的目标规则"
            )
            # 离线替身走的是与脚本对照同长度的路径，逐格可比。
            assert cell.control_action_count == cell.spend.actions_attempted
        else:
            # 缺陷没被走到时脚本对照同样干净：此时的「无结论」不是误报。
            assert cell.control_case_status == "pass", f"{cell.profile} 的脚本对照意外失败"
            assert cell.control_failing_rules == []


def test_normal_cells_stay_clean_and_other_cells_meet_their_goals(agent_eval: EvalRun) -> None:
    report = agent_eval.report
    normal = [cell for cell in report.cells if cell.fault_id is None]

    assert len(normal) == len(NORMAL_CELLS)
    for cell in normal:
        assert cell.triggered is False
        assert cell.is_designated is False
        assert cell.failing_rules == []
        assert (cell.case_status, cell.goal_met, cell.stop_reason) == ("pass", True, "goal_met")
        assert cell.exit_code == EXIT_OK

    assert report.summary.normal_false_positives == 0
    # 除三个预定机会外，其余格都必须把目标跑完（否则「通过」只是没跑出结论）。
    for cell in report.cells:
        if not cell.is_designated:
            assert cell.goal_met is True, f"{cell.profile} × {cell.goal_id} 未达成目标"
            assert cell.failing_rules == []


def test_a_model_may_finish_early_only_when_coverage_is_already_met(agent_eval: EvalRun) -> None:
    """离线替身会主动申请结束；能否算完成由程序的覆盖条件判定，不是它自己说了算。"""
    report = agent_eval.report
    unfinished_by_budget = [
        cell for cell in report.cells if cell.stop_reason in ("action_budget", "model_call_budget")
    ]

    assert unfinished_by_budget == []
    assert (report.summary.goals_met, report.summary.goals_incomplete) == (
        CELL_COUNT - DESIGNATED_COUNT,
        DESIGNATED_COUNT,
    )


# ------------------------------------------------------------------ 无模型重跑


def test_every_cell_replays_without_a_model(agent_eval: EvalRun) -> None:
    """12 格各自同 profile 重跑一次：全部逐字段一致，且重跑不请求模型。"""
    report = agent_eval.report

    assert {cell.replay_outcome for cell in report.cells} == {"match"}
    assert {cell.replay_differences for cell in report.cells} == {0}
    assert all(cell.replay_run_id and cell.replay_run_id != cell.run_id for cell in report.cells)
    assert (report.summary.replay_matches, report.summary.replay_comparable) == (
        CELL_COUNT,
        CELL_COUNT,
    )
    # 重跑的同长度证据：动作数与运行报告一致。
    for cell in report.cells:
        assert cell.control_action_count == cell.spend.actions_attempted


def test_every_cell_keeps_its_own_evidence_on_disk(agent_eval: EvalRun) -> None:
    """逐格四份证据都在，而且**能被读回来**：路径写错就等于没有证据。"""
    root = agent_eval.root
    report = agent_eval.report

    assert root == agent_eval.output_dir / report.run_id
    assert len(list(root.rglob("*.json"))) == CELL_COUNT * FILES_PER_CELL + 1

    for cell in report.cells:
        cell_dir = root / "cells" / cell.profile / cell.goal_id
        # 任务级结论与单次运行证据分开存放：前者在格目录下，后者各归各的子目录。
        assert [path.name for path in cell_dir.glob("*.json")] == [f"{cell.run_id}-agent.json"]
        for kind, relative in (("run", cell.run_report_path), ("replay", cell.replay_path)):
            assert relative, f"{cell.profile} × {cell.goal_id} 缺少{kind}证据"
            assert len(list((cell_dir / kind).glob("*.json"))) == 1
            assert _cell_path(root, relative).parent.name == kind
        assert len(list((cell_dir / "control").glob("*.json"))) == 1
        assert _cell_path(root, cell.control_report_path).parent.name == "control"
        assert Path(cell.agent_report_path).parent.parts[-2:] == (cell.profile, cell.goal_id)

        # Agent 报告：结论与计数必须和摘要里记的一致，且指回那份运行报告。
        agent = AgentRunReport.model_validate_json(
            _cell_path(root, cell.agent_report_path).read_text(encoding="utf-8")
        )
        assert agent.schema_version == AGENT_SCHEMA_VERSION
        assert (agent.run_id, agent.goal_id, agent.goal) == (cell.run_id, cell.goal_id, cell.goal)
        assert agent.seed == AGENT_SEED
        assert agent.summary.stop_reason == cell.stop_reason
        assert agent.summary.exit_code == cell.exit_code
        assert agent.summary.goal_met == cell.goal_met
        assert agent.summary.actions_attempted == cell.spend.actions_attempted
        assert agent.summary.model_calls == cell.spend.model_calls
        assert agent.summary.format_retries == cell.spend.format_retries
        assert agent.coverage.met == cell.goal_met
        assert agent.run_report is not None
        assert agent.run_report.run_id == cell.run_id

        # 运行报告：schema=1.1，能被既有严格读取器读回，动作就是被接受的那几个。
        run = load_report(_cell_path(root, cell.run_report_path or ""))
        assert (run.mode, run.run_id) == ("run", cell.run_id)
        assert run.cases[0].case_id == f"agent-{cell.goal_id}"
        assert run.cases[0].status == cell.case_status
        assert [step.action for step in run.cases[0].executed_actions] == cell.accepted_actions

        # 重跑报告与脚本对照报告同样是可读的证据。
        replay = ReplayReport.model_validate_json(
            _cell_path(root, cell.replay_path).read_text(encoding="utf-8")
        )
        assert (replay.mode, replay.run_id) == ("replay", cell.replay_run_id)
        assert replay.summary.mismatched == 0
        assert replay.cases[0].case_id == f"agent-{cell.goal_id}"
        control = load_report(_cell_path(root, cell.control_report_path))
        assert (control.mode, control.run_id) == ("run", cell.control_run_id)
        assert control.cases[0].case_id == cell.control_case_id
        assert control.cases[0].status == cell.control_case_status


def test_the_summary_is_not_overwritten(agent_eval: EvalRun) -> None:
    """摘要已存在时必须失败：上次的证据不能被这次悄悄覆盖。"""
    before = agent_eval.summary_path.read_text(encoding="utf-8")

    with pytest.raises(ReportWriteError):
        _write_summary(agent_eval.report, agent_eval.root)

    assert agent_eval.summary_path.read_text(encoding="utf-8") == before


# -------------------------------------------------------------- 模型看不到什么


def test_the_model_never_sees_the_profile_or_the_defect(agent_eval: EvalRun) -> None:
    """模型输入里不得出现靶场名、故障编号或脚本对照的用例号。

    这是「独立判定」的前置条件：只要有一处泄露，检出就可能是背答案而不是
    真的从观测里发现问题。这里逐条核对**实际发出**的输入（报告里留档的那份）。
    """
    root = agent_eval.root
    report = agent_eval.report
    needles = [
        *PROFILES,
        *(cell.fault_id for cell in report.cells if cell.fault_id),
        *GOAL_SCRIPT_COUNTERPART.values(),
    ]

    for cell in report.cells:
        agent = AgentRunReport.model_validate_json(
            _cell_path(root, cell.agent_report_path).read_text(encoding="utf-8")
        )
        assert agent.model_calls, f"{cell.profile} × {cell.goal_id} 没有留下模型调用记录"
        blob = json.dumps([call.model_dump() for call in agent.model_calls], ensure_ascii=False)
        for needle in needles:
            assert needle not in blob, f"模型输入里出现了 {needle!r}（{cell.profile}）"
        # 正向：模型看到的是目标与公开观测，不是标准答案。
        first = agent.model_calls[0]
        assert first.messages[0].content.startswith("[公开规则")
        assert cell.goal in first.messages[-1].content
        assert "状态：" in first.messages[-1].content


# ------------------------------------------------------------ 付费路径的拒绝前置


def test_the_cli_refuses_a_paid_run_without_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """没给 `--paid`：一次模型请求都不发，连输出目录都不创建。"""
    code = cli.main(
        ["agent", "--provider", "anthropic-compatible", "--output-dir", str(tmp_path / "out")]
    )
    captured = capsys.readouterr()

    assert code == EXIT_EXECUTION_ERROR
    assert "付费运行默认关闭" in captured.err
    assert "--paid" in captured.err
    assert list(tmp_path.iterdir()) == [], "拒绝时不得留下任何产物"


def test_the_cli_reports_only_the_variable_name(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给了 `--paid` 但环境变量未设置：明确失败，且只出现变量名。"""
    from gamepilot.agent.provider import DEFAULT_API_KEY_ENV

    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    code = cli.main(
        [
            "agent",
            "--provider",
            "anthropic-compatible",
            "--paid",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    captured = capsys.readouterr()

    assert code == EXIT_EXECUTION_ERROR
    assert DEFAULT_API_KEY_ENV in captured.err
    assert list(tmp_path.iterdir()) == [], "找不到密钥时同样不产生任何产物"


@pytest.mark.parametrize(
    "extra",
    [
        ["--input-price", "1"],
        ["--output-price", "2"],
        ["--currency", "TEST"],
        ["--input-price", "1", "--output-price", "2"],
        ["--input-price", "-1", "--output-price", "2", "--currency", "TEST"],
        ["--input-price", "1", "--output-price", "2", "--currency", "   "],
    ],
)
def test_invalid_pricing_is_rejected_before_any_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    extra: list[str],
) -> None:
    output_dir = tmp_path / "out"
    code = cli.main(["agent", "--output-dir", str(output_dir), *extra])
    captured = capsys.readouterr()

    assert code == EXIT_EXECUTION_ERROR
    assert "输入错误" in captured.err
    assert not output_dir.exists()


def test_invalid_budget_is_rejected_before_any_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    output_dir = tmp_path / "out"
    code = cli.main(["agent", "--max-actions", "0", "--output-dir", str(output_dir)])
    captured = capsys.readouterr()

    assert code == EXIT_EXECUTION_ERROR
    assert "输入错误" in captured.err
    assert "max_action_attempts" in captured.err
    assert not output_dir.exists()


def test_timeout_alias_targets_the_http_budget_field() -> None:
    parser = cli.build_parser()

    assert parser.parse_args(["agent", "--timeout", "7"]).http_timeout == 7
    assert parser.parse_args(["agent", "--http-timeout", "8"]).http_timeout == 8


def test_the_acceptance_note_is_derived_from_the_provider_not_pasted() -> None:
    """那句「真实模型验收待完成」必须来自供应商身份，不能是写死的一句话。

    写死的说明会在真实模型跑起来之后继续撒谎——那正是这份报告最要紧的地方。
    """
    double = offline_provider_factory("healing")
    real = AnthropicCompatibleProvider(
        api_key="dummy-value-for-construction-only",
        model="claude-sonnet-5",
        base_url="http://model.invalid",
    )

    assert double.is_test_double is True and real.is_test_double is False
    assert "真实模型验收待完成" in acceptance_note(double)
    assert "真实模型验收待完成" not in acceptance_note(real)
