"""用已重跑的游戏事实构造缺陷单，并复用原判定器执行回归。"""

import hashlib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from gamepilot.agent.models import AGENT_SCHEMA_VERSION, AgentRunReport
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import CaseReport, ReplayReport, RuleCheck, RunReport, Scenario
from gamepilot.testing.planning import derive_expectation
from gamepilot.testing.replay import (
    ReplayInputError,
    compare_case,
    load_replay_report,
    load_report,
    require_complete_fields,
)
from gamepilot.testing.reporting import (
    ReportWriteError,
    new_run_id,
    single_case_report,
    write_report,
)
from gamepilot.testing.rules import (
    R_CONTROL_MATCH,
    R_CREATE_GET_IDENTICAL,
    R_CREATE_SEED,
    R_CREATE_STATUS,
    R_EXPECTED_CODE,
    R_EXPECTED_STATUS,
    R_FINAL_STATUS,
    R_GET_STATUS,
    R_INIT_STATE,
    R_STEP_LIMIT,
    RULE_CATALOG,
    RULES_VERSION,
)
from gamepilot.testing.runner import run_case

from .models import DefectArtifact, SourceRef

EXCLUDED_RULES = frozenset(
    {
        R_CONTROL_MATCH,
        R_CREATE_GET_IDENTICAL,
        R_CREATE_SEED,
        R_CREATE_STATUS,
        R_EXPECTED_CODE,
        R_EXPECTED_STATUS,
        R_FINAL_STATUS,
        R_GET_STATUS,
        R_INIT_STATE,
        R_STEP_LIMIT,
    }
)
GAME_STEP_RULES = frozenset(RULE_CATALOG) - EXCLUDED_RULES
_SECRET = re.compile(r"(?i)sk[-_][a-z0-9_-]{8,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+")


class DefectInputError(Exception):
    """输入不是可确认的游戏缺陷，或回归规格已被篡改。"""


def _safe_text(value: str) -> str:
    return _SECRET.sub("[redacted]", " ".join(value.split()))[:300]


