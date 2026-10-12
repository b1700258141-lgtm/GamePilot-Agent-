"""确认的规则失败可以变成回归；执行错误和伪造证据不能。"""

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gamepilot.agent.models import (
    AgentRunReport,
    AgentRunReportRef,
    AgentSummary,
    BudgetSpec,
    GoalCoverage,
    ProviderInfo,
)
from gamepilot.defects.logic import (
    DefectInputError,
    export_defect,
    load_artifact,
    verify_defect,
)
from gamepilot.lab.app import create_lab_app
from gamepilot.testing.client import GameClient
from gamepilot.testing.models import Scenario
from gamepilot.testing.replay import ReplayInputError, replay_report
from gamepilot.testing.reporting import (
    ReportWriteError,
    new_run_id,
    single_case_report,
    write_report,
)
from gamepilot.testing.rules import RULES_SOURCE, RULES_VERSION
from gamepilot.testing.runner import run_case
from gamepilot.testing.scenarios import get_suite


def _scenario(case_id: str) -> Scenario:
    return next(case for case in get_suite("baseline") if case.case_id == case_id)


async def _evidence(profile: str, scenario: Scenario, directory: Path) -> tuple[Path, Path]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app(profile))
    ) as http:
        client = GameClient(http, base_url="http://lab.test")
        case = await run_case(client, scenario)
        assert case.status == "fail", case.failure
        run = single_case_report(case, client, new_run_id(), "agent")
        run_path = write_report(run, directory / "run")
        replay = await replay_report(client, run, source_report=str(run_path))
        assert replay.summary.matched == 1
        replay_path = write_report(replay, directory / "replay")
    return run_path, replay_path


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("profile", "case_id", "rule_id", "actions"),
    [
        ("potion_overheal", "attack-then-potion", "R-POTION-CAP", ["attack", "use_potion"]),
        (
            "potion_not_consumed",
            "attack-then-potion",
            "R-POTION-DECREMENTS",
            ["attack", "use_potion"],
        ),
        (
            "retaliate_after_death",
            "win-then-rejected",
            "R-NO-RETALIATE-ON-KILL",
            ["attack", "attack", "attack"],
        ),
    ],
)
async def test_confirmed_defect_becomes_regression(
    profile: str, case_id: str, rule_id: str, actions: list[str], tmp_path: Path
) -> None:
    run_path, replay_path = await _evidence(profile, _scenario(case_id), tmp_path / profile)
    artifact, path = export_defect(
        run_path, replay_path, case_id, tmp_path / "defects", rule_id=rule_id
    )
    loaded = load_artifact(path)
    assert loaded == artifact
    assert loaded.actions == actions
    assert [step.action for step in loaded.regression.steps] == actions
    assert loaded.regression.expected_final_status is None
    assert loaded.regression.control is None
    assert "body" not in path.read_text(encoding="utf-8")

    for target_profile, expected_code, expected_outcome in (
        (profile, 1, "target_reproduced"),
        ("normal", 0, "passed"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_lab_app(target_profile))
        ) as http:
            client = GameClient(http, base_url="http://lab.test")
            code, report_path, case, outcome = await verify_defect(
                loaded, client, tmp_path / "verification"
            )
        assert (code, outcome) == (expected_code, expected_outcome)
        assert report_path.is_file()
        assert case.status == ("fail" if expected_code else "pass")


@pytest.mark.anyio
async def test_fingerprint_does_not_depend_on_run_id_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = _scenario("attack-then-potion")
    first = await _evidence("potion_not_consumed", scenario, tmp_path / "first")
    second = await _evidence("potion_not_consumed", scenario, tmp_path / "second")
    artifact1, path1 = export_defect(*first, scenario.case_id, tmp_path / "out")
    artifact2, path2 = export_defect(*second, scenario.case_id, tmp_path / "out")
    assert artifact1.fingerprint == artifact2.fingerprint
    assert path1 != path2
    assert path1.read_bytes() != path2.read_bytes()

    monkeypatch.setattr("gamepilot.defects.logic.new_run_id", lambda: "20261008T000000Z-abcdef")
    _artifact, fixed_path = export_defect(*first, scenario.case_id, tmp_path / "out")
    original = fixed_path.read_bytes()
    with pytest.raises(ReportWriteError):
        export_defect(*first, scenario.case_id, tmp_path / "out")
    assert fixed_path.read_bytes() == original


@pytest.mark.anyio
async def test_tampered_or_unconfirmed_evidence_is_rejected(tmp_path: Path) -> None:
    scenario = _scenario("attack-then-potion")
    run_path, replay_path = await _evidence("potion_not_consumed", scenario, tmp_path / "evidence")
    with pytest.raises(DefectInputError, match="指定规则"):
        export_defect(
            run_path,
            replay_path,
            scenario.case_id,
            tmp_path / "out",
            rule_id="R-NO-RETALIATE-ON-KILL",
        )
    replay_raw = json.loads(replay_path.read_text(encoding="utf-8"))
    replay_raw["source_run_id"] = "other-run"
    bad_replay = tmp_path / "bad-replay.json"
    bad_replay.write_text(json.dumps(replay_raw), encoding="utf-8")
    with pytest.raises(DefectInputError, match="没有引用"):
        export_defect(run_path, bad_replay, scenario.case_id, tmp_path / "out")

    replay_raw["source_run_id"] = json.loads(run_path.read_text(encoding="utf-8"))["run_id"]
    replay_raw["cases"][0]["outcome"] = "not_comparable"
    bad_replay.write_text(json.dumps(replay_raw), encoding="utf-8")
    with pytest.raises(ReplayInputError, match="汇总"):
        export_defect(run_path, bad_replay, scenario.case_id, tmp_path / "out")
    replay_raw["cases"][0]["outcome"] = "match"
    replay_raw["cases"][0]["execution"]["final_snapshot"]["player"]["hp"] -= 1
    bad_replay.write_text(json.dumps(replay_raw), encoding="utf-8")
    with pytest.raises(DefectInputError, match="逐字段"):
        export_defect(run_path, bad_replay, scenario.case_id, tmp_path / "out")
    assert not (tmp_path / "out").exists()

    artifact, path = export_defect(run_path, replay_path, scenario.case_id, tmp_path / "out")
    assert artifact.rule_id == "R-POTION-DECREMENTS"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["regression"]["steps"][0]["action"] = "use_potion"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(DefectInputError, match="不一致"):
        load_artifact(path)


