"""重跑校验：用报告里记录的场景在新会话中重新执行，并逐字段比对。

约定：

- 目标地址只来自本次命令行参数；报告里的地址仅作展示，不会被采用，
  报告中的任何内容也不会被当作命令执行；
- 只排除明确的非确定字段（session_id、run_id、时间与耗时），
  业务字段一个都不忽略——不允许靠忽略字段「获得一致」；
- 报告缺少必需字段、含未知字段、版本不支持或动作边界不合法时明确拒绝；
- 原运行是执行错误（error）时结果未知：可以重新执行检查，
  但不承诺复现原错误，该用例标记为 not_comparable。
"""

import json
import time
from pathlib import Path

from pydantic import ValidationError

from .client import GameClient
from .models import (
    CaseReport,
    ControlReport,
    ExecutedAction,
    HttpObservation,
    ReplayCaseReport,
    ReplayDifference,
    ReplayReport,
    ReplaySummary,
    RuleCheck,
    RunReport,
    SnapshotView,
    StepReport,
)
from .rules import MAX_ACTION_ATTEMPTS, RULES_VERSION, SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS
from .runner import current_environment, new_run_id, run_case, utc_now_iso

# 单条差异的渲染上限：报告要能读，不能把整份快照塞进一行。
_MAX_RENDER = 300
_MAX_DIFFS_PER_FIELD = 5


class ReplayInputError(Exception):
    """报告不可用于重跑（文件、版本、结构或动作边界不合法）。"""


