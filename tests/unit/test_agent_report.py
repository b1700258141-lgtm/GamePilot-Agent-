"""报告契约：退出码必须能从停止原因唯一推出，费用与版本不许猜。

退出码是对外契约，因此这里既检查每个取值的映射，也检查**完备性**：
新增停止原因却忘了登记时，测试要失败，而不是让它悄悄落到默认分支。
"""

import json
from pathlib import Path

import pytest

from gamepilot.agent.models import (
    AGENT_SCHEMA_VERSION,
    STOP_REASON_LABELS,
    AgentStopReason,
    CostEstimate,
    GoalCondition,
    GoalCoverage,
    TokenUsage,
)
from gamepilot.agent.report import (
    DEFECT_STOP_REASONS,
    ERROR_STOP_REASONS,
    EXIT_ERROR,
    EXIT_GAME_DEFECT,
    EXIT_INCOMPLETE,
    EXIT_OK,
    INCOMPLETE_STOP_REASONS,
    agent_report_path,
    derive_exit_code,
    estimate_cost,
    is_goal_met,
    source_revision,
    write_agent_report,
)

# 从报告契约里取出全部合法停止原因：这是「完备性」检查的基准。
ALL_STOP_REASONS: tuple[str, ...] = tuple(AgentStopReason.__args__)  # type: ignore[attr-defined]


def _coverage(met: bool) -> GoalCoverage:
    return GoalCoverage(
        goal_id="full-health",
        goal="验证满血时使用药水的处理及状态是否保持一致",
        met=met,
        conditions=[GoalCondition(condition_id="c1", met=met, detail="")],
    )


# ------------------------------------------------------------------ 退出码


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("goal_met", EXIT_OK),
        ("rule_failure", EXIT_GAME_DEFECT),
        ("execution_error", EXIT_ERROR),
        ("model_error", EXIT_ERROR),
        ("model_format_error", EXIT_ERROR),
        ("input_error", EXIT_ERROR),
        ("finish_requested", EXIT_INCOMPLETE),
        ("action_budget", EXIT_INCOMPLETE),
        ("model_call_budget", EXIT_INCOMPLETE),
        ("time_budget", EXIT_INCOMPLETE),
    ],
)
def test_every_registered_stop_reason_has_an_exit_code(stop_reason: str, expected: int) -> None:
    assert derive_exit_code(stop_reason) == expected


def test_stop_reason_partitions_cover_the_whole_contract() -> None:
    """三组停止原因恰好划满登记表：既没有漏登记的，也没有重复归类的。"""
    groups = [
        frozenset({"goal_met"}),
        DEFECT_STOP_REASONS,
        ERROR_STOP_REASONS,
        INCOMPLETE_STOP_REASONS,
    ]
    union = frozenset().union(*groups)
    assert union == set(ALL_STOP_REASONS)
    for left in range(len(groups)):
        for right in range(left + 1, len(groups)):
            assert not (groups[left] & groups[right]), "一个停止原因不得归入两组"


def test_every_stop_reason_has_a_label() -> None:
    assert set(STOP_REASON_LABELS) == set(ALL_STOP_REASONS)


def test_unknown_stop_reason_is_an_error_not_a_pass() -> None:
    """未登记的停止原因按错误处理：不静默当成成功，也不当成发现缺陷。"""
    assert derive_exit_code("something_new") == EXIT_ERROR
    assert derive_exit_code("") == EXIT_ERROR


def test_error_outranks_a_defect_conclusion() -> None:
    """错误优先：执行错误不会被包装成「发现了缺陷」（退出码 1）。"""
    assert derive_exit_code("execution_error") != EXIT_GAME_DEFECT
    assert derive_exit_code("model_error") != EXIT_GAME_DEFECT


# ------------------------------------------------------------------ 目标达成


def test_goal_met_requires_both_coverage_and_clean_rules() -> None:
    assert is_goal_met(_coverage(True), []) is True
    # 覆盖满足但有规则失败 → 不是达成：两条证据都是必需的。
    assert is_goal_met(_coverage(True), ["R-POTION-CAP"]) is False
    # 没有规则失败但覆盖没满足 → 同样不是达成（例如提前 finish）。
    assert is_goal_met(_coverage(False), []) is False


# ------------------------------------------------------------------ 费用


def test_cost_stays_unknown_without_explicit_prices() -> None:
    """没有显式单价就不给金额：报告里凭空写出金额比留空更糟。"""
    cost = estimate_cost(TokenUsage.of(1000, 500))
    assert cost.amount is None
    assert cost.method == "none"
    assert cost.label == "unknown"


