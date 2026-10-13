"""固定新 seed 的公平脚本对照与离线预检失败分母。"""

import asyncio
from pathlib import Path

import httpx
import pytest

from gamepilot.benchmark import agent_seed_preflight as preflight
from gamepilot.benchmark.agent_manifest import AGENT_COMBINATIONS, AgentCombination
from gamepilot.benchmark.agent_models import AgentCellEvidence
from gamepilot.benchmark.agent_seed_reachability import search_fault_reachability
from gamepilot.lab import PROFILE_NORMAL, TriggerRecorder, create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import CaseReport
from gamepilot.testing.runner import run_case


@pytest.mark.parametrize("seed", preflight.PREFLIGHT_SEEDS)
@pytest.mark.parametrize("goal_id", ("full-health", "healing", "victory"))
def test_control_script_is_valid_at_its_fixed_seed(seed: int, goal_id: str) -> None:
    combination = next(
        row
        for row in AGENT_COMBINATIONS
        if row.profile == PROFILE_NORMAL and row.goal_id == goal_id
    )
    scenario = preflight.control_scenario(seed, combination)

    async def run() -> CaseReport:
        app = create_lab_app(PROFILE_NORMAL)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://preflight.invalid"
        ) as http:
            client = GameClient(http, base_url="http://preflight.invalid")
            return await run_case(client, scenario)

    case = asyncio.run(run())
    assert case.status == "pass"
    assert case.seed == seed
    if goal_id == "victory":
        assert len(case.executed_actions) == preflight.VICTORY_ATTACKS[seed] + 1
        assert case.final_snapshot.status == "won"


@pytest.mark.parametrize("seed", preflight.PREFLIGHT_SEEDS)
@pytest.mark.parametrize("combination", [row for row in AGENT_COMBINATIONS if row.is_designated])
def test_designated_reachability_matches_http_oracle(
    seed: int, combination: AgentCombination
) -> None:
    proof = search_fault_reachability(
        seed, combination, max_action_attempts=preflight.PREFLIGHT_BUDGET.max_action_attempts
    )
    scenario = preflight.control_scenario(seed, combination)
    recorder = TriggerRecorder()

    async def run() -> CaseReport:
        app = create_lab_app(combination.profile, recorder=recorder)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://preflight.invalid"
        ) as http:
            client = GameClient(http, base_url="http://preflight.invalid")
            return await run_case(client, scenario)

    case = asyncio.run(run())
    triggered = recorder.get(case.session_id) is not None
    failed_rules = {
        check.rule_id
        for check in [*case.checks, *(check for step in case.steps for check in step.checks)]
        if check.status == "fail"
    }
    assert triggered is proof.reachable
    if proof.reachable:
        assert case.status == "fail"
        assert set(combination.target_rules).intersection(failed_rules)
        assert proof.witness_actions
    else:
        assert seed == 0 and combination.profile == "potion_overheal"
        assert case.status == "pass"
        assert proof.witness_actions == []
        assert proof.sequences_checked == proof.search_space_size == 126


def _evidence(profile: str, goal_id: str, *, designated: bool = False) -> AgentCellEvidence:
    return AgentCellEvidence(
        profile=profile,
        goal_id=goal_id,
        goal="test goal",
        fault_id=None if profile == PROFILE_NORMAL else profile,
        is_designated=designated,
        target_rules=["R-TARGET"] if designated else [],
        run_id="run",
        agent_report_path="agent.json",
        run_report_path="run.json",
        stop_reason="goal_met",
        exit_code=0,
        goal_met=True,
        case_status="pass",
        replay_outcome="match",
        replay_path="replay.json",
        replay_status="pass",
        control_report_path="control.json",
        control_case_status="pass",
    )


def test_preflight_classifies_trigger_miss_and_replay_failure() -> None:
    normal = next(row for row in AGENT_COMBINATIONS if row.profile == PROFILE_NORMAL)
    variant = next(row for row in AGENT_COMBINATIONS if row.is_designated)
    control = _evidence(PROFILE_NORMAL, normal.goal_id)
    assert preflight._row_status(normal, control, reachable=None) == "ok"
    control.control_case_status = "fail"
    assert preflight._row_status(normal, control, reachable=None) == "deviation"
    control.control_case_status = "pass"
    control.goal_met = False
    assert preflight._row_status(normal, control, reachable=None) == "deviation"

    fault = _evidence(variant.profile, variant.goal_id, designated=True)
    fault.control_case_status = "fail"
    fault.control_failing_rules = list(variant.target_rules)
    assert preflight._row_status(variant, fault, reachable=True) == "deviation"
    fault.triggered = True
    fault.case_status = "fail"
    fault.exit_code = 1
    assert preflight._row_status(variant, fault, reachable=True) == "deviation"
    fault.target_rules_hit = ["R-TARGET"]
    assert preflight._row_status(variant, fault, reachable=True) == "ok"
    fault.control_failing_rules = []
    assert preflight._row_status(variant, fault, reachable=True) == "deviation"
    fault.control_failing_rules = list(variant.target_rules)
    fault.replay_outcome = "mismatch"
    assert preflight._row_status(variant, fault, reachable=True) == "deviation"
    fault.replay_outcome = "not_comparable"
    assert preflight._row_status(variant, fault, reachable=True) == "execution_error"
    fault.replay_outcome = "match"
    fault.replay_path = ""
    assert preflight._row_status(variant, fault, reachable=True) == "execution_error"

    unreachable = _evidence(variant.profile, variant.goal_id, designated=True)
    assert preflight._row_status(variant, unreachable, reachable=False) == "ok"
    unreachable.triggered = True
    assert preflight._row_status(variant, unreachable, reachable=False) == "deviation"


def test_unexpected_error_keeps_fixed_denominator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated")

    monkeypatch.setattr(preflight, "_run_cell", fail)
    report, path = asyncio.run(preflight.run_agent_seed_preflight(tmp_path))
    assert (report.planned, report.executed, report.skipped) == (48, 1, 47)
    assert report.exit_code == 2 and report.execution_errors == 1
    assert report.rows[0].error_kind == "OSError"
    assert all(row.status == "skipped" for row in report.rows[1:])
    assert path.is_file()
