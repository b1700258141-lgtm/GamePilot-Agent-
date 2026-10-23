"""固定四种 seed 的 84 格离线 Agent 预检；替身成绩不作为模型能力结论。"""

import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from gamepilot.agent.models import BudgetSpec
from gamepilot.lab import PROFILE_NORMAL, PROFILES, TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import ActionStep, Scenario
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso
from gamepilot.testing.scenarios import BASELINE_SCENARIOS

from .agent_eval import (
    AGENT_EVAL_BASE_URL,
    _cell_error_stages,
    _run_cell,
    offline_provider_factory,
)
from .agent_manifest import AGENT_COMBINATIONS, AGENT_GOAL_IDS, AgentCombination
from .agent_models import AgentCellEvidence
from .agent_seed_reachability import ReachabilityCase, search_fault_reachability

PREFLIGHT_VERSION = "2.0.0"
PREFLIGHT_SEEDS = (0, 1, 2, 3)
# 连续 seed，不按 Agent 结果筛选。HTTP 独立判定器会再次验证这些对照脚本。
VICTORY_ACTIONS = {seed: ("power_strike", "cast_meteor") for seed in PREFLIGHT_SEEDS}
PREFLIGHT_BUDGET = BudgetSpec(
    max_action_attempts=6,
    max_model_calls=6,
    max_format_retries=1,
    total_timeout_seconds=60,
)
MANIFEST_SOURCE = "docs/BATTLE_RULES_V2.md#缺陷靶场"


class PreflightModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PreflightRow(PreflightModel):
    seed: int
    profile: str
    goal_id: str
    report_root: str
    status: Literal["ok", "deviation", "execution_error", "skipped"]
    error_kind: str | None = None
    evidence: AgentCellEvidence | None = None


class PreflightReport(PreflightModel):
    version: str
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    manifest_source: str
    seeds: list[int]
    profiles: list[str]
    goal_ids: list[str]
    budget: dict[str, object]
    provider_is_test_double: bool
    planned: int
    executed: int
    skipped: int
    execution_errors: int
    deviations: int
    replay_matches: int
    normal_goals_met: int
    normal_goals_planned: int
    normal_false_positives: int
    designated_planned: int
    designated_reachable: int
    designated_unreachable: int
    designated_triggered: int
    designated_detected: int
    reachability: list[ReachabilityCase]
    abort_after: str | None
    exit_code: int
    rows: list[PreflightRow]


def control_scenario(seed: int, combination: AgentCombination) -> Scenario:
    """为固定 seed 生成同参对照；胜利目标的攻击次数不能沿用 seed 42。"""
    if seed not in PREFLIGHT_SEEDS:
        raise ValueError(f"seed {seed} 不属于固定离线预检清单")
    baseline = next(
        item for item in BASELINE_SCENARIOS if item.case_id == combination.script_case_id
    )
    data = baseline.model_dump()
    data["seed"] = seed
    data["description"] = f"固定 Agent 对照：{combination.goal_id}，seed={seed}"
    if combination.goal_id == "victory":
        data["steps"] = [
            ActionStep(action=action).model_dump() for action in VICTORY_ACTIONS[seed]
        ] + [
            ActionStep(
                action="attack", expected_status=409, expected_code="battle_not_active"
            ).model_dump()
        ]
    elif combination.goal_id == "healing" and seed == 3:
        # 骷髅首回合伤害不低于药水效力，满血药水是该 seed 的上限边界对照。
        data["steps"] = [ActionStep(action="use_potion").model_dump()]
    scenario = Scenario.model_validate(data)
    if len(scenario.steps) > PREFLIGHT_BUDGET.max_action_attempts:
        raise ValueError("对照脚本超过 Agent 动作预算")
    return scenario


def _row_status(
    combination: AgentCombination, evidence: AgentCellEvidence, *, reachable: bool | None
) -> str:
    if _cell_error_stages(evidence) or evidence.replay_outcome in (
        "not_comparable",
        "not_executed",
    ):
        return "execution_error"
    if not all(
        (
            evidence.agent_report_path,
            evidence.run_report_path,
            evidence.replay_path,
            evidence.control_report_path,
            evidence.replay_status,
            evidence.control_case_status,
        )
    ):
        return "execution_error"
    if evidence.replay_outcome != "match":
        return "deviation"
    if combination.is_designated:
        if reachable is None:
            return "execution_error"
        if reachable and (
            evidence.control_case_status != "fail"
            or not set(combination.target_rules).intersection(evidence.control_failing_rules)
            or not set(evidence.target_rules_hit).intersection(evidence.control_failing_rules)
        ):
            return "deviation"
        if not reachable and (evidence.control_case_status != "pass" or evidence.triggered):
            return "deviation"
    if combination.profile == PROFILE_NORMAL:
        return (
            "ok"
            if evidence.goal_met
            and evidence.case_status == "pass"
            and evidence.control_case_status == "pass"
            and not evidence.triggered
            else "deviation"
        )
    if evidence.triggered:
        if combination.is_designated and not evidence.detected:
            return "deviation"
        return "ok" if evidence.case_status == "fail" else "deviation"
    if combination.is_designated and reachable:
        return "deviation"
    return "ok" if evidence.case_status == "pass" else "deviation"


