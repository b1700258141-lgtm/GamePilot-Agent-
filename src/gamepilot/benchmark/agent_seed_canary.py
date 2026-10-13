"""固定新 seed 的单格 Agent 配对 canary；默认只用离线测试替身。"""

import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from gamepilot.agent.goals import GOAL_VICTORY
from gamepilot.agent.models import BudgetSpec
from gamepilot.agent.provider import ModelProvider
from gamepilot.lab import FAULT_RETALIATE_AFTER_DEATH, TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso

from .agent_eval import AGENT_EVAL_BASE_URL, _run_cell, offline_provider_factory
from .agent_manifest import AGENT_COMBINATIONS, AgentCombination
from .agent_models import AgentCellEvidence, AgentPricing
from .agent_seed_preflight import _row_status, control_scenario
from .agent_seed_reachability import ReachabilityCase, search_fault_reachability

CANARY_VERSION = "1.0.0"
CANARY_SEED = 2
CANARY_PROFILE = FAULT_RETALIATE_AFTER_DEATH
CANARY_GOAL_ID = GOAL_VICTORY
CANARY_BUDGET = BudgetSpec(
    max_action_attempts=6,
    max_model_calls=6,
    max_format_retries=1,
    model_timeout_seconds=30,
    http_timeout_seconds=5,
    total_timeout_seconds=60,
    max_output_tokens=512,
)
MANIFEST_SOURCE = "docs/claude-tasks/TASK-006E-agent-seed-canary.md"


class CanaryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: str
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    manifest_source: str
    seed: int
    profile: str
    goal_id: str
    budget: dict[str, object]
    completion_token_upper_bound: int
    provider: dict[str, object]
    pricing: AgentPricing | None
    reachability: ReachabilityCase
    status: Literal["ok", "deviation", "execution_error"]
    exit_code: int
    error_kind: str | None
    evidence: AgentCellEvidence | None


def _combination() -> AgentCombination:
    return next(
        row
        for row in AGENT_COMBINATIONS
        if row.profile == CANARY_PROFILE and row.goal_id == CANARY_GOAL_ID
    )


async def run_agent_seed_canary(
    output_dir: str | Path,
    *,
    provider: ModelProvider | None = None,
    pricing: AgentPricing | None = None,
) -> tuple[CanaryReport, Path]:
    """只执行冻结的一格；调用方须显式装配真实模型和付费授权。"""
    combination = _combination()
    reachability = search_fault_reachability(
        CANARY_SEED, combination, max_action_attempts=CANARY_BUDGET.max_action_attempts
    )
    if not reachability.reachable:
        raise ValueError("固定 canary 的缺陷在当前预算内不可达")
    scenario = control_scenario(CANARY_SEED, combination)
    selected_provider = provider or offline_provider_factory(CANARY_GOAL_ID)
    started_at = utc_now_iso()
    started = time.perf_counter()
    run_id = new_run_id()
    root = Path(output_dir) / run_id
    evidence: AgentCellEvidence | None = None
    error_kind: str | None = None
    recorder = TriggerRecorder()
    app = create_lab_app(CANARY_PROFILE, recorder=recorder)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=AGENT_EVAL_BASE_URL
        ) as http:
            client = GameClient(
                http,
                base_url=AGENT_EVAL_BASE_URL,
                timeout=CANARY_BUDGET.http_timeout_seconds,
            )
            evidence = await _run_cell(
                combination,
                provider=selected_provider,
                client=client,
                recorder=recorder,
                root=root,
                budget_spec=CANARY_BUDGET,
                pricing=pricing,
                seed=CANARY_SEED,
                control_scenario=scenario,
            )
            status = _row_status(combination, evidence, reachable=True)
    except Exception as exc:  # noqa: BLE001 - 保留固定单格和异常类型，不记录供应商异常原文
        status = "execution_error"
        error_kind = type(exc).__name__
    result = CanaryReport(
        version=CANARY_VERSION,
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=(time.perf_counter() - started) * 1000,
        manifest_source=MANIFEST_SOURCE,
        seed=CANARY_SEED,
        profile=CANARY_PROFILE,
        goal_id=CANARY_GOAL_ID,
        budget=CANARY_BUDGET.model_dump(),
        completion_token_upper_bound=(
            CANARY_BUDGET.max_model_calls * CANARY_BUDGET.max_output_tokens
        ),
        provider={
            "provider_id": selected_provider.provider_id,
            "model": selected_provider.model,
            "base_url": selected_provider.base_url,
            "is_test_double": selected_provider.is_test_double,
            "api_key_env": selected_provider.api_key_env,
            "sampling": dict(selected_provider.sampling),
        },
        pricing=pricing,
        reachability=reachability,
        status=status,
        exit_code={"ok": 0, "deviation": 1, "execution_error": 2}[status],
        error_kind=error_kind,
        evidence=evidence,
    )
    output = root / "agent-seed-canary.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入 Agent 跨 seed canary 摘要：{type(exc).__name__}") from exc
    return result, output