def _fingerprint(rules_version: str, rule_id: str, seed: int, actions: list[str]) -> str:
    payload = json.dumps(
        {"rules_version": rules_version, "rule_id": rule_id, "seed": seed, "actions": actions},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_ref(path: Path, run_id: str) -> SourceRef:
    try:
        resolved = path.resolve(strict=True)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise DefectInputError(f"证据文件无法读取：{type(exc).__name__}") from exc
    return SourceRef(
        run_id=run_id,
        path=str(resolved),
        sha256=digest,
    )


def _same_file(recorded: str, supplied: Path) -> bool:
    return Path(recorded).resolve() == supplied.resolve()


def _choose_case(report: object, case_id: str) -> CaseReport:
    cases = getattr(report, "cases")
    matches = [case for case in cases if case.case_id == case_id]
    if len(matches) != 1:
        raise DefectInputError("原始报告中的场景 ID 缺失或不唯一")
    return matches[0]


def _failed_checks(case: CaseReport) -> list[RuleCheck]:
    failure = case.failure
    if (
        case.status != "fail"
        or case.error is not None
        or failure is None
        or failure.kind != "fail"
        or failure.rule_id not in GAME_STEP_RULES
        or failure.step is None
        or failure.step <= 0
        or failure.step != len(case.executed_actions)
        or not case.steps
    ):
        raise DefectInputError("原始场景没有可导出的动作规则失败，或混有执行错误")
    step = case.steps[-1]
    if step.index != failure.step or step.status != "fail" or step.after is None:
        raise DefectInputError("失败步骤与动作证据不一致")
    checks = [check for check in step.checks if check.status == "fail"]
    if not checks or checks[0].rule_id != failure.rule_id:
        raise DefectInputError("首个失败规则与步骤证据不一致")
    if any(check.step != failure.step for check in checks):
        raise DefectInputError("规则失败位置与步骤证据不一致")
    for evidence, plan in zip(case.steps, case.scenario.steps, strict=False):
        if evidence.index > len(case.executed_actions):
            break
        if derive_expectation(evidence.before, plan.action) != plan:
            raise DefectInputError("原始动作预期不是按公开规则确定的")
    return checks


def _validate_replay(
    replay: ReplayReport, run_path: Path, run_id: str, original: CaseReport
) -> None:
    if replay.source_run_id != run_id or not _same_file(replay.source_report, run_path):
        raise DefectInputError("重跑报告没有引用本次原始运行")
    if replay.summary.not_comparable:
        raise DefectInputError("重跑含不可比的执行错误")
    matches = [case for case in replay.cases if case.case_id == original.case_id]
    if len(matches) != 1:
        raise DefectInputError("重跑报告中的目标场景缺失或不唯一")
    recorded = matches[0]
    if (
        recorded.outcome != "match"
        or recorded.differences
        or recorded.execution.status != "fail"
        or compare_case(original, recorded.execution)
    ):
        raise DefectInputError("目标缺陷未在无模型重跑中逐字段复现")


def _load_agent(
    path: Path, run_path: Path, run: RunReport, case: CaseReport, rule_id: str
) -> AgentRunReport:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DefectInputError(f"Agent 报告无法读取：{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != AGENT_SCHEMA_VERSION:
        raise DefectInputError("Agent 报告版本不受支持")
    try:
        report = AgentRunReport.model_validate(raw)
        require_complete_fields(raw, report.model_dump(mode="json"))
    except (ValidationError, ReplayInputError) as exc:
        raise DefectInputError("Agent 报告缺少字段或结构不合法") from exc
    ref = report.run_report
    if (
        report.run_id != run.run_id
        or report.seed != case.seed
        or report.rules_version != run.rules_version
        or report.session_id != case.session_id
        or report.base_url != run.base_url
        or report.summary.stop_reason != "rule_failure"
        or report.summary.exit_code != 1
        or rule_id not in report.summary.rule_failures
        or ref is None
        or ref.run_id != run.run_id
        or not _same_file(ref.path, run_path)
    ):
        raise DefectInputError("Agent 结论与游戏运行报告不一致")
    return report


def export_defect(
    run_path: str | Path,
    replay_path: str | Path,
    case_id: str,
    output_dir: str | Path,
    *,
    rule_id: str | None = None,
    agent_path: str | Path | None = None,
) -> tuple[DefectArtifact, Path]:
    """离线导出；必须有原始规则失败及同 profile 的无模型重跑 match。"""
    run_file, replay_file = Path(run_path), Path(replay_path)
    run = load_report(run_file)
    replay = load_replay_report(replay_file)
    if run.summary.errored:
        raise DefectInputError("原始报告含执行错误，不能确认为游戏缺陷")
    original = _choose_case(run, case_id)
    checks = _failed_checks(original)
    chosen = next(
        (check for check in checks if check.rule_id == (rule_id or checks[0].rule_id)), None
    )
    if chosen is None or chosen.rule_id not in GAME_STEP_RULES:
        raise DefectInputError("指定规则未在失败步骤中由独立判定器确认")
    _validate_replay(replay, run_file, run.run_id, original)
    agent_ref = None
    if agent_path is not None:
        agent_file = Path(agent_path)
        agent = _load_agent(agent_file, run_file, run, original, chosen.rule_id)
        agent_ref = _source_ref(agent_file, agent.run_id)
    actions = [entry.action for entry in original.executed_actions]
    fingerprint = _fingerprint(run.rules_version, chosen.rule_id, original.seed, actions)
    scenario = Scenario(
        case_id=f"regression-{fingerprint[:12]}",
        description=f"回归验证 {chosen.rule_id}",
        seed=original.seed,
        steps=original.scenario.steps[: len(actions)],
        expected_final_status=None,
        control=None,
    )
    artifact = DefectArtifact(
        fingerprint=fingerprint,
        title=RULE_CATALOG[chosen.rule_id],
        rule_id=chosen.rule_id,
        rules_version=run.rules_version,
        step=chosen.step or 0,
        expected=_safe_text(chosen.expected),
        actual=_safe_text(chosen.actual),
        seed=original.seed,
        actions=actions,
        reproduction=[f"新建 seed={original.seed} 的会话"]
        + [f"第 {index} 步：{action}" for index, action in enumerate(actions, 1)]
        + [f"检查第 {chosen.step} 步的 {chosen.rule_id}"],
        source_run=_source_ref(run_file, run.run_id),
        source_replay=_source_ref(replay_file, replay.run_id),
        source_agent=agent_ref,
        regression=scenario,
    )
    target = Path(output_dir) / f"defect-{fingerprint[:16]}-{new_run_id()}.json"
    serialized = artifact.model_dump_json(indent=2)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as output:
            output.write(serialized)
    except OSError as exc:
        raise ReportWriteError(f"无法写入缺陷单：{type(exc).__name__}") from exc
    return artifact, target


def load_artifact(path: str | Path) -> DefectArtifact:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DefectInputError(f"缺陷单无法读取：{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "1.0":
        raise DefectInputError("缺陷单版本不受支持")
    try:
        artifact = DefectArtifact.model_validate(raw)
        require_complete_fields(raw, artifact.model_dump(mode="json"))
    except (ValidationError, ReplayInputError) as exc:
        raise DefectInputError("缺陷单缺少字段或结构不合法") from exc
    scenario = artifact.regression
    if (
        artifact.rules_version != RULES_VERSION
        or artifact.rule_id not in GAME_STEP_RULES
        or artifact.fingerprint
        != _fingerprint(artifact.rules_version, artifact.rule_id, artifact.seed, artifact.actions)
        or scenario.case_id != f"regression-{artifact.fingerprint[:12]}"
        or scenario.seed != artifact.seed
        or [step.action for step in scenario.steps] != artifact.actions
        or scenario.expected_final_status is not None
        or scenario.control is not None
        or artifact.step != len(artifact.actions)
        or not artifact.actions
    ):
        raise DefectInputError("缺陷单的规则、指纹或回归动作不一致")
    for source in (artifact.source_run, artifact.source_replay, artifact.source_agent):
        if source is not None and (not Path(source.path).is_absolute() or "://" in source.path):
            raise DefectInputError("缺陷单包含非法的证据引用路径")
    return artifact


async def verify_defect(
    artifact: DefectArtifact, client: GameClient, output_dir: str | Path
) -> tuple[int, Path, CaseReport, str]:
    """在调用方指定的目标服务上执行回归；2 始终主导执行/写入错误。"""
    case = await run_case(client, artifact.regression)
    report = single_case_report(case, client, new_run_id(), "defect-regression")
    path = write_report(report, output_dir)
    code, outcome = classify_verification(artifact, case)
    return code, path, case, outcome


def classify_verification(artifact: DefectArtifact, case: CaseReport) -> tuple[int, str]:
    """复用同一结果分类，避免在线执行与离线读取得出不同结论。"""
    if case.status == "error":
        return 2, "execution_error"
    target_checks = [
        check
        for step in case.steps
        for check in step.checks
        if step.index == artifact.step and check.rule_id == artifact.rule_id
    ]
    if case.status == "pass":
        return (0, "passed") if target_checks else (1, "target_not_exercised")
    target_hit = any(check.status == "fail" for check in target_checks)
    return 1, "target_reproduced" if target_hit else "other_rule_failure"
