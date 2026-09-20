"""命令行入口：`python -m gamepilot.benchmark run --output-dir ...`。

退出码（与任务单第 8 节一致）：

- 0：全部符合评测预期，包含「成功发现预植入缺陷」——缺陷被稳定检出属于预期结果；
- 1：漏检、误报、触发不符或重跑差异；
- 2：输入/配置、执行或报告 I/O 错误；与 1 同时出现时以 2 为准。

评测在进程内组装四个隔离的靶场应用，不连接也不依赖任何外部服务，
不会启动、连接或清理开发数据库，也不会删除任何会话。
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from gamepilot.testing.replay import ReplayInputError
from gamepilot.testing.rules import DEFAULT_TIMEOUT_SECONDS
from gamepilot.testing.runner import ReportWriteError

from .models import BenchmarkReport, CombinationEvidence
from .runner import EXIT_EXECUTION_ERROR, BenchmarkExecutionError, run_benchmark

DEFAULT_OUTPUT_DIR = "artifacts/benchmarks"

_STATUS_LABEL = {
    "ok": "符合预期",
    "deviation": "存在偏差",
    "execution_error": "执行错误",
}


def build_parser() -> argparse.ArgumentParser:
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_EXECUTION_ERROR
    except Exception as exc:  # noqa: BLE001 - 未跑出结论时一律按执行错误退出，绝不谎报 0
        print(f"评测异常终止：{type(exc).__name__}：{exc}", file=sys.stderr)
        return EXIT_EXECUTION_ERROR


__all__ = ["build_parser", "main"]
