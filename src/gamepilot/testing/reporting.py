"""报告产物的公共能力：时间、run_id、排他写入、环境快照与单场景报告装配。

这些能力由脚本执行器、重跑器和 Agent 报告共用，所以单独成模块。
放在 `runner` 里是不行的：`runner` 会导入内置场景数据（`scenarios`），
而 Agent 不允许沿导入链读到脚本答案，它只能复用这个不含场景的模块。
`single_case_report` 同理放在这里：它把一份 `CaseReport` 封成 `RunReport`，
不需要任何场景数据，而 Agent 必须能装配自己的运行报告。

本模块只依赖 `models`、`client` 与 `rules`，不导入 `scenarios`，
也不导入本包内任何会间接拉入场景的模块。
"""

import platform
import re
import secrets
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .client import GameClient
from .models import CaseReport, ReplayReport, RunEnvironment, RunReport, RunSummary
from .rules import RULES_SOURCE, RULES_VERSION, SCHEMA_VERSION

# run_id 会被用作文件名：只允许时间戳与短随机后缀，外部文本永远不进路径。
RUN_ID_PATTERN = re.compile(r"^[0-9A-Za-z]{8,32}-[0-9a-f]{6}$")


class ReportWriteError(Exception):
    """报告写入失败（例如目标文件已存在）。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def report_path(output_dir: str | Path, run_id: str) -> Path:
    if not RUN_ID_PATTERN.match(run_id):
        raise ReportWriteError(f"非法的 run_id，拒绝用作文件名：{run_id!r}")
    return Path(output_dir) / f"{run_id}.json"


def write_report(report: RunReport | ReplayReport, output_dir: str | Path) -> Path:
    """写出报告；目标文件已存在时直接失败，不覆盖已有证据。"""
    path = report_path(output_dir, report.run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 排他创建：检查与写入之间也不能覆盖另一份证据。
        with path.open("x", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
    except OSError as exc:
        raise ReportWriteError(f"无法写入报告 {path}：{type(exc).__name__}") from exc
    return path


def single_case_report(case: CaseReport, client: GameClient, run_id: str, suite: str) -> RunReport:
    """把单场景的完整证据包成一份可被既有重跑器读取的列表报告。

    供 Agent 生成的动态场景使用：两者产出同一套 schema=1.1 的 `RunReport`，
    因此 Agent 的轨迹能被原有 `replay` 读取与重跑，不需要另一条读取路径。
    """
    return RunReport(
        schema_version=SCHEMA_VERSION,
        rules_version=RULES_VERSION,
        rules_source=RULES_SOURCE,
        mode="run",
        run_id=run_id,
        suite=suite,
        base_url=client.base_url,
        started_at=case.started_at,
        finished_at=case.finished_at,
        duration_ms=case.duration_ms,
        timeout_seconds=client.timeout,
        environment=current_environment(),
        summary=RunSummary(
            passed=int(case.status == "pass"),
            failed=int(case.status == "fail"),
            errored=int(case.status == "error"),
        ),
        cases=[case],
    )


def current_environment() -> RunEnvironment:
    try:
        gamepilot_version = version("gamepilot")
    except PackageNotFoundError:  # 源码目录直接运行时可能没有元数据
        gamepilot_version = "unknown"
    return RunEnvironment(
        python_version=platform.python_version(),
        platform=platform.platform(),
        gamepilot_version=gamepilot_version,
    )


__all__ = [
    "RUN_ID_PATTERN",
    "ReportWriteError",
    "current_environment",
    "new_run_id",
    "report_path",
    "single_case_report",
    "utc_now_iso",
    "write_report",
]
