"""固定跨种子脚本评测：复用 HTTP 执行器、独立判定器和无模型重放。"""

import hashlib
import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from gamepilot.lab import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_NORMAL,
    PROFILES,
    TriggerRecorder,
    create_lab_app,
)
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import ActionStep, CaseReport, ReplayCaseReport, Scenario
from gamepilot.testing.replay import load_report, replay_report
from gamepilot.testing.reporting import (
    ReportWriteError,
    new_run_id,
    utc_now_iso,
    write_report,
)
from gamepilot.testing.rules import (
    R_NO_RETALIATE_ON_KILL,
    R_POTION_CAP,
    R_POTION_DECREMENTS,
    RULES_VERSION,
)
from gamepilot.testing.runner import run_suite

SEED_SWEEP_VERSION = "1.1.0"
SEEDS = tuple(range(32))
TRACE_ACTIONS = {
    "attack-potion": ("attack", "use_potion"),
    "three-attacks": ("attack", "attack", "attack"),
}
TARGET_RULES = {
    FAULT_POTION_OVERHEAL: R_POTION_CAP,
    FAULT_POTION_NOT_CONSUMED: R_POTION_DECREMENTS,
    FAULT_RETALIATE_AFTER_DEATH: R_NO_RETALIATE_ON_KILL,
}
SUITE = "seed-sweep-v1"
BASE_URL = "http://seed-sweep.invalid"


class SweepModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SweepCell(SweepModel):
    profile: str
    seed: int
    trace: str
    case_id: str
    case_status: str
    triggered: bool
    trigger_fault_id: str | None
    target_rule: str | None
    target_hit: bool
    failing_rules: list[str]
    replay_outcome: str
    replay_status: str
    verdict: str


class SweepReport(SweepModel):
    version: str
    run_id: str
    started_at: str
    finished_at: str
    duration_ms: float
    rules_version: str
    seeds: list[int]
    traces: list[str]
    profiles: list[str]
    planned: int
    executed: int
    run_errors: int
    replay_errors: int
    deviations: int
    replay_matches: int
    trigger_counts: dict[str, int]
    detection_counts: dict[str, int]
    run_reports: dict[str, str]
    replay_reports: dict[str, str]
    report_sha256: dict[str, str]
    exit_code: int
    cells: list[SweepCell]


def _scenarios() -> tuple[Scenario, ...]:
    return tuple(
        Scenario(
            case_id=f"{trace}-seed-{seed:04d}",
            description=f"固定跨种子脚本：{trace}，seed={seed}",
            seed=seed,
            steps=[ActionStep(action=action) for action in actions],
        )
        for seed in SEEDS
        for trace, actions in TRACE_ACTIONS.items()
    )


def _verdict(
    profile: str, case: CaseReport, replayed: ReplayCaseReport, trigger_fault_id: str | None
) -> SweepCell:
    triggered = trigger_fault_id is not None
    target = TARGET_RULES.get(profile)
    failing = sorted({check.rule_id for check in case.checks if check.status == "fail"})
    hit = target in failing if target is not None else False
    if case.status == "error" or replayed.execution.status == "error":
        verdict = "execution_error"
    elif replayed.outcome == "not_comparable":
        verdict = "execution_error"
    elif replayed.outcome != "match":
        verdict = "deviation"
    elif trigger_fault_id not in (None, profile):
        verdict = "deviation"
    elif profile == PROFILE_NORMAL:
        verdict = "ok" if case.status == "pass" and not triggered else "deviation"
    else:
        expected = (case.status == "fail" and hit) if triggered else case.status == "pass"
        verdict = "ok" if expected else "deviation"
    trace = next(name for name in TRACE_ACTIONS if case.case_id.startswith(f"{name}-seed-"))
    return SweepCell(
        profile=profile,
        seed=case.seed,
        trace=trace,
        case_id=case.case_id,
        case_status=case.status,
        triggered=triggered,
        trigger_fault_id=trigger_fault_id,
        target_rule=target,
        target_hit=hit,
        failing_rules=failing,
        replay_outcome=replayed.outcome,
        replay_status=replayed.execution.status,
        verdict=verdict,
    )


async def run_seed_sweep(output_dir: str | Path) -> tuple[SweepReport, Path]:
    """执行固定 32 seed × 2 轨迹 × 4 profile；不选择有利种子。"""
    started_at = utc_now_iso()
    started = time.perf_counter()
    run_id = new_run_id()
    root = Path(output_dir) / run_id
    scenarios = _scenarios()
    cells: list[SweepCell] = []
    runs: dict[str, str] = {}
    replays: dict[str, str] = {}
    digests: dict[str, str] = {}

    for profile in PROFILES:
        recorder = TriggerRecorder()
        app = create_lab_app(profile, recorder=recorder)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as http:
            client = GameClient(http, base_url=BASE_URL)
            report = await run_suite(client, SUITE, scenarios)
            run_path = write_report(report, root / "runs" / profile)
            loaded = load_report(run_path)
            replayed = await replay_report(client, loaded, source_report=str(run_path))
            replay_path = write_report(replayed, root / "replays" / profile)
            runs[profile] = str(run_path.relative_to(root))
            replays[profile] = str(replay_path.relative_to(root))
            digests[runs[profile]] = hashlib.sha256(run_path.read_bytes()).hexdigest()
            digests[replays[profile]] = hashlib.sha256(replay_path.read_bytes()).hexdigest()
            if len(report.cases) != len(scenarios) or len(replayed.cases) != len(scenarios):
                raise ValueError("跨种子运行或重放数量不等于固定清单")
            for case, replay_case in zip(report.cases, replayed.cases):
                if case.case_id != replay_case.case_id:
                    raise ValueError("跨种子运行和重放场景顺序不一致")
                cells.append(
                    _verdict(
                        profile,
                        case,
                        replay_case,
                        (trigger.fault_id if (trigger := recorder.get(case.session_id)) else None),
                    )
                )

    triggers = {
        profile: sum(c.profile == profile and c.triggered for c in cells) for profile in PROFILES
    }
    detections = {
        profile: sum(c.profile == profile and c.triggered and c.target_hit for c in cells)
        for profile in PROFILES
    }
    run_errors = sum(c.case_status == "error" for c in cells)
    replay_errors = sum(
        c.replay_status == "error" or c.replay_outcome == "not_comparable" for c in cells
    )
    deviations = sum(c.verdict == "deviation" for c in cells)
    missing_coverage = any(triggers[profile] == 0 for profile in TARGET_RULES)
    exit_code = 2 if run_errors or replay_errors else 1 if deviations or missing_coverage else 0
    result = SweepReport(
        version=SEED_SWEEP_VERSION,
        run_id=run_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_ms=(time.perf_counter() - started) * 1000,
        rules_version=RULES_VERSION,
        seeds=list(SEEDS),
        traces=list(TRACE_ACTIONS),
        profiles=list(PROFILES),
        planned=len(PROFILES) * len(scenarios),
        executed=len(cells),
        run_errors=run_errors,
        replay_errors=replay_errors,
        deviations=deviations,
        replay_matches=sum(c.replay_outcome == "match" for c in cells),
        trigger_counts=triggers,
        detection_counts=detections,
        run_reports=runs,
        replay_reports=replays,
        report_sha256=digests,
        exit_code=exit_code,
        cells=cells,
    )
    summary_path = root / "seed-sweep.json"
    try:
        with summary_path.open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2))
        SweepReport.model_validate(json.loads(summary_path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise ReportWriteError(f"无法写入跨种子摘要：{type(exc).__name__}") from exc
    return result, summary_path
