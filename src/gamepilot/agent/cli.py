"""命令行入口：`python -m gamepilot.agent run`。

退出码是对外契约（见 `report.derive_exit_code`）：

- 0：覆盖目标已达到、所观察规则全部通过并正常收束；
- 1：独立规则证据确认游戏违规；
- 2：模型 / 工具 / 输入 / 报告错误（优先于其他结论）；
- 3：正常请求但目标未完成（提前 finish、预算耗尽）。

三条安全默认值：

1. **付费默认关闭**。默认供应商是真实模型入口，但没有 `--paid` 时**一次模型请求都不会发**，
   直接以退出码 2 结束并说明原因；
2. **密钥只从环境读**。`--api-key-env` 给的是**变量名**，不是密钥本身；
   输出、报告与错误信息里只会出现变量名，任何位置都不会出现密钥值；
3. `langgraph` 只在真正执行时导入：`--help` 与参数错误在没装 agent extra 的环境里也能正常工作。
"""

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Sequence

import httpx
from pydantic import ValidationError

from gamepilot.testing.client import GameClient
from gamepilot.testing.reporting import ReportWriteError, new_run_id, utc_now_iso

from .goals import GOAL_IDS, UnknownGoalError, resolve_goal
from .models import AgentRunReport, AgentRunReportRef, BudgetSpec
from .prompts import PROMPT_VERSION, RULES_PROMPT_VERSION
from .provider import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    PROVIDER_ANTHROPIC,
    PROVIDER_FAKE,
)
from .report import (
    AGENT_OUTPUT_DIR,
    EXIT_ERROR,
    agent_report_path,
    build_agent_report,
    write_agent_report,
    write_run_report,
)

PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_FAKE)


def _default(field: str) -> object:
    return BudgetSpec.model_fields[field].default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gamepilot.agent",
        description="LangGraph 游戏测试 Agent：模型选动作、程序判定、报告可重跑。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行一次 Agent 闭环并写出两份报告")
    run_parser.add_argument(
        "--base-url", required=True, help="被测服务地址，如 http://127.0.0.1:8000"
    )
    run_parser.add_argument("--goal", required=True, choices=sorted(GOAL_IDS), help="测试目标编号")
    run_parser.add_argument("--seed", type=int, required=True, help="游戏会话 seed")
    run_parser.add_argument(
        "--provider",
        default=PROVIDER_ANTHROPIC,
        choices=sorted(PROVIDERS),
        help=(
            f"模型供应商（默认 {PROVIDER_ANTHROPIC}）；{PROVIDER_FAKE} 是测试替身，"
            "结果不计入 Agent 能力成绩"
        ),
    )
    run_parser.add_argument(
        "--paid",
        action="store_true",
        help="确认允许付费的真实模型调用；不给出时不会发出任何模型请求",
    )
    run_parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"模型名（默认 {DEFAULT_MODEL}）"
    )
    run_parser.add_argument(
        "--model-base-url", default=DEFAULT_BASE_URL, help="模型入口地址（仅真实供应商使用）"
    )
    run_parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"存放密钥的**环境变量名**（默认 {DEFAULT_API_KEY_ENV}）；不接受密钥本身",
    )
    run_parser.add_argument(
        "--script",
        default=None,
        help=(
            "测试替身的动作计划，如 attack,use_potion,finish；"
            f"只在 --provider {PROVIDER_FAKE} 时可用"
        ),
    )
    run_parser.add_argument(
        "--output-dir", default=AGENT_OUTPUT_DIR, help=f"报告目录（默认 {AGENT_OUTPUT_DIR}）"
    )
    run_parser.add_argument(
        "--max-actions",
        type=int,
        default=_default("max_action_attempts"),
        help="动作尝试上限（含被拒绝的动作）",
    )
    run_parser.add_argument(
        "--max-model-calls", type=int, default=_default("max_model_calls"), help="模型调用上限"
    )
    run_parser.add_argument(
        "--max-format-retries",
        type=int,
        default=_default("max_format_retries"),
        help="全任务累计的格式纠正次数上限",
    )
    run_parser.add_argument(
        "--model-timeout",
        type=float,
        default=_default("model_timeout_seconds"),
        help="单次模型调用超时秒数",
    )
    run_parser.add_argument(
        "--http-timeout",
        type=float,
        default=_default("http_timeout_seconds"),
        help="单次游戏 HTTP 超时秒数",
    )
    run_parser.add_argument(
        "--total-timeout",
        type=float,
        default=_default("total_timeout_seconds"),
        help="整个任务的时限秒数",
    )
    run_parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=_default("max_output_tokens"),
        help="单次模型调用的输出 Token 上限",
    )
    run_parser.add_argument(
        "--input-price", type=float, default=None, help="每百万输入 Token 的单价（给出才估算费用）"
    )
    run_parser.add_argument(
        "--output-price", type=float, default=None, help="每百万输出 Token 的单价"
    )
    run_parser.add_argument("--currency", default=None, help="费用币种，仅在给出单价时有意义")
    return parser


def _budget_spec(args: argparse.Namespace) -> BudgetSpec:
    return BudgetSpec(
        max_action_attempts=args.max_actions,
        max_model_calls=args.max_model_calls,
        max_format_retries=args.max_format_retries,
        model_timeout_seconds=args.model_timeout,
        http_timeout_seconds=args.http_timeout,
        total_timeout_seconds=args.total_timeout,
        max_output_tokens=args.max_output_tokens,
    )


def _parse_script(script: str) -> list[str]:
    """把 `attack,use_potion,finish` 解析成动作序列；末尾的 finish 表示申请结束。"""
    steps = [item.strip() for item in script.split(",") if item.strip()]
    if not steps:
        raise ValueError("--script 不能为空")
    return steps


