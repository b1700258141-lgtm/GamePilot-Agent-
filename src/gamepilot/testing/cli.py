"""命令行入口：`run` 执行场景并判定，`replay` 在新会话中重跑并比对。

退出码：

- 0：全部通过 / 全部一致；
- 1：规则失败或重跑差异；
- 2：输入错误或执行错误（两者同时出现时以 2 为准）。

目标地址只来自本次命令的 `--base-url`：报告里记录的地址不参与，也不会被采用。
报告中的任何内容都不会被当成命令执行。远端会话不会被删除，也不会清理任何数据库。
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from .client import GameClient
from .models import CaseReport, ReplayReport, RunReport
from .replay import ReplayInputError, load_report, replay_report
from .rules import DEFAULT_TIMEOUT_SECONDS
from .runner import ReportWriteError, run_suite, write_report
from .scenarios import SUITE_BASELINE, SUITES

EXIT_OK = 0
EXIT_RULE_FAILURE = 1
EXIT_INPUT_OR_EXECUTION_ERROR = 2

DEFAULT_OUTPUT_DIR = "artifacts/test-runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gamepilot.testing",
        description="确定性测试执行器与独立规则判定基线（不使用 LLM、不重试动作）。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行内置场景套件并输出证据报告")
    _add_common_arguments(run_parser)
    run_parser.add_argument(
        "--suite",
        default=SUITE_BASELINE,
        choices=sorted(SUITES),
        help=f"场景套件名（默认 {SUITE_BASELINE}）",
    )

    replay_parser = subparsers.add_parser("replay", help="在新会话中重跑报告并逐字段比对")
    _add_common_arguments(replay_parser)
    replay_parser.add_argument("--report", required=True, help="要重跑的报告文件")
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True, help="被测服务地址，如 http://127.0.0.1:8000")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"报告输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"单次请求超时秒数（默认 {DEFAULT_TIMEOUT_SECONDS}）",
    )


def _open_client() -> httpx.AsyncClient:
    """建立 HTTP 客户端。

    `trust_env=False`：目标地址只能来自本次命令，不允许环境变量里的代理
    （HTTP_PROXY 等）把请求转到别处；判定与报告应当与运行环境无关。
    """
    return httpx.AsyncClient(trust_env=False)


async def _run_command(args: argparse.Namespace) -> int:
    async with _open_client() as http:
        client = GameClient(http, base_url=args.base_url, timeout=args.timeout)
        print(f"执行套件 {args.suite} -> {client.base_url}")
        report = await run_suite(client, args.suite)
    for case in report.cases:
        _print_case_line(case)
    path = _write(report, args.output_dir)
    summary = report.summary
    print(
        f"run 摘要：pass={summary.passed} fail={summary.failed} error={summary.errored}"
        f"（共 {summary.total} 个场景）"
    )
    print(f"报告：{path}")
    if summary.errored:
        return EXIT_INPUT_OR_EXECUTION_ERROR
    if summary.failed:
        return EXIT_RULE_FAILURE
    return EXIT_OK


async def _replay_command(args: argparse.Namespace) -> int:
    report = load_report(args.report)
    print(f"重跑报告 {args.report}（原 run_id={report.run_id}）")
    async with _open_client() as http:
        client = GameClient(http, base_url=args.base_url, timeout=args.timeout)
        print(f"目标地址（本次命令提供）：{client.base_url}")
        replayed = await replay_report(client, report, source_report=str(args.report))
    for case in replayed.cases:
        print(f"  [{case.outcome}] {case.case_id}：{case.note}")
        for difference in case.differences[:5]:
            where = "" if difference.step is None else f"（第 {difference.step} 步）"
            print(f"      差异{where} {difference.field}")
            print(f"        记录={difference.recorded}")
            print(f"        重跑={difference.replayed}")
        if len(case.differences) > 5:
            print(f"      其余 {len(case.differences) - 5} 处差异见报告")
    path = _write(replayed, args.output_dir)
    summary = replayed.summary
    print(
        f"replay 摘要：match={summary.matched} mismatch={summary.mismatched}"
        f" not_comparable={summary.not_comparable}（共 {summary.total} 个场景）"
    )
    print(f"报告：{path}")
    if summary.not_comparable:
        return EXIT_INPUT_OR_EXECUTION_ERROR
    if summary.mismatched:
        return EXIT_RULE_FAILURE
    return EXIT_OK


def _write(report: RunReport | ReplayReport, output_dir: str | Path) -> Path:
    return write_report(report, output_dir)


def _print_case_line(case: CaseReport) -> None:
    detail = ""
    if case.failure is not None:
        rule = f" {case.failure.rule_id}" if case.failure.rule_id else ""
        step = "" if case.failure.step is None else f" 第 {case.failure.step} 步"
        detail = f" ->{rule}{step} {case.failure.message}"
    elif case.error is not None:
        detail = f" -> {case.error}"
    print(f"  [{case.status}] {case.case_id}（seed={case.seed}）{detail}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run_command(args))
        return asyncio.run(_replay_command(args))
    except ReplayInputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT_OR_EXECUTION_ERROR
    except ReportWriteError as exc:
        print(f"报告写入失败：{exc}", file=sys.stderr)
        return EXIT_INPUT_OR_EXECUTION_ERROR
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return EXIT_INPUT_OR_EXECUTION_ERROR


__all__ = ["main", "build_parser"]