def _summarize(
    rows: list[PreflightRow],
    reachability: list[ReachabilityCase],
    *,
    started_at: str,
    started: float,
    run_id: str,
) -> PreflightReport:
    executed = [row for row in rows if row.status != "skipped"]
    evidence = [row.evidence for row in executed if row.evidence is not None]
    planned = len(PREFLIGHT_SEEDS) * len(AGENT_COMBINATIONS)
    errors = sum(row.status == "execution_error" for row in rows)
    deviations = sum(row.status == "deviation" for row in rows)
    normal = [row for row in rows if row.profile == PROFILE_NORMAL and row.evidence is not None]
    designated = [
        row.evidence for row in rows if row.evidence is not None and row.evidence.is_designated
    ]
    exit_code = 2 if errors else 1 if deviations or len(executed) != planned else 0
    aborted = next((row for row in rows if row.status == "execution_error"), None)
    return PreflightReport(
        version=PREFLIGHT_VERSION,
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=(time.perf_counter() - started) * 1000,
        manifest_source=MANIFEST_SOURCE,
        seeds=list(PREFLIGHT_SEEDS),
        profiles=list(PROFILES),
        goal_ids=list(AGENT_GOAL_IDS),
        budget=PREFLIGHT_BUDGET.model_dump(),
        provider_is_test_double=True,
        planned=planned,
        executed=len(executed),
        skipped=planned - len(executed),
        execution_errors=errors,
        deviations=deviations,
        replay_matches=sum(item.replay_outcome == "match" for item in evidence),
        normal_goals_met=sum(row.evidence.goal_met for row in normal if row.evidence),
        normal_goals_planned=len(PREFLIGHT_SEEDS) * len(AGENT_GOAL_IDS),
        normal_false_positives=sum(
            row.evidence.case_status == "fail" for row in normal if row.evidence
        ),
        designated_planned=len(PREFLIGHT_SEEDS)
        * sum(combination.is_designated for combination in AGENT_COMBINATIONS),
        designated_reachable=sum(item.reachable for item in reachability),
        designated_unreachable=sum(not item.reachable for item in reachability),
        designated_triggered=sum(item.triggered for item in designated),
        designated_detected=sum(item.detected for item in designated),
        reachability=reachability,
        abort_after=(
            f"seed={aborted.seed} {aborted.profile} × {aborted.goal_id}" if aborted else None
        ),
        exit_code=exit_code,
        rows=rows,
    )


async def run_agent_seed_preflight(output_dir: str | Path) -> tuple[PreflightReport, Path]:
    """只用离线观测驱动替身跑固定 84 格；绝不提供真实模型入口。"""
    started_at = utc_now_iso()
    started = time.perf_counter()
    run_id = new_run_id()
    root = Path(output_dir) / run_id
    planned = [
        (seed, combination) for seed in PREFLIGHT_SEEDS for combination in AGENT_COMBINATIONS
    ]
    reachability = [
        search_fault_reachability(
            seed, combination, max_action_attempts=PREFLIGHT_BUDGET.max_action_attempts
        )
        for seed, combination in planned
        if combination.is_designated
    ]
    reachable_by_cell = {
        (case.seed, case.profile, case.goal_id): case.reachable for case in reachability
    }
    rows: list[PreflightRow] = []
    stop = False
    for seed in PREFLIGHT_SEEDS:
        if stop:
            break
        seed_root = root / "seeds" / f"seed-{seed:04d}"
        async with AsyncExitStack() as stack:
            clients: dict[str, GameClient] = {}
            recorders: dict[str, TriggerRecorder] = {}
            for profile in PROFILES:
                recorder = TriggerRecorder()
                app = create_lab_app(profile, recorder=recorder)
                http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=AGENT_EVAL_BASE_URL
                    )
                )
                clients[profile] = GameClient(
                    http,
                    base_url=AGENT_EVAL_BASE_URL,
                    timeout=PREFLIGHT_BUDGET.http_timeout_seconds,
                )
                recorders[profile] = recorder
            for combination in AGENT_COMBINATIONS:
                try:
                    evidence = await _run_cell(
                        combination,
                        provider=offline_provider_factory(combination.goal_id),
                        client=clients[combination.profile],
                        recorder=recorders[combination.profile],
                        root=seed_root,
                        budget_spec=PREFLIGHT_BUDGET,
                        pricing=None,
                        seed=seed,
                        control_scenario=control_scenario(seed, combination),
                    )
                    status = _row_status(
                        combination,
                        evidence,
                        reachable=reachable_by_cell.get(
                            (seed, combination.profile, combination.goal_id)
                        ),
                    )
                    error_kind = None
                except Exception as exc:  # noqa: BLE001 - 留下固定分母并停止；不记录外部异常原文
                    evidence = None
                    status = "execution_error"
                    error_kind = type(exc).__name__
                rows.append(
                    PreflightRow(
                        seed=seed,
                        profile=combination.profile,
                        goal_id=combination.goal_id,
                        report_root=str(seed_root.relative_to(root)),
                        status=status,
                        error_kind=error_kind,
                        evidence=evidence,
                    )
                )
                if status == "execution_error":
                    stop = True
                    break
    for seed, combination in planned[len(rows) :]:
        rows.append(
            PreflightRow(
                seed=seed,
                profile=combination.profile,
                goal_id=combination.goal_id,
                report_root=str((Path("seeds") / f"seed-{seed:04d}")),
                status="skipped",
            )
        )
    result = _summarize(rows, reachability, started_at=started_at, started=started, run_id=run_id)
    output = root / "agent-seed-preflight.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入 Agent 跨 seed 预检摘要：{type(exc).__name__}") from exc
    return result, output