def _make_provider(args: argparse.Namespace) -> object:
    """按参数建立供应商；真实供应商需要显式付费确认与可用的环境变量密钥。

    这里**不做任何网络访问**：`AnthropicCompatibleProvider` 只是建立客户端，
    真正的请求发生在图里的 `decide` 节点，且必先通过预算检查。
    """
    from .provider import AnthropicCompatibleProvider, FakeProvider, scripted_responder

    if args.provider == PROVIDER_FAKE:
        if not args.script:
            raise ValueError(f"--provider {PROVIDER_FAKE} 必须同时给出 --script")
        return FakeProvider(scripted_responder(_parse_script(args.script)))
    if not args.paid:
        raise ValueError(
            "付费运行默认关闭：未给出 --paid，本次不会发出任何模型请求。"
            f"确认预算后再加 --paid（密钥变量名 {args.api_key_env}，本次未读取它的值）"
        )
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        # 只报变量名：不回显值，也不描述它的内容。
        raise ValueError(
            f"环境变量 {args.api_key_env} 未设置，无法调用真实模型；"
            "请先导出它（本次不会显示或记录它的值）"
        )
    return AnthropicCompatibleProvider(
        api_key=api_key,
        model=args.model,
        base_url=args.model_base_url,
        api_key_env=args.api_key_env,
    )


async def _run_command(args: argparse.Namespace) -> int:
    goal = resolve_goal(args.goal)
    spec = _budget_spec(args)
    provider = _make_provider(args)
    run_id = new_run_id()

    if getattr(provider, "is_test_double", False):
        print("注意：本次使用测试替身供应商，结果不计入 Agent 能力成绩。")

    from .budget import Budget
    from .graph import run_agent

    budget = Budget(spec)
    started_at = utc_now_iso()
    started = time.perf_counter()
    async with httpx.AsyncClient(trust_env=False) as http:
        client = GameClient(http, base_url=args.base_url, timeout=spec.http_timeout_seconds)
        print(f"目标地址（本次命令提供）：{client.base_url}")
        print(
            f"目标={goal.goal_id} seed={args.seed} 供应商={provider.provider_id}"
            f" 模型={provider.model} 提示词={PROMPT_VERSION} 规则提示词={RULES_PROMPT_VERSION}"
        )
        state, context, _meta = await run_agent(
            client=client,
            provider=provider,
            budget=budget,
            goal=goal,
            seed=args.seed,
            description=f"Agent 目标 {goal.goal_id}（seed={args.seed}）",
            timeout_provider=lambda: budget.request_timeout(spec.http_timeout_seconds),
        )
        duration_ms = (time.perf_counter() - started) * 1000

        # 先写游戏事实、再写任务结论：后者要引用前者的路径。
        # 会话都没创建成功时不存在可重跑的轨迹，报告里如实留空。
        run_report_ref: AgentRunReportRef | None = None
        report_error: str | None = None
        if context.execution.session_id is not None:
            try:
                _, path = write_run_report(context.case_report, client, run_id, args.output_dir)
                run_report_ref = AgentRunReportRef(run_id=run_id, path=str(path))
            except ReportWriteError as exc:
                report_error = str(exc)

    if report_error is not None:
        # 报告写不出去就不能声称成功，也不能声称发现了缺陷：退出码以 2 为准。
        state["stop_reason"] = "input_error"
        state["stop_detail"] = f"运行报告写入失败：{report_error}"
        print(f"报告写入失败：{report_error}", file=sys.stderr)

    report = build_agent_report(
        state=state,
        context=context,
        client=client,
        run_id=run_id,
        started_at=started_at,
        duration_ms=duration_ms,
        run_report=run_report_ref,
        input_price_per_million=args.input_price,
        output_price_per_million=args.output_price,
        currency=args.currency,
    )
    _print_summary(report)
    try:
        path = write_agent_report(report, args.output_dir)
    except ReportWriteError as exc:
        print(f"Agent 报告写入失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Agent 报告：{path}")
    if run_report_ref is not None:
        print(f"运行报告（可用 testing replay 重跑）：{run_report_ref.path}")
    elif report_error is None:
        print("没有创建会话，因此没有可重跑的运行报告。")
    return report.summary.exit_code


def _print_summary(report: AgentRunReport) -> None:
    summary = report.summary
    coverage = report.coverage
    print(
        f"停止原因：{summary.stop_reason}"
        f"（动作尝试 {summary.actions_attempted}，模型调用 {summary.model_calls}，"
        f"格式纠正 {summary.format_retries}，耗时 {summary.duration_ms / 1000:.2f}s）"
    )
    if summary.stop_detail:
        print(f"  说明：{summary.stop_detail}")
    if summary.rule_failures:
        print(f"  规则失败：{', '.join(summary.rule_failures)}")
    print(f"目标覆盖：{'已达到' if coverage.met else '未达到'}（{coverage.goal_id}）")
    for condition in coverage.conditions:
        mark = "已满足" if condition.met else "未满足"
        print(f"  [{mark}] {condition.condition_id}：{condition.detail}")
    for decision in report.decisions:
        if decision.accepted:
            print(f"  决策 {decision.index}：{decision.tool} -> {decision.action}")
        else:
            print(f"  决策 {decision.index}：{decision.tool} 被拒绝（{decision.rejection}）")
    print(f"结论：goal_met={summary.goal_met} completed={summary.completed}")
    print(f"用量：{summary.usage.label}")
    print(f"费用：{summary.cost.label}")
    if report.provider.is_test_double:
        print("供应商性质：测试替身（该结果不能作为 Agent 能力成绩）")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run_command(args))
    except (UnknownGoalError, ValidationError, ValueError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ReportWriteError as exc:
        print(f"报告写入失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_ERROR


__all__ = ["agent_report_path", "build_parser", "main"]
