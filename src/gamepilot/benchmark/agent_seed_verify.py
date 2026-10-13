"""只读核验固定 Agent 单格 canary 的摘要和四份配对证据。"""

import json
from pathlib import Path

from pydantic import ValidationError

from gamepilot.agent.models import AgentRunReport, ModelCallRecord, TokenUsage
from gamepilot.agent.report import derive_exit_code
from gamepilot.testing.replay import (
    ReplayInputError,
    load_replay_report,
    load_report,
    require_complete_fields,
)

from .agent_seed_canary import (
    CANARY_BUDGET,
    CANARY_GOAL_ID,
    CANARY_PROFILE,
    CANARY_SEED,
    CANARY_VERSION,
    MANIFEST_SOURCE,
    CanaryReport,
    _combination,
)
from .agent_seed_preflight import _row_status, control_scenario
from .agent_seed_reachability import search_fault_reachability


class CanaryEvidenceError(ValueError):
    """证据损坏、越界或结论与原始报告不一致。"""


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanaryEvidenceError(f"证据不可读取：{type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise CanaryEvidenceError("证据根节点必须是对象")
    return raw


def _reference(root: Path, value: str) -> Path:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not value
        or normalized.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or ":" in parts[0]
    ):
        raise CanaryEvidenceError("证据引用必须是安全的相对路径")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise CanaryEvidenceError("证据引用越界或文件不存在")
    return path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CanaryEvidenceError(message)


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _usage(calls: list[ModelCallRecord]) -> TokenUsage:
    if not calls or any(not call.usage.available for call in calls):
        return TokenUsage.unknown()
    return TokenUsage.of(
        sum(call.usage.prompt_tokens for call in calls),
        sum(call.usage.completion_tokens for call in calls),
        sum(call.usage.total_tokens for call in calls),
    )


