"""本地页面的一次 Agent 测试；复用既有图、独立判定和重放。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path
from traceback import extract_tb
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from gamepilot.agent.models import BudgetSpec
from gamepilot.agent.provider import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL, DEFAULT_MODEL
from gamepilot.agent.skills import DEFAULT_SKILL_ID, SkillStore
from gamepilot.lab.selection import activation_opportunity, select_faults
from gamepilot.persistence.models import AgentTestRunRow
from gamepilot.testing.reporting import RUN_ID_PATTERN, new_run_id, utc_now_iso

WEB_BUDGET = BudgetSpec(
    max_action_attempts=50,
    max_model_calls=50,
    max_format_retries=1,
    model_timeout_seconds=30,
    http_timeout_seconds=5,
    total_timeout_seconds=600,
    max_output_tokens=512,
)
WEB_BASE_URL = "http://gamepilot-agent-test.local"
WEB_REPORT_ROOT = Path("artifacts/agent-web-runs")
LOGGER = logging.getLogger(__name__)
PREVIEW_ACCESS_MESSAGE = "当前版本无权限，请联系平台制作者申请权限。"


def public_preview_enabled() -> bool:
    """公开预览由部署环境显式开启；默认的本地流程保持原样。"""
    return os.environ.get("GAMEPILOT_PUBLIC_PREVIEW", "").lower() in {"1", "true"}


def real_key() -> tuple[str, str] | None:
    """只在服务端读取密钥；兼容此前配置的 DeepSeek Anthropic 入口。"""
    if public_preview_enabled():
        return None
    if key := os.environ.get(DEFAULT_API_KEY_ENV):
        return DEFAULT_API_KEY_ENV, key
    if os.environ.get("ANTHROPIC_BASE_URL") == DEFAULT_BASE_URL:
        if key := os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "ANTHROPIC_AUTH_TOKEN", key
    return None


def _safe_view(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "reports"}


class AgentWebRunManager:
    """单实例同一时间只启动一个任务；PostgreSQL 模式保存完整证据。"""

    def __init__(self, db_factory: sessionmaker[Session] | None = None) -> None:
        self._db_factory = db_factory
        self._records: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.skills = SkillStore()

    def _save(self, record: dict[str, Any]) -> None:
        self._records[record["run_id"]] = record
        if self._db_factory is not None:
            with self._db_factory.begin() as db:
                db.merge(AgentTestRunRow(run_id=record["run_id"], payload=record))

    def _load(self, run_id: str) -> dict[str, Any] | None:
        if record := self._records.get(run_id):
            return record
        if self._db_factory is None:
            return None
        with self._db_factory() as db:
            row = db.get(AgentTestRunRow, run_id)
            if row is None:
                return None
            record = dict(row.payload)
        if record["status"] in ("queued", "running"):
            record.update(status="error", phase="interrupted", message="服务重启，运行已中断")
            self._save(record)
        return record

    def get(self, run_id: str, *, visitor_id: str | None = None) -> dict[str, Any] | None:
        if visitor_id is not None and self._owners.get(run_id) != visitor_id:
            return None
        record = self._load(run_id)
        return _safe_view(record) if record is not None else None

    def report(self, run_id: str, *, visitor_id: str | None = None) -> dict[str, Any] | None:
        if visitor_id is not None and self._owners.get(run_id) != visitor_id:
            return None
        record = self._load(run_id)
        return record.get("reports") if record is not None else None

    async def start(
        self, config: dict[str, Any], *, visitor_id: str | None = None
    ) -> dict[str, Any]:
        async with self._start_lock:
            if config["provider_mode"] == "real" and public_preview_enabled():
                raise ValueError(PREVIEW_ACCESS_MESSAGE)
            if self._task is not None and not self._task.done():
                raise RuntimeError("已有自动测试正在运行，请等待它结束")
            if config["provider_mode"] == "real" and real_key() is None:
                raise ValueError("服务端未配置真实模型密钥")
            skill = self.skills.get(config.get("skill_id", DEFAULT_SKILL_ID))
            if skill is None:
                raise ValueError("所选 skill 不存在，请刷新列表后重试")
            if config["provider_mode"] == "offline" and not skill.built_in:
                raise ValueError("上传的自定义 skill 需要真实模型执行；离线替身只支持默认策略")
            config = dict(config)
            legacy_profile = config.get("profile")
            if legacy_profile is not None:
                selected = () if legacy_profile == "normal" else (legacy_profile,)
                config["fault_selection_mode"] = "legacy"
                config["selected_faults"] = list(selected)
            else:
                selection = select_faults(config["seed"], tuple(config.get("forced_faults") or ()))
                config["fault_selection_mode"] = selection.mode
                config["selected_faults"] = list(selection.fault_ids)
            config["fault_timing_mode"] = (
                "immediate" if config["fault_selection_mode"] == "legacy" else "seeded"
            )
            config["fault_schedule"] = {
                fault_id: (
                    1
                    if config["fault_timing_mode"] == "immediate"
                    else activation_opportunity(
                        config["seed"], config.get("battle_count", 1), fault_id
                    )
                )
                for fault_id in config["selected_faults"]
            }
            run_id = new_run_id()
            record: dict[str, Any] = {
                "run_id": run_id,
                "config": config,
                "skill": skill.model_dump(),
                "status": "queued",
                "phase": "queued",
                "message": "测试已创建，等待开始",
                "created_at": utc_now_iso(),
                "budget": WEB_BUDGET.model_dump(),
                "model_calls": 0,
                "actions_attempted": 0,
                "steps": [],
                "result": None,
                "reports": None,
            }
            if visitor_id is not None:
                self._owners[run_id] = visitor_id
            self._save(record)
            self._task = asyncio.create_task(self._execute(record))
            return _safe_view(record)

    async def clear_visitor(self, visitor_id: str) -> None:
        """停止该访客的运行并删除其内存记录和独立报告目录。"""
        async with self._start_lock:
            owned = [run_id for run_id, owner in self._owners.items() if owner == visitor_id]
            if self._task is not None and not self._task.done():
                active = [
                    run_id
                    for run_id in owned
                    if self._records.get(run_id, {}).get("status") in ("queued", "running")
                ]
                if active:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
            for run_id in owned:
                if RUN_ID_PATTERN.fullmatch(run_id):
                    root = WEB_REPORT_ROOT.resolve()
                    target = (WEB_REPORT_ROOT / run_id).resolve()
                    if target.parent == root:
                        try:
                            shutil.rmtree(target)
                        except FileNotFoundError:
                            pass
                self._records.pop(run_id, None)
                self._owners.pop(run_id, None)

    def _update(self, record: dict[str, Any], **changes: Any) -> None:
        record.update(changes)
        self._save(record)

    def _progress(self, record: dict[str, Any], node: str, state: Any, context: Any) -> None:
        steps = list(record["steps"])
        message = {
            "initialize": "已读取初始战斗状态",
            "decide": "模型已返回选择",
            "validate": "已校验工具请求",
            "execute": "已执行动作并检查规则",
            "check": "已计算目标覆盖",
            "route": "正在决定下一步",
            "finalize": "正在整理运行报告",
        }[node]
        item: dict[str, Any] | None = None
        if node == "initialize" and (snapshot := state.get("snapshot")) is not None:
            item = {"kind": "initial", "snapshot": snapshot.model_dump(mode="json")}
        elif node == "decide" and context.model_call_records:
            call = context.model_call_records[-1]
            if not any(
                row.get("kind") == "model" and row.get("index") == call.index for row in steps
            ):
                item = {
                    "kind": "model",
                    "index": call.index,
                    "call_kind": call.kind,
                    "outcome": call.outcome,
                    "response_text": call.response_text,
                    "tool_calls": [tool.model_dump(mode="json") for tool in call.tool_calls],
                }
        elif node == "validate" and (decisions := state.get("decisions")):
            decision = decisions[-1]
            item = {"kind": "decision", **decision.model_dump(mode="json")}
        elif node == "execute" and context.execution.steps:
            step = context.execution.steps[-1]
            if not any(
                row.get("kind") == "action" and row.get("index") == step.index for row in steps
            ):
                item = {"kind": "action", **step.model_dump(mode="json")}
        elif node == "check" and (coverage := state.get("coverage")) is not None:
            item = {"kind": "coverage", **coverage.model_dump(mode="json")}
        if item is not None:
            item["sequence"] = len(steps) + 1
            steps.append(item)
        self._update(
            record,
            status="running",
            phase=node,
            message=message,
            model_calls=context.budget.model_calls,
            actions_attempted=context.budget.actions,
            steps=steps,
        )

    async def _execute(self, record: dict[str, Any]) -> None:
        try:
            await self._run(record)
        except Exception as exc:  # noqa: BLE001 - 对外只给稳定且无凭据的错误分类
            frames = extract_tb(exc.__traceback__)
            last = frames[-1] if frames else None
            LOGGER.error(
                "Agent run %s failed: %s at %s:%s",
                record["run_id"],
                type(exc).__name__,
                Path(last.filename).name if last else "unknown",
                last.lineno if last else 0,
            )
            self._update(
                record,
                status="error",
                phase="error",
                message=f"执行错误：{type(exc).__name__}；请检查服务端日志",
            )

    async def _run(self, record: dict[str, Any]) -> None:
        # 延迟导入，手动页面在未安装 agent 可选依赖时仍可启动。
        from gamepilot.agent.budget import Budget
        from gamepilot.agent.goals import resolve_goal
        from gamepilot.agent.graph import run_agent
        from gamepilot.agent.models import AgentRunReportRef
        from gamepilot.agent.provider import AnthropicCompatibleProvider
        from gamepilot.agent.report import build_agent_report, write_agent_report
        from gamepilot.benchmark.agent_eval import observation_reacting_double
        from gamepilot.lab import TriggerRecorder, create_lab_app
        from gamepilot.testing.client import GameClient
        from gamepilot.testing.replay import replay_report
        from gamepilot.testing.reporting import single_case_report, write_report
        from gamepilot.testing.rules import (
            R_EXP_REWARD,
            R_LEVEL_UP,
            R_LOOT_ITEM,
            R_LOOT_PRESENT,
            R_LOOT_RANGE,
            R_NO_ACTION_AFTER_PLAYER_DEATH,
            R_NO_RETALIATE_ON_KILL,
            R_POTION_CAP,
            R_POTION_DECREMENTS,
            R_SKILL_POWER,
        )

        config = record["config"]
        skill = record["skill"]
        goal = resolve_goal(config["goal_id"])
        if config["provider_mode"] == "offline":
            provider = observation_reacting_double(goal.goal_id)
        else:
            key_name, key = real_key() or ("", "")
            if not key:
                raise ValueError("服务端未配置真实模型密钥")
            provider = AnthropicCompatibleProvider(
                api_key=key,
                api_key_env=key_name,
                model=DEFAULT_MODEL,
                base_url=DEFAULT_BASE_URL,
                thinking="disabled",
            )
        recorder = TriggerRecorder()
        lab_app = create_lab_app(
            config.get("profile") or "normal",
            recorder=recorder,
            fault_ids=frozenset(config["selected_faults"]),
            timing_mode=config["fault_timing_mode"],
        )
        budget = Budget(WEB_BUDGET)
        self._update(record, status="running", phase="initialize", message="正在创建隔离战斗")
        started_at = utc_now_iso()
        started = time.perf_counter()
        output_dir = WEB_REPORT_ROOT / record["run_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lab_app), base_url=WEB_BASE_URL
        ) as http:
            client = GameClient(
                http, base_url=WEB_BASE_URL, timeout=WEB_BUDGET.http_timeout_seconds
            )
            state, context, _meta = await run_agent(
                client=client,
                provider=provider,
                budget=budget,
                goal=goal,
                seed=config["seed"],
                battle_count=config.get("battle_count", 1),
                description=f"网页 Agent 连战测试 {goal.goal_id}",
                skill_instructions=skill["instructions"],
                timeout_provider=lambda: budget.request_timeout(WEB_BUDGET.http_timeout_seconds),
                on_update=lambda node, current, ctx: self._progress(record, node, current, ctx),
            )
            case = context.case_report
            if case is None:
                raise RuntimeError("Agent 未生成游戏运行报告")
            run_report = single_case_report(case, client, record["run_id"], "agent-web")
            run_path = write_report(run_report, output_dir / "run")
            agent_report = build_agent_report(
                state=state,
                context=context,
                client=client,
                run_id=record["run_id"],
                started_at=started_at,
                duration_ms=(time.perf_counter() - started) * 1000,
                run_report=AgentRunReportRef(run_id=record["run_id"], path=str(run_path)),
            )
            write_agent_report(agent_report, output_dir / "agent")
            self._update(record, phase="replay", message="正在无模型重放")
            replay = await replay_report(client, run_report, source_report=str(run_path))
            write_report(replay, output_dir / "replay")

        triggers = recorder.for_session(case.session_id)
        failing = sorted({check.rule_id for check in case.checks if check.status == "fail"})
        target_rules = {
            "potion_overheal": [R_POTION_CAP],
            "potion_not_consumed": [R_POTION_DECREMENTS],
            "retaliate_after_death": [R_NO_RETALIATE_ON_KILL, R_NO_ACTION_AFTER_PLAYER_DEATH],
            "loot_anomaly": [R_LOOT_PRESENT, R_LOOT_RANGE, R_LOOT_ITEM, R_EXP_REWARD],
            "level_anomaly": [R_LEVEL_UP],
            "skill_power_anomaly": [R_SKILL_POWER],
        }
        selected_faults = config["selected_faults"]
        triggered_faults = {item.fault_id for item in triggers}
        detected_faults = [
            fault
            for fault in selected_faults
            if fault in triggered_faults and set(failing) & set(target_rules[fault])
        ]
        missed_faults = [
            fault
            for fault in selected_faults
            if fault in triggered_faults and fault not in detected_faults
        ]
        untriggered_faults = [fault for fault in selected_faults if fault not in triggered_faults]
        target = sorted({rule for fault in selected_faults for rule in target_rules[fault]})
        target_hit = sorted(set(failing) & set(target))
        replay_outcome = replay.cases[0].outcome if replay.cases else "not_executed"
        replay_status = replay.cases[0].execution.status if replay.cases else None
        if (
            agent_report.summary.exit_code == 2
            or replay_outcome != "match"
            or replay_status == "error"
        ):
            verdict = "execution_error"
        elif missed_faults:
            verdict = "agent_missed"
        elif detected_faults:
            verdict = "defect_detected"
        elif untriggered_faults:
            verdict = "not_triggered"
        elif failing:
            verdict = "rule_failure"
        elif not agent_report.coverage.met:
            verdict = "goal_incomplete"
        else:
            verdict = "pass"
        result = {
            "verdict": verdict,
            "triggered": bool(triggers),
            "trigger_detail": triggers[0].detail if triggers else None,
            "trigger_details": [
                {
                    "fault_id": item.fault_id,
                    "detail": item.detail,
                    "battle_index": item.battle_index,
                    "turn": item.turn,
                    "opportunity": item.opportunity,
                }
                for item in triggers
            ],
            "selected_faults": selected_faults,
            "detected_faults": detected_faults,
            "missed_faults": missed_faults,
            "untriggered_faults": untriggered_faults,
            "failing_rules": failing,
            "target_rules": target,
            "target_rules_hit": target_hit,
            "goal_met": agent_report.coverage.met,
            "coverage": agent_report.coverage.model_dump(mode="json"),
            "stop_reason": agent_report.summary.stop_reason,
            "stop_detail": agent_report.summary.stop_detail,
            "replay_outcome": replay_outcome,
            "replay_status": replay_status,
            "usage": agent_report.summary.usage.model_dump(mode="json"),
            "cost": agent_report.summary.cost.model_dump(mode="json"),
            "provider_is_test_double": provider.is_test_double,
        }
        self._update(
            record,
            status="completed",
            phase="completed",
            message="测试结束，报告已保存",
            result=result,
            reports={
                "skill": skill,
                "lab": {
                    "seed": config["seed"],
                    "battle_count": config.get("battle_count", 1),
                    "selection_mode": config["fault_selection_mode"],
                    "timing_mode": config["fault_timing_mode"],
                    "selected_faults": selected_faults,
                    "fault_schedule": config["fault_schedule"],
                    "trigger_details": result["trigger_details"],
                    "detected_faults": detected_faults,
                    "untriggered_faults": untriggered_faults,
                    "verdict": verdict,
                },
                "agent": agent_report.model_dump(mode="json"),
                "run": run_report.model_dump(mode="json"),
                "replay": replay.model_dump(mode="json"),
            },
        )
