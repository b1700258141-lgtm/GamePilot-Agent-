"""命令行入口：`python -m gamepilot.benchmark run --output-dir ...`。

退出码（与任务单第 8 节一致）：

- 0：全部符合评测预期，包含「成功发现预植入缺陷」——缺陷被稳定检出属于预期结果；
- 1：漏检、误报、触发不符或重跑差异；
- 2：输入/配置、执行或报告 I/O 错误；与 1 同时出现时以 2 为准。

评测在进程内组装四个隔离的靶场应用，不连接也不依赖任何外部服务，
不会启动、连接或清理开发数据库，也不会删除任何会话。

**注意 `agent` 子命令的退出码含义不同**：Agent 的一条闭环有自己的对外契约
（0 目标达成 / 1 确认游戏违规 / 2 执行错误 / 3 目标未完成，见
`gamepilot.agent.report.derive_exit_code`）。本入口一律返回**评测层**的含义：
0 = 12 格无偏差，1 = 出现偏差，2 = 执行错误，不转发单格的退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from gamepilot.testing.replay import ReplayInputError
from gamepilot.testing.rules import DEFAULT_TIMEOUT_SECONDS
from gamepilot.testing.runner import ReportWriteError

from .models import BenchmarkReport, CombinationEvidence
from .runner import EXIT_EXECUTION_ERROR, BenchmarkExecutionError, run_benchmark

if TYPE_CHECKING:  # 只在类型检查时需要：运行期不导入 agent extra（也不导入 langgraph）
    from gamepilot.agent.models import BudgetSpec
    from gamepilot.agent.provider import ModelProvider

    from .agent_models import AgentCellEvidence, AgentEvalReport, AgentPricing

DEFAULT_OUTPUT_DIR = "artifacts/benchmarks"
DEFAULT_AGENT_OUTPUT_DIR = "artifacts/agent-benchmarks"

AGENT_PROVIDER_OFFLINE = "offline"
AGENT_PROVIDER_REAL = "anthropic-compatible"

_STATUS_LABEL = {
    "ok": "符合预期",
    "deviation": "存在偏差",
    "execution_error": "执行错误",
}


def build_parser() -> argparse.ArgumentParser:
    from .agent_manifest import AGENT_EVAL_BUDGET

    parser = argparse.ArgumentParser(
        prog="python -m gamepilot.benchmark",
        description="固定缺陷矩阵评测：组装隔离靶场、跑 baseline 组合并同 profile 重跑。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行固定评测矩阵并输出评测摘要")
    run_parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"评测输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT_SECONDS}）",
    )

    agent_parser = subparsers.add_parser(
        "agent",
        help="运行 4 profile × 3 目标的 12 格 Agent 配对试验（退出码含义与 run 不同，见模块说明）",
    )
    agent_parser.add_argument(
        "--output-dir",
        default=DEFAULT_AGENT_OUTPUT_DIR,
        help=f"评测输出目录（默认 {DEFAULT_AGENT_OUTPUT_DIR}）",
    )
    agent_parser.add_argument(
        "--provider",
        default=AGENT_PROVIDER_OFFLINE,
        choices=sorted((AGENT_PROVIDER_OFFLINE, AGENT_PROVIDER_REAL)),
        help=(
            f"供应商（默认 {AGENT_PROVIDER_OFFLINE} 测试替身）；"
            f"{AGENT_PROVIDER_REAL} 需要 --paid 且从环境变量读密钥"
        ),
    )
    agent_parser.add_argument(
        "--paid",
        action="store_true",
        help="确认允许付费的真实模型调用；不给出时不会发出任何模型请求",
    )
    agent_parser.add_argument("--model", default=None, help="模型名（仅真实供应商使用）")
    agent_parser.add_argument(
        "--model-base-url", default=None, help="模型入口地址（仅真实供应商使用）"
    )
    agent_parser.add_argument(
        "--api-key-env",
        default=None,
        help="存放密钥的**环境变量名**；不接受密钥本身，输出中只出现变量名",
    )
    agent_parser.add_argument(
        "--http-timeout",
        "--timeout",
        dest="http_timeout",
        type=float,
        default=AGENT_EVAL_BUDGET.http_timeout_seconds,
        help=(
            "单次游戏 HTTP 请求超时秒数"
            f"（默认 {AGENT_EVAL_BUDGET.http_timeout_seconds}；--timeout 为兼容别名）"
        ),
    )
    agent_parser.add_argument(
        "--max-actions",
        type=int,
        default=AGENT_EVAL_BUDGET.max_action_attempts,
        help="每格动作尝试上限",
    )
    agent_parser.add_argument(
        "--max-model-calls",
        type=int,
        default=AGENT_EVAL_BUDGET.max_model_calls,
        help="每格模型调用上限",
    )
    agent_parser.add_argument(
        "--max-format-retries",
        type=int,
        default=AGENT_EVAL_BUDGET.max_format_retries,
        help="每格格式纠正上限",
    )
    agent_parser.add_argument(
        "--model-timeout",
        type=float,
        default=AGENT_EVAL_BUDGET.model_timeout_seconds,
        help="单次模型调用超时秒数",
    )
    agent_parser.add_argument(
        "--total-timeout",
        type=float,
        default=AGENT_EVAL_BUDGET.total_timeout_seconds,
        help="每格 Agent 总时限秒数",
    )
    agent_parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=AGENT_EVAL_BUDGET.max_output_tokens,
        help="每次模型调用的输出 Token 上限",
    )
    agent_parser.add_argument(
        "--input-price", type=float, default=None, help="每百万输入 Token 的显式单价"
    )
    agent_parser.add_argument(
        "--output-price", type=float, default=None, help="每百万输出 Token 的显式单价"
    )
    agent_parser.add_argument(
        "--currency", default=None, help="计价币种；与输入、输出单价必须同时给出"
    )
    return parser


def _marks(item: CombinationEvidence) -> str:
    """一行标记：T=真实触发，U=意外触发，!=与清单不符。"""
    return "".join(
        (
            "T" if item.triggered else "-",
            "U" if item.unexpected_trigger else "-",
            "!" if not item.matrix_ok else " ",
        )
    )


def _print_combination(item: CombinationEvidence) -> None:
    expected = "fail" if item.expect_fail else "pass"
    detail = "" if item.matrix_ok else f" -> {'；'.join(item.notes)}"
    print(
        f"  [{_marks(item)}] {item.profile} x {item.case_id}："
        f"预期 {expected}，实际 {item.case_status}，"
        f"触发 {item.trigger_detail or '无'}，重跑 {item.replay_outcome}{detail}"
    )
    if item.replay_status == "error":
        print(f"      重放执行错误：{item.replay_error}；证据：{item.replay_path}")


def _print_metrics(report: BenchmarkReport) -> None:
    print("指标：")
    for metric in report.metrics:
        mark = "达标" if metric.passed else "未达标"
        print(
            f"  {metric.metric_id}：{metric.numerator}/{metric.denominator}"
            f"（目标 {metric.target}）{mark} - {metric.description}"
        )
        for line in metric.details:
            print(f"      {line}")


def _print_negative_validation(report: BenchmarkReport) -> None:
    negative = report.negative_validation
    if negative is None:
        print("负向验证：未执行（摘要缺少该部分）")
        return
    mark = "通过" if negative.passed else "不通过"
    print(
        f"负向验证（不计入 32 次重跑）：{mark} - {negative.source_profile} x "
        f"{negative.source_case_id} 在 normal 上重放，结果为 {negative.replay_outcome}"
    )
    print(f"      {negative.note}")
    if negative.execution_status == "error":
        print(f"      错误证据：{negative.replay_path}")


def _render(report: BenchmarkReport, output_dir: str) -> None:
    summary = report.summary
    print(f"评测运行 {report.run_id}（benchmark {report.benchmark_version}，套件 {report.suite}）")
    print(f"输出目录：{output_dir}")
    print(
        f"组合：计划 {summary.combinations_planned}，执行 {summary.combinations_executed}，"
        f"符合清单 {summary.as_expected}，偏差 {summary.deviations}，"
        f"原始运行执行错误 {summary.execution_errors}"
    )
    print(
        f"分阶段执行错误：原始运行 {summary.execution_errors}/{summary.combinations_planned}，"
        f"同 profile 重放 {summary.replay_execution_errors}/{summary.combinations_planned}，"
        f"负向验证 {summary.negative_execution_errors}/1"
    )
    print("逐组合结果（T=真实触发，U=意外触发，!=不符清单）：")
    current = None
    for item in report.combinations:
        if item.profile != current:
            current = item.profile
            print(f"[{current}] 靶场：{report.profile_map.get(current, '未记录该 profile')}")
        _print_combination(item)
    _print_metrics(report)
    _print_negative_validation(report)
    print(
        f"评测摘要：{_STATUS_LABEL[summary.status]}"
        f"（指标 {summary.metrics_passed}/{summary.metrics_total}，退出码 {summary.exit_code}）"
    )


async def _run_command(args: argparse.Namespace) -> int:
    report = await run_benchmark(args.output_dir, timeout=args.timeout)
    _render(report, args.output_dir)
    print(f"摘要：{args.output_dir}/{report.run_id}/benchmark.json")
    return report.summary.exit_code


# ------------------------------------------------------------- agent 子命令


def _agent_budget(args: argparse.Namespace) -> BudgetSpec:
    from gamepilot.agent.models import BudgetSpec

    return BudgetSpec(
        max_action_attempts=args.max_actions,
        max_model_calls=args.max_model_calls,
        max_format_retries=args.max_format_retries,
        model_timeout_seconds=args.model_timeout,
        http_timeout_seconds=args.http_timeout,
        total_timeout_seconds=args.total_timeout,
        max_output_tokens=args.max_output_tokens,
    )


def _agent_pricing(args: argparse.Namespace) -> AgentPricing | None:
    from .agent_models import AgentPricing

    values = (args.input_price, args.output_price, args.currency)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("--input-price、--output-price 与 --currency 必须同时给出")
    return AgentPricing(
        input_price_per_million=args.input_price,
        output_price_per_million=args.output_price,
        currency=args.currency,
    )


def _make_agent_provider_factory(args: argparse.Namespace) -> Callable[[str], ModelProvider]:
    """构造 `goal_id -> ModelProvider` 的工厂。

    默认给出**离线测试替身**：本入口不会隐式发起付费调用。真实模型需要
    `--paid` 与一个已导出的密钥环境变量；这里只读环境变量名，不读密钥本身。
    """
    from gamepilot.agent.provider import (
        DEFAULT_API_KEY_ENV,
        DEFAULT_BASE_URL,
        DEFAULT_MODEL,
        AnthropicCompatibleProvider,
    )

    from .agent_eval import offline_provider_factory

    if args.provider == AGENT_PROVIDER_OFFLINE:
        return offline_provider_factory

    if not args.paid:
        raise ValueError(
            f"付费运行默认关闭：--provider {AGENT_PROVIDER_REAL} 需要同时给出 --paid；"
            "本次不会发出任何模型请求"
        )
    api_key_env = args.api_key_env or DEFAULT_API_KEY_ENV
    api_key = os.environ.get(api_key_env)
    if not api_key:
        # 只报变量名：不回显值，也不描述它的内容。
        raise ValueError(
            f"环境变量 {api_key_env} 未设置，无法调用真实模型；"
            "请先导出它（本次不会显示或记录它的值）"
        )
    # 4 profile 共用同一个供应商实例：12 格的模型配置必须完全一致。
    provider = AnthropicCompatibleProvider(
        api_key=api_key,
        model=args.model or DEFAULT_MODEL,
        base_url=args.model_base_url or DEFAULT_BASE_URL,
        api_key_env=api_key_env,
    )
    return lambda _goal_id: provider


def _print_agent_cell(cell: AgentCellEvidence) -> None:
    marks = "".join(
        (
            "T" if cell.triggered else "-",
            "D" if cell.detected else "-",
            "+" if cell.beyond_designated else "-",
        )
    )
    print(
        f"  [{marks}] {cell.profile} × {cell.goal_id}：停止 {cell.stop_reason}"
        f"（退出码 {cell.exit_code}），覆盖{'达到' if cell.goal_met else '未达到'}，"
        f"判定 {cell.case_status}，动作 {cell.spend.actions_attempted}，"
        f"模型调用 {cell.spend.model_calls}，重跑 {cell.replay_outcome}"
    )
    if cell.target_rules_missed:
        print(
            f"      未命中目标规则：{'、'.join(cell.target_rules_missed)}"
            f"（触发={cell.triggered}，停止原因={cell.stop_reason}）"
        )


def _print_agent_metrics(report: AgentEvalReport) -> None:
    print("指标（门槛项与信息项分开）：")
    for metric in report.metrics:
        mark = "达标" if metric.passed else "未达标"
        gate = "门槛" if metric.required else "信息"
        print(
            f"  [{gate}] {metric.metric_id}：{metric.ratio}"
            f"（目标 {metric.target}）{mark} - {metric.description}"
        )
        for line in metric.details:
            print(f"      {line}")


def _render_agent(report: AgentEvalReport, output_dir: str) -> None:
    summary = report.summary
    provider = report.provider
    print(
        f"Agent 评测运行 {report.run_id}（agent-benchmark {report.agent_benchmark_version}，"
        f"seed={report.seed}）"
    )
    print(f"输出目录：{output_dir}")
    print(
        f"供应商：{provider.get('provider_id')} / {provider.get('model')}"
        f"（测试替身={provider.get('is_test_double')}）"
    )
    print(f"清单来源：{report.manifest_source}")
    budget = report.budget
    print(
        "预算："
        f"动作 {budget.get('max_action_attempts')}，"
        f"模型调用 {budget.get('max_model_calls')}，"
        f"格式纠正 {budget.get('max_format_retries')}，"
        f"模型超时 {budget.get('model_timeout_seconds')}s，"
        f"HTTP 超时 {budget.get('http_timeout_seconds')}s，"
        f"总时限 {budget.get('total_timeout_seconds')}s，"
        f"输出 {budget.get('max_output_tokens')} Token"
    )
    print(
        f"格数：计划 {summary.cells_planned}，执行 {summary.cells_executed}，"
        f"目标达成 {summary.goals_met}，目标未完成 {summary.goals_incomplete}，"
        f"执行错误 {summary.execution_errors}"
    )
    print(
        f"分阶段执行错误：Agent {summary.agent_execution_errors}/{summary.cells_planned}，"
        f"重跑 {summary.replay_execution_errors}/{summary.cells_planned}，"
        f"对照 {summary.control_execution_errors}/{summary.cells_planned}"
    )
    print(
        f"预定机会：{summary.designated_detected}/{summary.designated_opportunities}，"
        f"去重缺陷 {summary.distinct_defects_detected}，"
        f"normal 误报 {summary.normal_false_positives}，"
        f"重跑一致 {summary.replay_matches}/{summary.replay_comparable} 可比，"
        f"差异 {summary.replay_mismatches}，不可比 {summary.replay_not_comparable}，"
        f"未执行 {summary.replay_not_executed}"
    )
    print(
        f"用量：已知 {summary.usage_known_cells}/{summary.cells_planned}，"
        f"未知 {summary.usage_unknown_cells}/{summary.cells_planned}，"
        f"Token {summary.usage.label}"
    )
    print(f"总费用：{summary.cost.label}（{summary.cost.note}）")
    print("逐格结果（T=真实触发，D=清单内检出，+=清单外额外检出）：")
    current = None
    for cell in report.cells:
        if cell.profile != current:
            current = cell.profile
            print(f"[{current}] 靶场：{report.profile_map.get(current, '未记录该 profile')}")
        _print_agent_cell(cell)
    _print_agent_metrics(report)
    print(
        f"Agent 评测结论：{_STATUS_LABEL[summary.status]}"
        f"（门槛指标 {summary.metrics_passed}/{summary.metrics_total}，"
        f"退出码 {summary.exit_code}）"
    )
    print(f"说明：{summary.acceptance_note}")


async def _run_agent_command(args: argparse.Namespace) -> int:
    from .agent_eval import run_agent_eval

    budget = _agent_budget(args)
    pricing = _agent_pricing(args)
    factory = _make_agent_provider_factory(args)
    report, path = await run_agent_eval(
        args.output_dir,
        provider_factory=factory,
        budget=budget,
        pricing=pricing,
    )
    _render_agent(report, args.output_dir)
    print(f"摘要：{path}")
    return report.summary.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "agent":
            return asyncio.run(_run_agent_command(args))
        return asyncio.run(_run_command(args))
    except ReplayInputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except ReportWriteError as exc:
        print(f"报告写入失败：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except BenchmarkExecutionError as exc:
        print(f"评测执行失败：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except ValueError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except Exception as exc:  # noqa: BLE001 - 未跑出结论时一律按执行错误退出，绝不谎报 0
        print(f"评测异常终止：{type(exc).__name__}：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR


__all__ = ["build_parser", "main"]