def verify_agent_seed_canary(path: str | Path) -> CanaryReport:
    """不调用模型或 HTTP；严格读取后核对固定身份、原始事实和汇总。"""
    summary_path = Path(path)
    raw = _read_json(summary_path)
    try:
        report = CanaryReport.model_validate(raw)
        require_complete_fields(raw, report.model_dump(mode="json"))
    except (ValidationError, ReplayInputError) as exc:
        raise CanaryEvidenceError("单格摘要结构不完整或不合法") from exc
    combination = _combination()
    _require(
        (report.version, report.seed, report.profile, report.goal_id)
        == (CANARY_VERSION, CANARY_SEED, CANARY_PROFILE, CANARY_GOAL_ID),
        "单格身份或版本与固定清单不符",
    )
    _require(report.budget == CANARY_BUDGET.model_dump(), "单格预算与固定清单不符")
    _require(report.manifest_source == MANIFEST_SOURCE, "固定清单来源不一致")
    _require(
        report.completion_token_upper_bound
        == CANARY_BUDGET.max_model_calls * CANARY_BUDGET.max_output_tokens,
        "输出 Token 上限与预算不符",
    )
    _require(
        report.reachability
        == search_fault_reachability(
            report.seed, combination, max_action_attempts=CANARY_BUDGET.max_action_attempts
        ),
        "预算内可达性与固定清单不一致",
    )
    _require(
        report.exit_code == {"ok": 0, "deviation": 1, "execution_error": 2}[report.status],
        "评测状态与退出码不一致",
    )
    evidence = report.evidence
    if evidence is None:
        _require(
            report.status == "execution_error" and report.error_kind is not None,
            "缺少配对证据却未记录执行错误",
        )
        return report
    _require(report.error_kind is None, "已有配对证据时不应有顶层执行错误")
    _require(
        (evidence.profile, evidence.goal_id, evidence.fault_id)
        == (combination.profile, combination.goal_id, combination.fault_id)
        and evidence.is_designated
        and evidence.target_rules == list(combination.target_rules),
        "配对证据与固定清单不符",
    )
    root = summary_path.parent
    refs = (
        evidence.agent_report_path,
        evidence.run_report_path,
        evidence.replay_path,
        evidence.control_report_path,
    )
    _require(all(refs), "配对证据缺少报告引用")
    agent_path, run_path, replay_path, control_path = (_reference(root, ref) for ref in refs)
    try:
        agent_raw = _read_json(agent_path)
        agent = AgentRunReport.model_validate(agent_raw)
        require_complete_fields(agent_raw, agent.model_dump(mode="json"))
        run = load_report(run_path)
        replay = load_replay_report(replay_path)
        control = load_report(control_path)
    except (ValidationError, ReplayInputError) as exc:
        raise CanaryEvidenceError("配对原始报告结构或版本不合法") from exc
    _require(
        len(run.cases) == len(replay.cases) == len(control.cases) == 1, "配对报告必须各有一个场景"
    )
    case, replay_case, control_case = run.cases[0], replay.cases[0], control.cases[0]
    _require(
        agent.run_id == run.run_id == evidence.run_id
        and agent.seed == case.seed == control_case.seed == report.seed
        and agent.goal_id == evidence.goal_id
        and agent.session_id == case.session_id,
        "Agent 与游戏运行身份不一致",
    )
    _require(
        agent.run_report is not None
        and agent.run_report.run_id == run.run_id
        and _basename(agent.run_report.path) == run_path.name,
        "Agent 的游戏报告引用不一致",
    )
    _require(agent.budget.model_dump() == report.budget, "Agent 实际预算与摘要不一致")
    _require(
        agent.provider.model_dump(exclude={"source_revision", "source_dirty"}) == report.provider,
        "Agent 供应商配置与摘要不一致",
    )
    _require(
        replay.source_run_id == run.run_id
        and _basename(replay.source_report) == run_path.name
        and replay.run_id == evidence.replay_run_id
        and replay_case.case_id == case.case_id
        and replay_case.outcome == evidence.replay_outcome
        and replay_case.execution.status == evidence.replay_status
        and len(replay_case.differences) == evidence.replay_differences,
        "重放来源或结论与摘要不一致",
    )
    _require(
        control.run_id == evidence.control_run_id
        and control_case.session_id != case.session_id
        and control_case.scenario == control_scenario(report.seed, combination)
        and control_case.case_id == evidence.control_case_id
        and control_case.status == evidence.control_case_status
        and len(control_case.executed_actions) == evidence.control_action_count,
        "同 seed 脚本对照与摘要不一致",
    )
    failing = sorted({check.rule_id for check in case.checks if check.status == "fail"})
    control_failing = sorted(
        {check.rule_id for check in control_case.checks if check.status == "fail"}
    )
    _require(
        evidence.case_status == case.status
        and evidence.failing_rules == failing
        and evidence.target_rules_hit == sorted(set(failing) & set(evidence.target_rules))
        and evidence.control_failing_rules == control_failing
        and evidence.accepted_actions == [item.action for item in case.executed_actions],
        "动作或独立规则事实与摘要不一致",
    )
    _require(
        evidence.stop_reason == agent.summary.stop_reason
        and evidence.exit_code == agent.summary.exit_code
        and evidence.goal_met == agent.summary.goal_met
        and evidence.spend.model_calls == agent.summary.model_calls == len(agent.model_calls)
        and evidence.spend.actions_attempted == agent.summary.actions_attempted
        and evidence.spend.actions_attempted == len(case.executed_actions)
        and evidence.spend.format_retries == agent.summary.format_retries
        and evidence.spend.format_retries
        == sum(call.kind == "format_repair" for call in agent.model_calls)
        and agent.summary.rule_failures == failing
        and agent.summary.exit_code == derive_exit_code(agent.summary.stop_reason)
        and evidence.spend.model_calls <= CANARY_BUDGET.max_model_calls
        and evidence.spend.actions_attempted <= CANARY_BUDGET.max_action_attempts,
        "Agent 结论或预算计数与摘要不一致",
    )
    _require(
        evidence.spend.usage == agent.summary.usage == _usage(agent.model_calls)
        and evidence.spend.cost == agent.summary.cost,
        "逐次 Token 用量或费用与摘要不一致",
    )
    _require(
        report.status == _row_status(combination, evidence, reachable=True),
        "评测结论与配对事实不一致",
    )
    return report
