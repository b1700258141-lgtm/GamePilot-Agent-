"""增量执行：把一次会话拆成「开始 → 逐步执行 → 收尾」三步。

脚本执行器（`runner.run_case`）与 Agent 共用这一份实现：

- `begin()`：创建会话、做初始 GET、核对初始状态与 seed；
- `execute(plan)`：执行**一个**计划动作，核对接口预期与游戏规则，
  预期 409 时再用 GET 证明状态未变；
- `finalize()`：收尾并产出可重跑的 `CaseReport`。

两者不再各写一套 HTTP / 409 / 规则检查逻辑——脚本一次跑完整条计划，
Agent 每个动作都由模型现选，但底层执行与判定完全同源。
每个场景的连接、初始校验和逐步校验语义都与原实现逐字段一致。

边界与 `runner` 一致：只通过 HTTP 观测与被测服务交互，不导入 `gamepilot.domain`。
刻意**不导入内置场景数据**（`scenarios`）：Agent 侧不允许沿导入链读到脚本答案，
所以可复用的执行能力必须住在不含场景的模块里。
"""

import time
from collections.abc import Callable

from pydantic import ValidationError

from .client import GameClient
from .models import (
    ActionStep,
    CaseFailure,
    CaseReport,
    CaseStatus,
    ControlReport,
    ControlSpec,
    ExecutedAction,
    HttpObservation,
    RuleCheck,
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
from .reporting import utc_now_iso
from .rules import MAX_ACTION_ATTEMPTS, R_STEP_LIMIT


class CaseStop(Exception):
    """首个失败或执行错误：停止当前场景并保留已有证据。

    `status` 取 `CaseStatus` 的取值：`fail` 表示已有独立规则证据确认游戏违规，
    `error` 表示基础设施或观测结构故障。调用方据此区分「发现缺陷」与「没跑出结论」。
    """

    def __init__(
        self, status: CaseStatus, message: str, rule_id: str | None = None, step: int | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.rule_id = rule_id
        self.step = step


class _Evidence:
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
                raise CaseStop(
                    "fail" if check.status == "fail" else "error",
                    check.message or check.rule_id,
                    check.rule_id,
                    check.step,
                )


def parse_snapshot(observation: HttpObservation) -> SnapshotView:
    """从观测中解析快照；不符合结构时按执行错误处理。"""
    if observation.body is None:
        raise CaseStop("error", "响应体不是可解析的 JSON 对象")
    try:
        return SnapshotView.model_validate(observation.body)
    except ValidationError as exc:
        raise CaseStop(
            "error", f"响应不符合会话快照结构（{len(exc.errors())} 处校验失败）"
        ) from exc


def ensure_available(observation: HttpObservation, phase: str, step: int | None = None) -> None:
    """把 HTTP 层执行错误（连不上、超时、5xx、存储故障）转成 CaseStop。"""
    if observation.is_error:
        raise CaseStop(
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


class CaseExecution:
    """一次会话的增量执行器。

    典型用法：

    ```python
    execution = CaseExecution(client, scenario)
    await execution.begin()
    for plan in execution.planned_steps:
        await execution.execute(plan)
    report = await execution.finalize()
    ```

    `execute` 失败时抛出 `CaseStop`，此时**证据已经落下**：
    失败步骤的观测、规则检查和最后已知快照都留在 `execution` 上，
    调用方随时可以 `finalize()` 取出完整报告，也可以据此决定是否继续。
    """

    def __init__(
        self,
        client: GameClient,
        scenario: Scenario,
        *,
        replay_of: CaseReport | None = None,
        timeout_provider: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._scenario = scenario
        self._replay_of = replay_of
        # 单次请求超时的来源。Agent 侧传入「单次上限与剩余总时限的较小值」，
        # 脚本执行器不传，沿用 `GameClient` 自己的默认超时。
        self._timeout_provider = timeout_provider
        self._evidence = _Evidence(scenario)
        self._started_at = utc_now_iso()
        self._started = time.perf_counter()
        self._current: SnapshotView | None = None
        self._accepted_status_codes: list[int | None] = []
        self._stop: CaseStop | None = None
        # 原计划作为元数据保留；重跑只执行已发生的前缀，不追加计划尾部。
        self._planned: list[ActionStep] = (
            list(scenario.steps)
            if replay_of is None
            else list(scenario.steps[: len(replay_of.executed_actions)])
        )

    def _timeout(self) -> float | None:
        return None if self._timeout_provider is None else self._timeout_provider()

    # --------------------------------------------------------------- 只读视图

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    @property
    def planned_steps(self) -> tuple[ActionStep, ...]:
        """本次要执行的计划前缀（重跑时是原运行真正执行过的部分）。"""
        return tuple(self._planned)

    @property
    def session_id(self) -> str | None:
        return self._evidence.session_id

    @property
    def current_snapshot(self) -> SnapshotView | None:
        """最新一次已核验的快照；会话尚未创建或首步即失败时为 None。"""
        return self._current

    @property
    def initial_snapshot(self) -> SnapshotView | None:
        return self._evidence.initial_snapshot

    @property
    def steps(self) -> tuple[StepReport, ...]:
        return tuple(self._evidence.steps)

    @property
    def executed_actions(self) -> tuple[ExecutedAction, ...]:
        return tuple(self._evidence.executed)

    @property
    def checks(self) -> tuple[RuleCheck, ...]:
        return tuple(self._evidence.checks)

    @property
    def stop(self) -> CaseStop | None:
        """记录到的首个 fail/error；尚未出现时为 None。"""
        return self._stop

    @property
    def actions_completed(self) -> bool:
        return self._evidence.actions_completed

    # ------------------------------------------------------------------ 执行

    def _require(self, checks: list[RuleCheck]) -> None:
        """记录检查结果并在首个非 pass 处停下，同时留下真正的停止原因。

        结论只取自这里捕获到的 stop：按规则顺序扫描检查列表可能在
        「同一步先落 error、后落 fail」时给出与执行不一致的归类。
        """
        try:
            self._evidence.require(checks)
        except CaseStop as stop:
            self._stop = self._stop or stop
            raise

    def interrupt(self, message: str, *, step: int | None = None) -> None:
        """记录由调用方预算等外部约束造成的中断，并保留已有部分证据。"""
        stop = CaseStop("error", message, step=step)
        self._stop = self._stop or stop
        if self._evidence.steps and self._evidence.steps[-1].status == "error":
            self._evidence.steps[-1].error = message

    async def begin(self) -> SnapshotView:
        """创建会话并完成初始校验，返回已核验的初始快照。

        未创建会话就失败时同样抛出 `CaseStop`，但此时没有可重跑的会话，
        调用方不应据此写出一份可重跑的运行报告。
        """
        try:
            created_observation = await self._client.create_session(
                self._scenario.seed, timeout=self._timeout()
            )
            self._evidence.create_observation = created_observation
            ensure_available(created_observation, "创建会话失败")
            self._require(check_create(created_observation))
            created = parse_snapshot(created_observation)
            self._evidence.session_id = created.session_id
            self._evidence.initial_snapshot = created
            self._evidence.final_snapshot = created
            self._require(check_seed(created, self._scenario.seed))

            fetched_observation = await self._client.get_session(
                created.session_id, timeout=self._timeout()
            )
            self._evidence.initial_get_observation = fetched_observation
            ensure_available(fetched_observation, "查询会话失败")
            self._require(check_get(fetched_observation))
            fetched = parse_snapshot(fetched_observation)
            self._evidence.final_snapshot = fetched
            self._require(check_initial_state(created, fetched))
        except CaseStop as stop:
            # `ensure_available` / `parse_snapshot` 直接抛错，没走 `_require`，
            # 所以这里也要留下停止原因，收尾时才能给出正确的 error 文本。
            self._stop = self._stop or stop
            raise
        self._current = fetched
        return fetched

    async def execute(self, plan: ActionStep) -> StepReport:
        """执行一个计划动作并记录证据；失败时抛出 `CaseStop`。

        成功的步骤返回 `StepReport`；被拒绝（预期 409）的步骤同样返回报告，
        其 `verification_observation` 保存用于证明状态未变的 GET 观测。
        """
        if self._current is None or self._evidence.session_id is None:
            raise CaseStop("error", "会话尚未创建，不能执行动作")
        index = len(self._evidence.steps) + 1
        if index > MAX_ACTION_ATTEMPTS:
            self._require(_step_limit(index))

        current = self._current
        session_id = self._evidence.session_id
        observation = await self._client.perform_action(
            session_id, plan.action, timeout=self._timeout()
        )
        self._evidence.executed.append(
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
        self._evidence.steps.append(report)
        try:
            ensure_available(observation, f"第 {index} 步动作执行失败", index)
            report.checks = check_step(index, plan, current, observation, None)
            self._require(report.checks)
            if observation.status_code == 200:
                after = parse_snapshot(observation)
                report.after = after
                report.new_events = after.events[len(current.events) :]
                self._evidence.final_snapshot = after
                checks = check_step(index, plan, current, observation, after)
                # 接口预期已校验过，只添加新产生的游戏规则判定。
                checks = checks[len(report.checks) :]
                report.checks.extend(checks)
                self._require(checks)
                self._accepted_status_codes.append(observation.status_code)
            else:
                verification = await self._client.get_session(session_id, timeout=self._timeout())
                report.verification_observation = verification
                ensure_available(verification, f"第 {index} 步核对 GET 失败", index)
                checks = check_get(verification, index)
                report.checks.extend(checks)
                self._require(checks)
                after = parse_snapshot(verification)
                report.after = after
                self._evidence.final_snapshot = after
                checks = check_rejected_unchanged(index, current, after)
                report.checks.extend(checks)
                self._require(checks)
            report.status = "pass"
            self._current = after
        except CaseStop as stop:
            report.status = stop.status
            if stop.status == "error":
                report.error = stop.message
            self._stop = self._stop or stop
            raise
        return report

    async def finalize(self) -> CaseReport:
        """收尾并产出完整证据。

        正常跑完计划时还会核对场景声明的终态并执行对照会话；
        Agent 生成的场景不声明终态也没有对照，因此可以随时调用，
        包括在早停之后——那时它只负责把已有证据封成报告。
        """
        evidence = self._evidence
        if self._replay_of is None:
            # 计划由调用方逐步提供（Agent 会边选边追加），因此按收尾时刻的长度比对。
            evidence.actions_completed = len(evidence.steps) == len(self._scenario.steps)
        else:
            evidence.actions_completed = self._replay_of.actions_completed

        # 已经早停的运行不再核对终态，也不执行对照会话：
        # 原顺序执行器在首个 fail/error 处直接结束，终态结论在那条路径上从未产生。
        if evidence.actions_completed and self._stop is None:
            await self._finish_terminal_checks()
        return self._build_report()

    async def _finish_terminal_checks(self) -> None:
        """跑完计划后：核对场景声明的终态，并按需执行对照会话。

        这里不再向外抛异常：调用方已经在收尾阶段，结论只体现在报告里。
        终态核对失败时不会继续执行对照会话，与顺序执行器的行为一致。
        """
        scenario = self._scenario
        try:
            if scenario.expected_final_status is not None:
                if self._current is None:
                    raise CaseStop("error", "缺少最终快照，无法核对终态")
                self._require(
                    check_final_status(
                        len(self._evidence.steps) or None,
                        self._current,
                        scenario.expected_final_status,
                    )
                )
            recorded = self._replay_of.control if self._replay_of else None
            if scenario.control is not None and (self._replay_of is None or recorded is not None):
                await self._run_control(scenario.control, recorded)
        except CaseStop as stop:
            self._stop = self._stop or stop

    async def _run_control(self, spec: ControlSpec, recorded: ControlReport | None) -> None:
        """对照运行同样保留部分轨迹，并受动作上限约束。"""
        report = ControlReport(
            description=spec.description, actions=list(spec.actions), status_codes=[]
        )
        self._evidence.control = report

        def require(checks: list[RuleCheck]) -> None:
            report.checks.extend(checks)
            self._require(checks)

        observation = await self._client.create_session(
            self._scenario.seed, timeout=self._timeout()
        )
        report.create_observation = observation
        ensure_available(observation, "对照会话创建失败")
        require(check_create(observation))
        current = parse_snapshot(observation)
        report.session_id = current.session_id
        report.final_snapshot = current
        require(check_seed(current, self._scenario.seed))
        actions = (
            spec.actions if recorded is None else spec.actions[: len(recorded.executed_actions)]
        )
        for index, action in enumerate(actions, start=1):
            if index > MAX_ACTION_ATTEMPTS:
                require(_step_limit(index))
            result = await self._client.perform_action(
                report.session_id, action, timeout=self._timeout()
            )
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
            ensure_available(result, "对照会话动作失败", index)
            plan = ActionStep(action=action)
            require(check_step(index, plan, current, result, None))
            after = parse_snapshot(result)
            report.final_snapshot = after
            require(check_step(index, plan, current, result, after))
            current = after
        report.completed = recorded is None or recorded.completed
        if report.completed:
            assert self._current is not None
            require(
                check_control(
                    self._accepted_status_codes, self._current, report.status_codes, current
                )
            )

    # ------------------------------------------------------------------ 报告

    def _build_report(self) -> CaseReport:
        evidence = self._evidence
        stop = self._stop
        failure: CaseFailure | None = None
        error_message: str | None = None
        # 结论只取自真正抛出过的那个 stop：按规则顺序扫描检查列表可能在
        # 「同一步先有 error 检查、后有 fail 检查」时给出与执行不一致的归类。
        if stop is not None:
            if stop.status == "fail":
                failure = CaseFailure(
                    kind="fail", rule_id=stop.rule_id, step=stop.step, message=stop.message
                )
            else:
                error_message = stop.message
        status: CaseStatus = "fail" if failure is not None else "error" if error_message else "pass"
        return CaseReport(
            case_id=self._scenario.case_id,
            description=self._scenario.description,
            seed=self._scenario.seed,
            status=status,
            session_id=evidence.session_id,
            create_observation=evidence.create_observation,
            initial_get_observation=evidence.initial_get_observation,
            actions_completed=evidence.actions_completed,
            started_at=self._started_at,
            finished_at=utc_now_iso(),
            duration_ms=(time.perf_counter() - self._started) * 1000,
            planned_actions=[step.action for step in self._scenario.steps],
            executed_actions=evidence.executed,
            scenario=self._scenario,
            initial_snapshot=evidence.initial_snapshot,
            final_snapshot=evidence.final_snapshot,
            steps=evidence.steps,
            checks=evidence.checks,
            control=evidence.control,
            failure=failure,
            error=error_message,
        )


__all__ = [
    "CaseExecution",
    "CaseStop",
    "ensure_available",
    "parse_snapshot",
]
