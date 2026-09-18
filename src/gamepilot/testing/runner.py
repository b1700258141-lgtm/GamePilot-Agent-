"""顺序执行器：按场景逐步执行动作、判定规则并产出可重跑的证据报告。

执行约定：

- 每个场景在新会话中执行；动作严格按场景顺序，一次一个；
- 每个场景最多 `MAX_ACTION_ATTEMPTS` 次动作尝试，**被拒绝的动作也计数**，
  达到上限记为 error 而不是继续跑；
- 首个 fail/error 立即停止当前场景，其余场景独立继续；
- 预期的 409 之后必须再用 GET 验证状态与完整事件未变，
  GET 失败记为 error，绝不推断「没变」；
- 执行器本身不做任何重试。

报告写入 `<output-dir>/<run_id>.json`，文件名只由生成的 run_id 决定，
已存在的文件不会被覆盖。
"""

import platform
import re
import secrets
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import ValidationError

from .client import GameClient
from .models import (
    ActionStep,
    CaseFailure,
    CaseReport,
    CaseStatus,
    ControlReport,
    ExecutedAction,
    HttpObservation,
    ReplayReport,
    RuleCheck,
    RunEnvironment,
    RunReport,
    RunSummary,
    Scenario,
    SnapshotView,
    StepReport,
)
from .oracle import (
    check_control,
    check_create,
    check_final_status,
    check_get,
    check_initial_state,
    check_rejected_unchanged,
    check_seed,
    check_step,
)
from .rules import (
    MAX_ACTION_ATTEMPTS,
    R_STEP_LIMIT,
    RULES_SOURCE,
    RULES_VERSION,
    SCHEMA_VERSION,
)
from .scenarios import get_suite

# run_id 会被用作文件名：只允许时间戳与短随机后缀，外部文本永远不进路径。
RUN_ID_PATTERN = re.compile(r"^[0-9A-Za-z]{8,32}-[0-9a-f]{6}$")