def load_report(path: str | Path) -> RunReport:
    """读取并校验报告；任何不确定的地方都明确拒绝。"""
    report_file = Path(path)
    if not report_file.is_file():
        raise ReplayInputError(f"报告文件不存在：{report_file}")
    try:
        raw = json.loads(report_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayInputError(f"报告无法解析为 JSON：{type(exc).__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        raise ReplayInputError(f"报告 schema_version={version!r} 不受支持或缺失；需重新生成报告")
    try:
        report = RunReport.model_validate(raw)
    except ValidationError as exc:
        raise ReplayInputError(
            f"报告缺少必需字段、含未知字段或字段类型不符（{len(exc.errors())} 处）"
        ) from exc

    _require_complete(raw, report.model_dump(mode="json"))

    if report.mode != "run":
        raise ReplayInputError(f"只能重跑运行报告，当前报告 mode={report.mode!r}")
    if report.rules_version != RULES_VERSION:
        raise ReplayInputError(
            f"报告规则版本 {report.rules_version!r} 与当前判定规则 {RULES_VERSION!r} 不一致，"
            "规则结论无法比较"
        )
    if not report.cases:
        raise ReplayInputError("报告不包含任何场景，无可校验内容")

    for case in report.cases:
        _validate_case_boundary(case)
    counts = [
        sum(case.status == status for case in report.cases) for status in ("pass", "fail", "error")
    ]
    if counts != [report.summary.passed, report.summary.failed, report.summary.errored]:
        raise ReplayInputError("报告汇总与场景结论不一致")
    return report


def _require_complete(raw: object, parsed: object, path: str = "report") -> None:
    """运行时模型可有便利默认值；读取证据时每个字段都必须实际存在。"""
    if isinstance(parsed, dict) and isinstance(raw, dict):
        if set(parsed) != set(raw):
            raise ReplayInputError(f"报告 {path} 缺少必需字段或含未知字段")
        for key in parsed:
            _require_complete(raw[key], parsed[key], f"{path}.{key}")
    elif isinstance(parsed, list) and isinstance(raw, list):
        for index, (left, right) in enumerate(zip(raw, parsed, strict=True)):
            _require_complete(left, right, f"{path}[{index}]")


def _validate_request(observation: HttpObservation | None, method: str, path: str) -> None:
    if observation is not None and (observation.method != method or observation.path != path):
        raise ReplayInputError("报告中的请求路径或方法与本会话不一致")


def _validate_case_boundary(case: CaseReport) -> None:
    """校验动作边界：超出上限或证据不自洽的报告不能重跑。"""
    attempts = len(case.executed_actions)
    if attempts > MAX_ACTION_ATTEMPTS:
        raise ReplayInputError(
            f"场景 {case.case_id} 记录的动作尝试数为 {attempts}，超过当前上限 {MAX_ACTION_ATTEMPTS}"
        )
    if len(case.scenario.steps) > MAX_ACTION_ATTEMPTS:
        raise ReplayInputError(
            f"场景 {case.case_id} 的计划动作数为 {len(case.scenario.steps)}，"
            f"超过当前上限 {MAX_ACTION_ATTEMPTS}"
        )
    if len(case.steps) != attempts:
        raise ReplayInputError(
            f"场景 {case.case_id} 的已执行动作数 {attempts} 与步骤证据数 {len(case.steps)} 不一致"
        )
    if (case.case_id, case.description, case.seed) != (
        case.scenario.case_id,
        case.scenario.description,
        case.scenario.seed,
    ):
        raise ReplayInputError("报告场景标识、说明或 seed 与计划不一致")
    if case.planned_actions != [step.action for step in case.scenario.steps]:
        raise ReplayInputError("报告计划动作与场景定义不一致")
    if attempts > len(case.scenario.steps):
        raise ReplayInputError("报告已执行动作不是计划前缀")
    if case.actions_completed and attempts != len(case.scenario.steps):
        raise ReplayInputError("报告声称动作完成但实际轨迹不完整")
    base = "/api/v1/game-sessions"
    if case.create_observation is None:
        raise ReplayInputError("报告缺少创建观测")
    if case.status == "pass" and (
        not case.actions_completed
        or case.initial_snapshot is None
        or case.final_snapshot is None
        or case.initial_get_observation is None
    ):
        raise ReplayInputError("报告通过结论缺少完整执行证据")
    _validate_request(case.create_observation, "POST", base)
    if case.initial_snapshot is not None:
        if case.session_id != case.initial_snapshot.session_id:
            raise ReplayInputError("报告初始快照与会话标识不一致")
        if (
            case.create_observation is None
            or case.create_observation.body is None
            or case.create_observation.body.get("session_id") != case.session_id
        ):
            raise ReplayInputError("报告创建响应与会话标识不一致")
    _validate_request(case.initial_get_observation, "GET", f"{base}/{case.session_id}")
    for index, (action, step, plan) in enumerate(
        zip(case.executed_actions, case.steps, case.scenario.steps), start=1
    ):
        if (
            action.index != index
            or step.index != index
            or action.action != plan.action
            or step.action != plan.action
            or step.expected_status != plan.expected_status
            or step.expected_code != plan.expected_code
        ):
            raise ReplayInputError("报告动作、索引或预期与已执行前缀不一致")
        if (
            action.status_code != step.observed_status
            or action.error_code != step.observed_code
            or action.status_code != step.observation.status_code
            or action.error_code != step.observation.error_code
        ):
            raise ReplayInputError("报告动作状态码或错误码与观测不一致")
        _validate_request(step.observation, "POST", f"{base}/{case.session_id}/actions")
        _validate_request(step.verification_observation, "GET", f"{base}/{case.session_id}")
    spec = case.scenario.control
    if spec is not None and len(spec.actions) > MAX_ACTION_ATTEMPTS:
        raise ReplayInputError("报告场景的对照动作超过上限")
    control = case.control
    if control is not None:
        if spec is None or control.actions != spec.actions or not case.actions_completed:
            raise ReplayInputError("报告对照运行与场景计划不一致")
        count = len(control.executed_actions)
        if not (
            count == len(control.observations) == len(control.status_codes)
            and count <= len(spec.actions)
            and count <= MAX_ACTION_ATTEMPTS
        ):
            raise ReplayInputError("报告对照运行的动作与证据数量不一致")
        if control.completed and count != len(spec.actions):
            raise ReplayInputError("报告对照运行未执行完整计划")
        if control.create_observation is None:
            raise ReplayInputError("报告对照运行缺少创建观测")
        if control.session_id is not None and (
            control.create_observation.body is None
            or control.create_observation.body.get("session_id") != control.session_id
        ):
            raise ReplayInputError("报告对照运行创建响应与会话标识不一致")
        _validate_request(control.create_observation, "POST", base)
        for index, (action, observation) in enumerate(
            zip(control.executed_actions, control.observations), start=1
        ):
            if (
                action.index != index
                or action.action != spec.actions[index - 1]
                or action.status_code != observation.status_code
                or action.error_code != observation.error_code
                or action.status_code != control.status_codes[index - 1]
            ):
                raise ReplayInputError("报告对照运行动作不是已记录前缀")
            _validate_request(observation, "POST", f"{base}/{control.session_id}/actions")


def _render(value: object) -> str:
    if isinstance(value, str):
        return value
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= _MAX_RENDER else text[: _MAX_RENDER - 3] + "..."


def _diff(
    step: int | None, field: str, recorded: object, replayed: object
) -> ReplayDifference | None:
    if recorded == replayed:
        return None
    return ReplayDifference(
        step=step, field=field, recorded=_render(recorded), replayed=_render(replayed)
    )


def _snapshot_diff(
    step: int | None, field: str, recorded: SnapshotView | None, replayed: SnapshotView | None
) -> ReplayDifference | None:
    if (recorded is None) != (replayed is None):
        return ReplayDifference(
            step=step,
            field=field,
            recorded="<none>" if recorded is None else "snapshot",
            replayed="<none>" if replayed is None else "snapshot",
        )
    if recorded is None or replayed is None:
        return None
    return _diff(step, field, recorded.model_dump(), replayed.model_dump())


def _canonical_case(case: CaseReport) -> CaseReport:
    """仅规范化已确认属于本次会话的 ID，保留错配 ID 和其他业务字段。"""
    result = case.model_copy(deep=True)

    def snapshot(value: SnapshotView | None, session_id: str | None) -> None:
        if value is not None and session_id is not None and value.session_id == session_id:
            value.session_id = "{session_id}"

    def observation(value: HttpObservation | None, session_id: str | None) -> None:
        if value is None or session_id is None:
            return
        base = f"/api/v1/game-sessions/{session_id}"
        if value.path in (base, base + "/actions"):
            value.path = value.path.replace(base, "/api/v1/game-sessions/{session_id}", 1)
        if value.body is not None and value.body.get("session_id") == session_id:
            value.body["session_id"] = "{session_id}"

    sid = result.session_id
    snapshot(result.initial_snapshot, sid)
    snapshot(result.final_snapshot, sid)
    observation(result.create_observation, sid)
    observation(result.initial_get_observation, sid)
    for step in result.steps:
        snapshot(step.before, sid)
        snapshot(step.after, sid)
        observation(step.observation, sid)
        observation(step.verification_observation, sid)
    if result.control is not None:
        control = result.control
        snapshot(control.final_snapshot, control.session_id)
        observation(control.create_observation, control.session_id)
        for item in control.observations:
            observation(item, control.session_id)
    return result


def _observation_diff(
    step: int | None, field: str, recorded: HttpObservation, replayed: HttpObservation
) -> list[ReplayDifference]:
    """比对观测；elapsed_ms 属于明确的非确定字段，不参与比较。"""
    diffs = []
    for attribute in ("method", "status_code", "error_kind", "error_detail"):
        difference = _diff(
            step, f"{field}.{attribute}", getattr(recorded, attribute), getattr(replayed, attribute)
        )
        if difference is not None:
            diffs.append(difference)
    difference = _diff(step, f"{field}.path", recorded.path, replayed.path)
    if difference is not None:
        diffs.append(difference)
    difference = _diff(step, f"{field}.body", recorded.body, replayed.body)
    if difference is not None:
        diffs.append(difference)
    return diffs


def _executed_diff(
    step: int, field: str, recorded: ExecutedAction, replayed: ExecutedAction
) -> list[ReplayDifference]:
    diffs = []
    for attribute in ("index", "action", "status_code", "error_code"):
        difference = _diff(
            step, f"{field}.{attribute}", getattr(recorded, attribute), getattr(replayed, attribute)
        )
        if difference is not None:
            diffs.append(difference)
    return diffs


def _checks_diff(
    step: int | None, field: str, recorded: list[RuleCheck], replayed: list[RuleCheck]
) -> list[ReplayDifference]:
    """比对规则结论：rule_id、结论、期望、实际与步骤位置。"""
    diffs: list[ReplayDifference] = []
    if len(recorded) != len(replayed):
        diffs.append(
            ReplayDifference(
                step=step,
                field=f"{field}.count",
                recorded=str(len(recorded)),
                replayed=str(len(replayed)),
            )
        )
    for index, (left, right) in enumerate(zip(recorded, replayed)):
        left_key = (left.rule_id, left.status, left.expected, left.actual, left.step)
        right_key = (right.rule_id, right.status, right.expected, right.actual, right.step)
        if left_key != right_key:
            diffs.append(
                ReplayDifference(
                    step=left.step,
                    field=f"{field}[{index}]",
                    recorded=_render(left_key),
                    replayed=_render(right_key),
                )
            )
            if len(diffs) >= _MAX_DIFFS_PER_FIELD:
                break
    return diffs


def _step_diff(recorded: StepReport, replayed: StepReport) -> list[ReplayDifference]:
    diffs: list[ReplayDifference] = []
    step = recorded.index
    for attribute in (
        "index",
        "action",
        "expected_status",
        "expected_code",
        "observed_status",
        "observed_code",
        "status",
        "error",
    ):
        difference = _diff(
            step,
            f"steps[{step}].{attribute}",
            getattr(recorded, attribute),
            getattr(replayed, attribute),
        )
        if difference is not None:
            diffs.append(difference)
    for field, left, right in (
        ("steps[%d].before" % step, recorded.before, replayed.before),
        ("steps[%d].after" % step, recorded.after, replayed.after),
    ):
        difference = _snapshot_diff(step, field, left, right)
        if difference is not None:
            diffs.append(difference)
    difference = _diff(step, f"steps[{step}].new_events", recorded.new_events, replayed.new_events)
    if difference is not None:
        diffs.append(difference)
    diffs.extend(
        _observation_diff(
            step, f"steps[{step}].observation", recorded.observation, replayed.observation
        )
    )
    left, right = recorded.verification_observation, replayed.verification_observation
    if left is not None and right is not None:
        diffs.extend(_observation_diff(step, "verification_observation", left, right))
    else:
        difference = _diff(step, "verification_observation", left is None, right is None)
        if difference is not None:
            diffs.append(difference)
    diffs.extend(_checks_diff(step, f"steps[{step}].checks", recorded.checks, replayed.checks))
    return diffs


def _control_diff(
    recorded: ControlReport | None, replayed: ControlReport | None
) -> list[ReplayDifference]:
    difference = _diff(None, "control", recorded is not None, replayed is not None)
    if difference is not None:
        return [difference]
    if recorded is None or replayed is None:
        return []
    diffs: list[ReplayDifference] = []
    for attribute in ("description", "actions", "status_codes", "completed"):
        item = _diff(
            None, f"control.{attribute}", getattr(recorded, attribute), getattr(replayed, attribute)
        )
        if item is not None:
            diffs.append(item)
    snapshot = _snapshot_diff(
        None, "control.final_snapshot", recorded.final_snapshot, replayed.final_snapshot
    )
    if snapshot is not None:
        diffs.append(snapshot)
    if recorded.create_observation is not None and replayed.create_observation is not None:
        diffs.extend(
            _observation_diff(
                None, "control.create", recorded.create_observation, replayed.create_observation
            )
        )
    difference = _diff(
        None, "control.observations.count", len(recorded.observations), len(replayed.observations)
    )
    if difference is not None:
        diffs.append(difference)
    for left, right in zip(recorded.observations, replayed.observations):
        diffs.extend(_observation_diff(None, "control.observation", left, right))
    for left, right in zip(recorded.executed_actions, replayed.executed_actions):
        diffs.extend(_executed_diff(left.index, "control.executed_action", left, right))
    diffs.extend(_checks_diff(None, "control.checks", recorded.checks, replayed.checks))
    return diffs


def compare_case(recorded: CaseReport, replayed: CaseReport) -> list[ReplayDifference]:
    """逐字段比对两次运行；只排除会话 id、时间与耗时。"""
    recorded, replayed = _canonical_case(recorded), _canonical_case(replayed)
    diffs: list[ReplayDifference] = []
    for field, left, right in (
        ("status", recorded.status, replayed.status),
        ("actions_completed", recorded.actions_completed, replayed.actions_completed),
        ("case_id", recorded.case_id, replayed.case_id),
        ("description", recorded.description, replayed.description),
        ("seed", recorded.seed, replayed.seed),
        ("scenario", recorded.scenario.model_dump(), replayed.scenario.model_dump()),
        ("planned_actions", recorded.planned_actions, replayed.planned_actions),
        ("error", recorded.error, replayed.error),
        (
            "failure",
            recorded.failure.model_dump() if recorded.failure else None,
            replayed.failure.model_dump() if replayed.failure else None,
        ),
    ):
        difference = _diff(None, field, left, right)
        if difference is not None:
            diffs.append(difference)
    for field, left_snapshot, right_snapshot in (
        ("initial_snapshot", recorded.initial_snapshot, replayed.initial_snapshot),
        ("final_snapshot", recorded.final_snapshot, replayed.final_snapshot),
    ):
        difference = _snapshot_diff(None, field, left_snapshot, right_snapshot)
        if difference is not None:
            diffs.append(difference)
    if (recorded.create_observation is None) != (replayed.create_observation is None):
        diffs.append(
            ReplayDifference(
                field="create_observation",
                recorded="<none>" if recorded.create_observation is None else "observation",
                replayed="<none>" if replayed.create_observation is None else "observation",
            )
        )
    elif recorded.create_observation is not None and replayed.create_observation is not None:
        diffs.extend(
            _observation_diff(
                None, "create_observation", recorded.create_observation, replayed.create_observation
            )
        )
    left_get, right_get = recorded.initial_get_observation, replayed.initial_get_observation
    if left_get is not None and right_get is not None:
        diffs.extend(_observation_diff(None, "initial_get", left_get, right_get))
    elif (left_get is None) != (right_get is None):
        diffs.append(
            ReplayDifference(
                field="initial_get", recorded=str(left_get is None), replayed=str(right_get is None)
            )
        )
    if len(recorded.executed_actions) != len(replayed.executed_actions):
        diffs.append(
            ReplayDifference(
                field="executed_actions.count",
                recorded=str(len(recorded.executed_actions)),
                replayed=str(len(replayed.executed_actions)),
            )
        )
    for left_action, right_action in zip(recorded.executed_actions, replayed.executed_actions):
        diffs.extend(
            _executed_diff(
                left_action.index,
                f"executed_actions[{left_action.index}]",
                left_action,
                right_action,
            )
        )
    if len(recorded.steps) != len(replayed.steps):
        diffs.append(
            ReplayDifference(
                field="steps.count",
                recorded=str(len(recorded.steps)),
                replayed=str(len(replayed.steps)),
            )
        )
    for left_step, right_step in zip(recorded.steps, replayed.steps):
        diffs.extend(_step_diff(left_step, right_step))
    diffs.extend(_checks_diff(None, "checks", recorded.checks, replayed.checks))
    diffs.extend(_control_diff(recorded.control, replayed.control))
    return diffs


async def replay_report(
    client: GameClient, report: RunReport, *, source_report: str
) -> ReplayReport:
    """在新会话中重跑报告中的每个场景，返回比对结果。"""
    started_at = utc_now_iso()
    started = time.perf_counter()
    cases: list[ReplayCaseReport] = []

    # 即便由 Python 直接调用，也必须先验证全部轨迹，不能发完请求才发现坏报告。
    for recorded in report.cases:
        _validate_case_boundary(recorded)
    for recorded in report.cases:
        replayed = await run_case(client, recorded.scenario, replay_of=recorded)
        if recorded.status == "error":
            cases.append(
                ReplayCaseReport(
                    case_id=recorded.case_id,
                    outcome="not_comparable",
                    note="原运行是执行错误，结果未知；已重新执行检查，但不承诺复现原错误",
                    session_id=replayed.session_id,
                    replayed_actions=replayed.executed_actions,
                    execution=replayed,
                )
            )
            continue
        if replayed.status == "error":
            cases.append(
                ReplayCaseReport(
                    case_id=recorded.case_id,
                    outcome="not_comparable",
                    note=f"重跑时出现执行错误：{replayed.error}",
                    session_id=replayed.session_id,
                    replayed_actions=replayed.executed_actions,
                    execution=replayed,
                )
            )
            continue
        differences = compare_case(recorded, replayed)
        cases.append(
            ReplayCaseReport(
                case_id=recorded.case_id,
                outcome="match" if not differences else "mismatch",
                note="与原运行逐字段一致" if not differences else "与原运行存在差异",
                session_id=replayed.session_id,
                replayed_actions=replayed.executed_actions,
                execution=replayed,
                differences=differences,
            )
        )

    finished_at = utc_now_iso()
    return ReplayReport(
        schema_version=SCHEMA_VERSION,
        rules_version=RULES_VERSION,
        mode="replay",
        run_id=new_run_id(),
        source_run_id=report.run_id,
        source_report=source_report,
        base_url=client.base_url,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        timeout_seconds=client.timeout,
        environment=current_environment(),
        summary=ReplaySummary(
            matched=sum(1 for case in cases if case.outcome == "match"),
            mismatched=sum(1 for case in cases if case.outcome == "mismatch"),
            not_comparable=sum(1 for case in cases if case.outcome == "not_comparable"),
        ),
        cases=cases,
    )
