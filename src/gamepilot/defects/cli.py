"""缺陷证据导出与无模型回归验证命令行。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

import httpx

from gamepilot.testing.client import GameClient
from gamepilot.testing.replay import ReplayInputError
from gamepilot.testing.reporting import ReportWriteError

from .batch import run_batch
from .logic import DefectInputError, export_defect, load_artifact, verify_defect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gamepilot.defects")
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="从已确认规则失败导出缺陷单和回归规格")
    export.add_argument("--run-report", required=True)
    export.add_argument("--replay-report", required=True)
    export.add_argument("--case-id", required=True)
    export.add_argument("--rule-id", help="同一失败步骤有多条规则时选定目标规则")
    export.add_argument("--agent-report", help="可选：关联并核对 Agent 结论")
    export.add_argument("--output-dir", default="artifacts/defects")

    verify = commands.add_parser("verify", help="在指定目标的新会话中验证回归规格")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--base-url", required=True)
    verify.add_argument("--timeout", type=float, default=5.0)
    verify.add_argument("--output-dir", default="artifacts/defect-verifications")
    batch = commands.add_parser("batch", help="按固定清单批量验证缺陷回归")
    batch.add_argument("--manifest", required=True)
    batch.add_argument("--base-url", required=True)
    batch.add_argument("--timeout", type=float, default=5.0)
    batch.add_argument("--output-dir", default="artifacts/defect-batches")
    demo = commands.add_parser("demo", help="离线生成四种缺陷的证据并验证完整回归闭环")
    demo.add_argument("--output-dir", default="artifacts/defect-demo")
    compare = commands.add_parser("compare", help="核验并比较两次批量回归结果")
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--output-dir", default="artifacts/defect-comparisons")
    return parser


async def _verify(args: argparse.Namespace) -> int:
    if not 0 < args.timeout <= 300:
        raise DefectInputError("timeout 必须在 (0, 300] 秒内")
    artifact = load_artifact(args.artifact)
    async with httpx.AsyncClient(trust_env=False) as http:
        client = GameClient(http, base_url=args.base_url, timeout=args.timeout)
        code, path, case, outcome = await verify_defect(artifact, client, args.output_dir)
    rule_ids = sorted(
        {check.rule_id for step in case.steps for check in step.checks if check.status == "fail"}
    )
    print(f"回归结果：{outcome}；目标规则={artifact.rule_id}；实测失败规则={rule_ids}")
    print(f"运行报告：{path}")
    return code


async def _batch(args: argparse.Namespace) -> int:
    if not 0 < args.timeout <= 300:
        raise DefectInputError("timeout 必须在 (0, 300] 秒内")
    # 预检必须先于 HTTP 客户端的任何请求；run_batch 会加载全部产物。
    async with httpx.AsyncClient(trust_env=False) as http:
        client = GameClient(http, base_url=args.base_url, timeout=args.timeout)
        report, path = await run_batch(args.manifest, client, args.output_dir)
    summary = report.summary
    print(
        f"批量结果：{summary.executed}/{summary.planned} 已执行，"
        f"{summary.passed} 通过，{summary.failed} 失败，"
        f"{summary.errored} 错误，{summary.skipped} 跳过"
    )
    print(f"批量报告：{path}")
    return summary.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "compare":
            from .compare import compare_batches

            report, path = compare_batches(args.before, args.after, args.output_dir)
            print(f"回归对比：{report['summary']}；报告：{path}")
            return report["summary"]["exit_code"]
        if args.command == "demo":
            from .demo import run_demo

            code, path = asyncio.run(run_demo(args.output_dir))
            print(f"离线脚本演示：exit={code}；未调用模型；汇总：{path}")
            return code
        if args.command == "export":
            artifact, path = export_defect(
                args.run_report,
                args.replay_report,
                args.case_id,
                args.output_dir,
                rule_id=args.rule_id,
                agent_path=args.agent_report,
            )
            print(f"缺陷单与回归规格：{path}")
            print(f"规则={artifact.rule_id}；指纹={artifact.fingerprint}；动作={artifact.actions}")
            return 0
        return asyncio.run(_batch(args) if args.command == "batch" else _verify(args))
    except (DefectInputError, ReplayInputError, ReportWriteError) as exc:
        print(f"输入或执行错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
