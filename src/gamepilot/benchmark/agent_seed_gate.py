"""固定四 seed 的胜利目标配对门槛；默认只运行离线测试替身。"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, model_validator

from gamepilot.agent.models import BudgetSpec, CostEstimate, TokenUsage
from gamepilot.agent.provider import PROVIDER_ANTHROPIC, ModelProvider
from gamepilot.lab import (
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_NORMAL,
    TriggerRecorder,
    create_lab_app,
)
from gamepilot.testing.client import GameClient
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso

from .agent_eval import (
    AGENT_EVAL_BASE_URL,
    _cell_error_stages,
    _run_cell,
    offline_provider_factory,
    summarize_spend,
)
from .agent_manifest import AGENT_COMBINATIONS, AgentCombination
from .agent_models import AgentCellEvidence, AgentPricing
from .agent_seed_preflight import _row_status, control_scenario
from .agent_seed_reachability import ReachabilityCase, search_fault_reachability

GATE_VERSION = "2.0.0"
GATE_GOAL_ID = "victory"
GATE_PROFILES = (PROFILE_NORMAL, FAULT_RETALIATE_AFTER_DEATH)
GATE_SEEDS = (0, 1, 2, 3)
GATE_MODEL = "deepseek-v4-flash"
GATE_BASE_URL = "https://api.deepseek.com/anthropic"
GATE_BUDGET = BudgetSpec(
    max_action_attempts=6,
    max_model_calls=6,
    max_format_retries=1,
    model_timeout_seconds=30,
    http_timeout_seconds=5,
    total_timeout_seconds=60,
    max_output_tokens=512,
)
GATE_MANIFEST_SOURCE = "docs/BATTLE_RULES_V2.md#缺陷靶场"
GATE_COMBINATIONS: tuple[AgentCombination, ...] = tuple(
    row
    for profile in GATE_PROFILES
    for row in AGENT_COMBINATIONS
    if row.profile == profile and row.goal_id == GATE_GOAL_ID
)
GATE_PLANNED = 8
GATE_MAX_MODEL_CALLS = GATE_PLANNED * GATE_BUDGET.max_model_calls
GATE_MAX_OUTPUT_TOKENS = GATE_MAX_MODEL_CALLS * GATE_BUDGET.max_output_tokens


class GateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GateRow(GateModel):
    seed: int
    profile: str
    goal_id: str
    report_root: str
    status: Literal["ok", "deviation", "execution_error", "skipped"]
    error_kind: str | None = None
    evidence: AgentCellEvidence | None = None


class GateReport(GateModel):
    version: str
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    manifest_source: str
    seeds: list[int]
    profiles: list[str]
    goal_id: str
    budget: dict[str, object]
    provider: dict[str, object]
    pricing: AgentPricing | None
    planned: int
    executed: int
    skipped: int
    execution_errors: int
    deviations: int
    replay_matches: int
    normal_goals_met: int
    normal_false_positives: int
    designated_reachable: int
    designated_triggered: int
    designated_detected: int
    max_model_calls: int
    max_output_tokens: int
    model_calls_observed: int | None
    usage_known_cells: int
    usage_unknown_cells: int
    usage: TokenUsage
    cost: CostEstimate
    reachability: list[ReachabilityCase]
    abort_after: str | None
    status: Literal["ok", "deviation", "execution_error"]
    exit_code: int
    rows: list[GateRow]

    @model_validator(mode="after")
    def _fixed_denominator(self) -> "GateReport":
        if self.planned != len(self.rows) or self.executed + self.skipped != self.planned:
            raise ValueError("门槛报告必须保留完整固定分母")
        if (
            self.executed != sum(row.status != "skipped" for row in self.rows)
            or self.execution_errors != sum(row.status == "execution_error" for row in self.rows)
            or self.deviations != sum(row.status == "deviation" for row in self.rows)
            or self.exit_code != {"ok": 0, "deviation": 1, "execution_error": 2}[self.status]
        ):
            raise ValueError("门槛报告计数或退出码与逐格结论不一致")
        if self.usage_known_cells + self.usage_unknown_cells != self.planned:
            raise ValueError("用量已知与未知格数必须覆盖固定分母")
        return self


def _provider_info(provider: ModelProvider) -> dict[str, object]:
    return {
        "provider_id": provider.provider_id,
        "model": provider.model,
        "base_url": provider.base_url,
        "is_test_double": provider.is_test_double,
        "api_key_env": provider.api_key_env,
        "sampling": dict(provider.sampling),
    }


def _summary(
    rows: list[GateRow],
    reachability: list[ReachabilityCase],
    *,
    provider: ModelProvider,
    pricing: AgentPricing | None,
    run_id: str,
    started_at: str,
    started: float,
) -> GateReport:
    evidence = [row.evidence for row in rows if row.evidence is not None]
    normal = [
        row.evidence for row in rows if row.profile == PROFILE_NORMAL and row.evidence is not None
    ]
    designated = [
        row.evidence
        for row in rows
        if row.profile == FAULT_RETALIATE_AFTER_DEATH and row.evidence is not None
    ]
    errors = sum(row.status == "execution_error" for row in rows)
    deviations = sum(row.status == "deviation" for row in rows)
    executed = sum(row.status != "skipped" for row in rows)
    status: Literal["ok", "deviation", "execution_error"] = (
        "execution_error" if errors else "deviation" if deviations else "ok"
    )
    known, unknown, usage, cost = summarize_spend(
        [item.spend for item in evidence], cells_planned=GATE_PLANNED, pricing=pricing
    )
    aborted = next((row for row in rows if row.status == "execution_error"), None)
    return GateReport(
        version=GATE_VERSION,
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=(time.perf_counter() - started) * 1000,
        manifest_source=GATE_MANIFEST_SOURCE,
        seeds=list(GATE_SEEDS),
        profiles=list(GATE_PROFILES),
        goal_id=GATE_GOAL_ID,
        budget=GATE_BUDGET.model_dump(),
        provider=_provider_info(provider),
        pricing=pricing,
        planned=GATE_PLANNED,
        executed=executed,
        skipped=GATE_PLANNED - executed,
        execution_errors=errors,
        deviations=deviations,
        replay_matches=sum(item.replay_outcome == "match" for item in evidence),
        normal_goals_met=sum(item.goal_met for item in normal),
        normal_false_positives=sum(item.case_status == "fail" for item in normal),
        designated_reachable=sum(item.reachable for item in reachability),
        designated_triggered=sum(item.triggered for item in designated),
        designated_detected=sum(item.detected for item in designated),
        max_model_calls=GATE_MAX_MODEL_CALLS,
        max_output_tokens=GATE_MAX_OUTPUT_TOKENS,
        model_calls_observed=(
            sum(item.spend.model_calls for item in evidence)
            if len(evidence) == GATE_PLANNED
            else None
        ),
        usage_known_cells=known,
        usage_unknown_cells=unknown,
        usage=usage,
        cost=cost,
        reachability=reachability,
        abort_after=(
            f"seed={aborted.seed} {aborted.profile} × {aborted.goal_id}" if aborted else None
        ),
        status=status,
        exit_code={"ok": 0, "deviation": 1, "execution_error": 2}[status],
        rows=rows,
    )


async def run_agent_seed_gate(
    output_dir: str | Path,
    *,
    provider_factory: Callable[[str], ModelProvider] | None = None,
    pricing: AgentPricing | None = None,
) -> tuple[GateReport, Path]:
    """固定 8 格顺序执行；真实供应商仅由调用方显式授权后装配。"""
    factory = provider_factory or offline_provider_factory
    selected_provider = factory(GATE_GOAL_ID)
    if not selected_provider.is_test_double and (
        selected_provider.provider_id != PROVIDER_ANTHROPIC
        or selected_provider.model != GATE_MODEL
        or selected_provider.base_url != GATE_BASE_URL
        or selected_provider.sampling.get("thinking") != "disabled"
        or selected_provider.sampling.get("max_retries") != 0
    ):
        raise ValueError("真实模型配置与四 seed 固定门槛不一致")
    started_at = utc_now_iso()
    started = time.perf_counter()
    run_id = new_run_id()
    root = Path(output_dir) / run_id
    planned = [(seed, row) for seed in GATE_SEEDS for row in GATE_COMBINATIONS]
    if len(planned) != GATE_PLANNED or [
        (row.profile, row.goal_id) for row in GATE_COMBINATIONS
    ] != [("normal", "victory"), ("retaliate_after_death", "victory")]:
        raise ValueError("四 seed 固定门槛清单漂移；禁止调用模型")
    reachability = [
        search_fault_reachability(seed, row, max_action_attempts=GATE_BUDGET.max_action_attempts)
        for seed, row in planned
        if row.is_designated
    ]
    if len(reachability) != len(GATE_SEEDS) or not all(item.reachable for item in reachability):
        raise ValueError("固定胜利缺陷机会不可达；禁止在无效清单上调用模型")
    reachable_by_seed = {item.seed: item.reachable for item in reachability}
    rows: list[GateRow] = []
    for index, (seed, combination) in enumerate(planned):
        seed_root = root / "seeds" / f"seed-{seed:04d}"
        recorder = TriggerRecorder()
        app = create_lab_app(combination.profile, recorder=recorder)
        try:
            cell_provider = selected_provider if index == 0 else factory(GATE_GOAL_ID)
            if _provider_info(cell_provider) != _provider_info(selected_provider):
                raise ValueError("同一门槛内供应商配置不一致")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=AGENT_EVAL_BASE_URL
            ) as http:
                client = GameClient(
                    http,
                    base_url=AGENT_EVAL_BASE_URL,
                    timeout=GATE_BUDGET.http_timeout_seconds,
                )
                cell = await _run_cell(
                    combination,
                    provider=cell_provider,
                    client=client,
                    recorder=recorder,
                    root=seed_root,
                    budget_spec=GATE_BUDGET,
                    pricing=pricing,
                    seed=seed,
                    control_scenario=control_scenario(seed, combination),
                )
                status = _row_status(
                    combination,
                    cell,
                    reachable=reachable_by_seed.get(seed) if combination.is_designated else None,
                )
            error_kind = ",".join(_cell_error_stages(cell)) or (
                "evidence" if status == "execution_error" else None
            )
        except Exception as exc:  # noqa: BLE001 - 固定分母与异常类型，不记录供应商异常原文
            cell = None
            status = "execution_error"
            error_kind = type(exc).__name__
        rows.append(
            GateRow(
                seed=seed,
                profile=combination.profile,
                goal_id=combination.goal_id,
                report_root=str(seed_root.relative_to(root)),
                status=status,
                error_kind=error_kind,
                evidence=cell,
            )
        )
        if status == "execution_error":
            break
    for seed, combination in planned[len(rows) :]:
        rows.append(
            GateRow(
                seed=seed,
                profile=combination.profile,
                goal_id=combination.goal_id,
                report_root=str(Path("seeds") / f"seed-{seed:04d}"),
                status="skipped",
            )
        )
    result = _summary(
        rows,
        reachability,
        provider=selected_provider,
        pricing=pricing,
        run_id=run_id,
        started_at=started_at,
        started=started,
    )
    output = root / "agent-seed-gate.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入 Agent 跨 seed 门槛摘要：{type(exc).__name__}") from exc
    return result, output