def test_cost_stays_unknown_when_usage_is_unknown() -> None:
    """单价有了但用量未知时同样不给金额：未知的 Token 不能被当成 0。"""
    cost = estimate_cost(
        TokenUsage.unknown(), input_price_per_million=1.0, output_price_per_million=2.0
    )
    assert cost.amount is None
    assert cost.method == "tokens-unknown"


def test_cost_is_computed_from_explicit_prices_only() -> None:
    cost = estimate_cost(
        TokenUsage.of(1_000_000, 500_000),
        input_price_per_million=3.0,
        output_price_per_million=6.0,
        currency="USD",
    )
    assert cost.amount == pytest.approx(3.0 + 3.0)
    assert cost.currency == "USD"
    assert cost.label.endswith("USD")


def test_unknown_usage_is_not_recorded_as_zero() -> None:
    """available=False 的三个计数必须是 None：0 是「确认没有消耗」，含义相反。"""
    usage = TokenUsage.unknown()
    assert usage.available is False
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (None, None, None)
    with pytest.raises(ValueError):
        TokenUsage(available=False, prompt_tokens=0, completion_tokens=0, total_tokens=0)


def test_partial_usage_downgrades_the_total_to_unknown() -> None:
    """只要有一轮用量未知，合计就必须记未知，不能把未知那轮当 0 相加。"""
    assert TokenUsage.of(10, 5).plus(TokenUsage.unknown()).available is False
    assert TokenUsage.unknown().plus(TokenUsage.of(10, 5)).available is False
    combined = TokenUsage.of(10, 5).plus(TokenUsage.of(1, 2))
    assert (combined.prompt_tokens, combined.completion_tokens) == (11, 7)


# ------------------------------------------------------------------ 写入与版本


def _report_payload(run_id: str) -> dict:
    """构造一份最小但完整的 Agent 报告负载，用于写入路径测试。"""
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "mode": "agent-run",
        "run_id": run_id,
        "goal_id": "full-health",
        "goal": "验证满血时使用药水的处理及状态是否保持一致",
        "seed": 42,
        "base_url": "http://agent-unit.invalid",
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "duration_ms": 1000.0,
        "provider": {"provider_id": "fake", "model": "unit", "is_test_double": True},
        "budget": {},
        "prompts_version": "1.0",
        "rules_version": "1.0",
        "rules_source": "docs",
        "summary": {"stop_reason": "goal_met"},
        "coverage": {"goal_id": "full-health", "goal": "…", "met": True, "conditions": []},
    }


def test_report_path_is_derived_from_run_id_only(tmp_path: Path) -> None:
    path = agent_report_path(tmp_path, "20260101T000000Z-abcdef")
    assert path.name == "20260101T000000Z-abcdef-agent.json"
    assert path.parent == tmp_path


def test_report_path_rejects_a_crafted_run_id(tmp_path: Path) -> None:
    """run_id 会被用作文件名，外部文本永远不得进入路径。"""
    from gamepilot.testing.reporting import ReportWriteError

    with pytest.raises(ReportWriteError):
        agent_report_path(tmp_path, "../../etc/passwd")


def test_report_is_written_exclusively(tmp_path: Path) -> None:
    """报告是证据：已存在的文件不会被覆盖。"""
    from gamepilot.agent.models import AgentRunReport
    from gamepilot.testing.reporting import ReportWriteError

    report = AgentRunReport.model_validate(_report_payload("20260101T000000Z-abcdef"))
    path = write_agent_report(report, tmp_path)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["mode"] == "agent-run"
    assert written["summary"]["stop_reason"] == "goal_met"

    with pytest.raises(ReportWriteError):
        write_agent_report(report, tmp_path)
    # 原有内容原样保留。
    assert json.loads(path.read_text(encoding="utf-8")) == written


def test_report_rejects_unknown_fields(tmp_path: Path) -> None:
    """报告的 schema 是严格的：多出来的字段不会被静默忽略。"""
    from pydantic import ValidationError

    from gamepilot.agent.models import AgentRunReport

    payload = _report_payload("20260101T000000Z-abcdef")
    payload["unexpected"] = "x"
    with pytest.raises(ValidationError):
        AgentRunReport.model_validate(payload)


def test_source_revision_is_readable_or_explicitly_unknown() -> None:
    """版本探测失败只能是 `unknown`，不能抛错把一次跑完的运行打崩。"""
    revision, dirty = source_revision()
    assert isinstance(revision, str) and revision
    assert dirty is None or isinstance(dirty, bool)


def test_report_does_not_carry_a_cost_without_prices() -> None:
    """默认报告里的费用必须是 unknown，而不是 0。"""
    from gamepilot.agent.models import AgentSummary

    assert AgentSummary(stop_reason="goal_met").cost == CostEstimate()
    assert AgentSummary(stop_reason="goal_met").cost.amount is None