class ReportWriteError(Exception):
    """报告写入失败（例如目标文件已存在）。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def report_path(output_dir: str | Path, run_id: str) -> Path:
    if not RUN_ID_PATTERN.match(run_id):
        raise ReportWriteError(f"非法的 run_id，拒绝用作文件名：{run_id!r}")
    return Path(output_dir) / f"{run_id}.json"


def write_report(report: RunReport | ReplayReport, output_dir: str | Path) -> Path:
    """写出报告；目标文件已存在时直接失败，不覆盖已有证据。"""
    path = report_path(output_dir, report.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 排他创建：检查与写入之间也不能覆盖另一份证据。
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入报告 {path}：{type(exc).__name__}") from exc
    return path


def current_environment() -> RunEnvironment:
    try:
        gamepilot_version = version("gamepilot")
    except PackageNotFoundError:  # 源码目录直接运行时可能没有元数据
        gamepilot_version = "unknown"
    return RunEnvironment(
        python_version=platform.python_version(),
        platform=platform.platform(),
        gamepilot_version=gamepilot_version,
    )


class _ExecutionStop(Exception):
    """首个失败或执行错误：停止当前场景并保留已有证据。"""

    def __init__(
        self, status: CaseStatus, message: str, rule_id: str | None = None, step: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.rule_id = rule_id
        self.step = step


class _CaseEvidence:
    """收集一个场景的证据，并在首个非 pass 的规则检查处停下。"""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.checks: list[RuleCheck] = []
        self.steps: list[StepReport] = []
        self.executed: list[ExecutedAction] = []
        self.session_id: str | None = None
        self.create_observation: HttpObservation | None = None
        self.initial_get_observation: HttpObservation | None = None
        self.actions_completed = False
        self.initial_snapshot: SnapshotView | None = None
        self.final_snapshot: SnapshotView | None = None
        self.control: ControlReport | None = None

    def require(self, checks: list[RuleCheck]) -> None:
        """记录检查结果；出现 fail/error 时抛出，停止本场景。"""
        self.checks.extend(checks)
        for check in checks:
            if check.status != "pass":
                raise _ExecutionStop(
                    "fail" if check.status == "fail" else "error",
                    check.message or check.rule_id,
                    check.rule_id,
                    check.step,
                )


def _parse_snapshot(observation: HttpObservation) -> SnapshotView:
    """从观测中解析快照；不符合结构时按执行错误处理。"""
    if observation.body is None:
        raise _ExecutionStop("error", "响应体不是可解析的 JSON 对象")
    try:
        return SnapshotView.model_validate(observation.body)
    except ValidationError as exc:
        raise _ExecutionStop(
            "error", f"响应不符合会话快照结构（{len(exc.errors())} 处校验失败）"
        ) from exc


async def run_case(
    client: GameClient, scenario: Scenario, *, replay_of: CaseReport | None = None
) -> CaseReport:
    """在一个新会话中执行一个场景，返回完整证据。"""
    started_at = utc_now_iso()
    started = time.perf_counter()
    evidence = _CaseEvidence(scenario)
    failure: CaseFailure | None = None
    error_message: str | None = None

    try:
        await _execute_case(client, scenario, evidence, replay_of=replay_of)
    except _ExecutionStop as stop:
        if stop.status == "fail":
            failure = CaseFailure(
                kind="fail", rule_id=stop.rule_id, step=stop.step, message=stop.message
            )
        else:
            error_message = stop.message

    finished_at = utc_now_iso()
    if failure is not None:
        status: CaseStatus = "fail"
    elif error_message is not None:
        status = "error"
    else:
        status = "pass"
    return CaseReport(
        case_id=scenario.case_id,
        description=scenario.description,
        seed=scenario.seed,
        status=status,
        session_id=evidence.session_id,
        create_observation=evidence.create_observation,
        initial_get_observation=evidence.initial_get_observation,
        actions_completed=evidence.actions_completed,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        planned_actions=[step.action for step in scenario.steps],
        executed_actions=evidence.executed,
        scenario=scenario,
        initial_snapshot=evidence.initial_snapshot,
        final_snapshot=evidence.final_snapshot,
        steps=evidence.steps,
        checks=evidence.checks,
        control=evidence.control,
        failure=failure,
        error=error_message,
    )


def _ensure_available(observation: HttpObservation, phase: str, step: int | None = None) -> None:
    if observation.is_error:
        raise _ExecutionStop(
            "error", f"{phase}：{observation.error_kind}（{observation.error_detail}）", step=step
        )


def _step_limit(index: int) -> list[RuleCheck]:
    return [
        RuleCheck(
            rule_id=R_STEP_LIMIT,
            status="error",
            expected=f"动作尝试数 <= {MAX_ACTION_ATTEMPTS}",
            actual=str(index),
            message=f"达到动作尝试上限（{MAX_ACTION_ATTEMPTS}）",
            step=index,
        )
    ]


async def _execute_case(
    client: GameClient,
    scenario: Scenario,
    evidence: _CaseEvidence,
    *,
    replay_of: CaseReport | None = None,
) -> None:
    created_observation = await client.create_session(scenario.seed)
    evidence.create_observation = created_observation
    _ensure_available(created_observation, "创建会话失败")
    evidence.require(check_create(created_observation))
    created = _parse_snapshot(created_observation)
    evidence.session_id = created.session_id
    evidence.initial_snapshot = created
    evidence.final_snapshot = created
    evidence.require(check_seed(created, scenario.seed))

    fetched_observation = await client.get_session(created.session_id)
    evidence.initial_get_observation = fetched_observation
    _ensure_available(fetched_observation, "查询会话失败")
    evidence.require(check_get(fetched_observation))
    fetched = _parse_snapshot(fetched_observation)
    evidence.final_snapshot = fetched
    evidence.require(check_initial_state(created, fetched))
    current = fetched

    # 原计划作为元数据保留；replay 仅执行已发生的前缀，不追加计划尾部。
    steps = (
        scenario.steps if replay_of is None else scenario.steps[: len(replay_of.executed_actions)]
    )
    accepted_status_codes: list[int | None] = []
    for index, plan in enumerate(steps, start=1):
        if index > MAX_ACTION_ATTEMPTS:
            evidence.require(_step_limit(index))
        observation = await client.perform_action(created.session_id, plan.action)
        evidence.executed.append(
            ExecutedAction(
                index=index,
                action=plan.action,
                status_code=observation.status_code,
                error_code=observation.error_code,
                elapsed_ms=observation.elapsed_ms,
            )
        )
        # 在解析与核对 GET 之前落下步骤骨架，任意失败都保留本次动作。
        report = StepReport(
            index=index,
            action=plan.action,
            expected_status=plan.expected_status,
            expected_code=plan.expected_code,
            observed_status=observation.status_code,
            observed_code=observation.error_code,
            status="error",
            before=current,
            observation=observation,
        )
        evidence.steps.append(report)
        try:
            _ensure_available(observation, f"第 {index} 步动作执行失败", index)
            report.checks = check_step(index, plan, current, observation, None)
            evidence.require(report.checks)
            if observation.status_code == 200:
                after = _parse_snapshot(observation)
                report.after = after
                report.new_events = after.events[len(current.events) :]
                evidence.final_snapshot = after
                checks = check_step(index, plan, current, observation, after)
                # 接口预期已校验过，只添加新产生的游戏规则判定。
                checks = checks[len(report.checks) :]
                report.checks.extend(checks)
                evidence.require(checks)
                accepted_status_codes.append(observation.status_code)
            else:
                verification = await client.get_session(created.session_id)
                report.verification_observation = verification
                _ensure_available(verification, f"第 {index} 步核对 GET 失败", index)
                checks = check_get(verification, index)
                report.checks.extend(checks)
                evidence.require(checks)
                after = _parse_snapshot(verification)
                report.after = after
                evidence.final_snapshot = after
                checks = check_rejected_unchanged(index, current, after)
                report.checks.extend(checks)
                evidence.require(checks)
            report.status = "pass"
            current = after
        except _ExecutionStop as stop:
            report.status = stop.status
            if stop.status == "error":
                report.error = stop.message
            raise

    # 在早停轨迹的重跑中，不运行原本未到达的终态判定或对照会话。
    evidence.actions_completed = replay_of is None or replay_of.actions_completed
    if not evidence.actions_completed:
        return
    if scenario.expected_final_status is not None:
        evidence.require(
            check_final_status(len(evidence.steps) or None, current, scenario.expected_final_status)
        )
    if scenario.control is not None and (replay_of is None or replay_of.control is not None):
        await _run_control(
            client,
            scenario,
            accepted_status_codes,
            current,
            evidence,
            replay_of.control if replay_of else None,
        )


async def _run_control(
    client: GameClient,
    scenario: Scenario,
    accepted_status_codes: list[int | None],
    main_snapshot: SnapshotView,
    evidence: _CaseEvidence,
    recorded: ControlReport | None = None,
) -> None:
    """对照运行同样保留部分轨迹，并受动作上限约束。"""
    assert scenario.control is not None
    spec = scenario.control
    report = ControlReport(
        description=spec.description, actions=list(spec.actions), status_codes=[]
    )
    evidence.control = report

    def require(checks: list[RuleCheck]) -> None:
        report.checks.extend(checks)
        evidence.require(checks)

    observation = await client.create_session(scenario.seed)
    report.create_observation = observation
    _ensure_available(observation, "对照会话创建失败")
    require(check_create(observation))
    current = _parse_snapshot(observation)
    report.session_id = current.session_id
    report.final_snapshot = current
    require(check_seed(current, scenario.seed))
    actions = spec.actions if recorded is None else spec.actions[: len(recorded.executed_actions)]
    for index, action in enumerate(actions, start=1):
        if index > MAX_ACTION_ATTEMPTS:
            require(_step_limit(index))
        result = await client.perform_action(report.session_id, action)
        report.observations.append(result)
        report.status_codes.append(result.status_code)
        report.executed_actions.append(
            ExecutedAction(
                index=index,
                action=action,
                status_code=result.status_code,
                error_code=result.error_code,
                elapsed_ms=result.elapsed_ms,
            )
        )
        _ensure_available(result, "对照会话动作失败", index)
        plan = ActionStep(action=action)
        require(check_step(index, plan, current, result, None))
        after = _parse_snapshot(result)
        report.final_snapshot = after
        require(check_step(index, plan, current, result, after))
        current = after
    report.completed = recorded is None or recorded.completed
    if report.completed:
        require(check_control(accepted_status_codes, main_snapshot, report.status_codes, current))


async def run_suite(
    client: GameClient, suite_name: str, scenarios: tuple[Scenario, ...] | None = None
) -> RunReport:
    """顺序执行整套场景，返回运行报告。"""
    selected = scenarios if scenarios is not None else get_suite(suite_name)
    started_at = utc_now_iso()
    started = time.perf_counter()
    cases = [await run_case(client, scenario) for scenario in selected]
    finished_at = utc_now_iso()
    return RunReport(
        schema_version=SCHEMA_VERSION,
        rules_version=RULES_VERSION,
        rules_source=RULES_SOURCE,
        mode="run",
        run_id=new_run_id(),
        suite=suite_name,
        base_url=client.base_url,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        timeout_seconds=client.timeout,
        environment=current_environment(),
        summary=RunSummary(
            passed=sum(1 for case in cases if case.status == "pass"),
            failed=sum(1 for case in cases if case.status == "fail"),
            errored=sum(1 for case in cases if case.status == "error"),
        ),
        cases=cases,
    )
