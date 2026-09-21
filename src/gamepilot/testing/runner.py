"""顺序执行器：按场景逐步执行动作、判定规则并产出可重跑的证据报告。

执行约定：

- 每个场景在新会话中执行；动作严格按场景顺序，一次一个；
- 每个场景最多 `MAX_ACTION_ATTEMPTS` 次动作尝试，**被拒绝的动作也计数**，
  达到上限记为 error 而不是继续跑；
- 首个 fail/error 立即停止当前场景，其余场景独立继续；
- 预期的 409 之后必须再用 GET 验证状态与完整事件未变，
  GET 失败记为 error，绝不推断「没变」；
- 执行器本身不做任何重试。

单步执行、409 后核对与规则判定都在 `execution.CaseExecution` 里——那是与
Agent 共用的增量能力，本模块只负责「把整条计划一次跑完」和套件编排。

报告写入 `<output-dir>/<run_id>.json`，文件名只由生成的 run_id 决定，
已存在的文件不会被覆盖。
"""

import time

from .client import GameClient
from .execution import CaseExecution, CaseStop
from .models import CaseReport, RunReport, RunSummary, Scenario
from .reporting import (
    ReportWriteError,
    current_environment,
    new_run_id,
    report_path,
    single_case_report,
    utc_now_iso,
    write_report,
)
from .rules import RULES_SOURCE, RULES_VERSION, SCHEMA_VERSION
from .scenarios import get_suite

__all__ = [
    "ReportWriteError",
    "current_environment",
    "new_run_id",
    "report_path",
    "run_case",
    "run_suite",
    "single_case_report",
    "utc_now_iso",
    "write_report",
]


async def run_case(
    client: GameClient, scenario: Scenario, *, replay_of: CaseReport | None = None
) -> CaseReport:
    """在一个新会话中执行一个场景，返回完整证据。"""
    execution = CaseExecution(client, scenario, replay_of=replay_of)
    try:
        await execution.begin()
        for plan in execution.planned_steps:
            await execution.execute(plan)
    except CaseStop:
        # 首个 fail/error 已完整落在证据里，收尾阶段只负责封报告。
        pass
    return await execution.finalize()


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


# `single_case_report` 的实现在 `reporting` 里（那个模块不含场景数据，
# Agent 才能沿导入链复用它而不读到脚本答案），这里只做一次再导出：
# 既有的 `from .runner import single_case_report` 与 `__all__` 都不受影响。
# 它由上面的 `from .reporting import ... single_case_report ...` 提供。