@pytest.mark.anyio
async def test_run_report_identity_and_failure_location_cannot_be_forged(tmp_path: Path) -> None:
    scenario = _scenario("attack-then-potion")
    run_path, replay_path = await _evidence("potion_not_consumed", scenario, tmp_path)
    original = json.loads(run_path.read_text(encoding="utf-8"))
    for name in ("seed", "action_index", "failure_rule", "failure_step"):
        data = deepcopy(original)
        case = data["cases"][0]
        if name == "seed":
            case["seed"] = 99
        elif name == "action_index":
            case["executed_actions"][0]["index"] = 9
        elif name == "failure_rule":
            case["failure"]["rule_id"] = "R-NO-RETALIATE-ON-KILL"
        else:
            case["failure"]["step"] = 1
        candidate = tmp_path / f"{name}.json"
        candidate.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises((ReplayInputError, DefectInputError)):
            export_defect(candidate, replay_path, scenario.case_id, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.anyio
async def test_agent_reference_and_error_precedence(tmp_path: Path) -> None:
    scenario = _scenario("attack-then-potion")
    run_path, replay_path = await _evidence("potion_not_consumed", scenario, tmp_path)
    run = json.loads(run_path.read_text(encoding="utf-8"))
    agent = AgentRunReport(
        run_id=run["run_id"],
        goal_id="healing",
        goal="治疗",
        seed=scenario.seed,
        base_url="http://lab.test",
        started_at="2026-10-08T00:00:00Z",
        finished_at="2026-10-08T00:00:01Z",
        duration_ms=1000,
        provider=ProviderInfo(provider_id="fake", model="fake", is_test_double=True),
        budget=BudgetSpec(),
        prompts_version="test",
        rules_version=RULES_VERSION,
        rules_source=RULES_SOURCE,
        session_id=run["cases"][0]["session_id"],
        run_report=AgentRunReportRef(run_id=run["run_id"], path=str(run_path)),
        summary=AgentSummary(
            stop_reason="rule_failure", exit_code=1, rule_failures=["R-POTION-DECREMENTS"]
        ),
        coverage=GoalCoverage(goal_id="healing", goal="治疗", met=False),
    )
    agent_path = tmp_path / "agent.json"
    agent_path.write_text(agent.model_dump_json(indent=2), encoding="utf-8")
    artifact, _path = export_defect(
        run_path, replay_path, scenario.case_id, tmp_path / "out", agent_path=agent_path
    )
    assert artifact.source_agent is not None

    bad = agent.model_copy(
        update={"summary": AgentSummary(stop_reason="execution_error", exit_code=2)}
    )
    agent_path.write_text(bad.model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(DefectInputError, match="结论"):
        export_defect(
            run_path, replay_path, scenario.case_id, tmp_path / "out", agent_path=agent_path
        )
    wrong_ref = agent.model_copy(
        update={"run_report": AgentRunReportRef(run_id="other-run", path=str(run_path))}
    )
    agent_path.write_text(wrong_ref.model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(DefectInputError, match="结论"):
        export_defect(
            run_path, replay_path, scenario.case_id, tmp_path / "out", agent_path=agent_path
        )


@pytest.mark.anyio
async def test_verification_distinguishes_other_rule_execution_and_write_errors(
    tmp_path: Path,
) -> None:
    scenario = _scenario("attack-then-potion")
    run_path, replay_path = await _evidence("potion_not_consumed", scenario, tmp_path)
    artifact, _path = export_defect(run_path, replay_path, scenario.case_id, tmp_path / "out")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app("potion_overheal"))
    ) as http:
        code, path, _case, outcome = await verify_defect(
            artifact, GameClient(http, base_url="http://lab.test"), tmp_path / "verification"
        )
    assert (code, outcome) == (1, "other_rule_failure")
    assert path.is_file()

    absent_rule = artifact.model_copy(update={"rule_id": "R-NO-RETALIATE-ON-KILL"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app("normal"))
    ) as http:
        code, _path, case, outcome = await verify_defect(
            absent_rule, GameClient(http, base_url="http://lab.test"), tmp_path / "verification"
        )
    assert case.status == "pass"
    assert (code, outcome) == (1, "target_not_exercised")

    broken = FastAPI()

    @broken.post("/api/v1/game-sessions")
    async def unavailable() -> JSONResponse:
        return JSONResponse({"error": "unavailable"}, status_code=503)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=broken)) as http:
        code, path, _case, outcome = await verify_defect(
            artifact, GameClient(http, base_url="http://lab.test"), tmp_path / "verification"
        )
    assert (code, outcome) == (2, "execution_error")
    assert path.is_file()

    blocked_dir = tmp_path / "not-a-directory"
    blocked_dir.write_text("existing", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_lab_app("normal"))
    ) as http:
        with pytest.raises(ReportWriteError):
            await verify_defect(artifact, GameClient(http, base_url="http://lab.test"), blocked_dir)
