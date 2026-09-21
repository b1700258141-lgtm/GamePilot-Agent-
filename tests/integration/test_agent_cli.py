"""Agent 命令行端到端：真实本地 HTTP 服务 + 真实命令入口。

这里的服务开真的监听端口（仅 127.0.0.1，测试结束即关闭），并且至少有一条
用例走**真正的子进程** `python -m gamepilot.agent run`：退出码、计数、
部分证据落盘与错误优先都是对外行为，必须连同网络层与进程入口一起验证，
进程内 ASGI 测试替代不了这一条。

真实模型调用**不在本文件里做**：没有密钥时它应当明确失败并只报变量名，
这本身就是一条被验证的行为（见最后两条用例）。
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# `langgraph` 属于可选的 agent extra（见 pyproject 与 README「游戏测试 Agent」）：
# 没装时整组测试明确跳过，而不是让收集阶段直接报导入错误。
pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')
import uvicorn
from fastapi import FastAPI

from gamepilot.agent.cli import main
from gamepilot.agent.provider import DEFAULT_API_KEY_ENV
from gamepilot.agent.report import EXIT_ERROR, EXIT_GAME_DEFECT, EXIT_OK
from gamepilot.lab import create_lab_app
from gamepilot.testing.cli import main as replay_main

REPO_ROOT = Path(__file__).resolve().parents[2]

# 三个目标里最短的一条脚本：攻击一次（受伤）→ 喝药（治疗）。
HEALING_SCRIPT = "attack,use_potion"
# 在这条脚本下，potion_overheal 靶场会真实触发 R-POTION-CAP。
DEFECT_SCRIPT = "attack,use_potion"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _closed_port() -> Iterator[str]:
    """产出一个确定连不上的地址：先占用端口但不 listen，连接会被拒绝。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


@contextlib.contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    """在后台线程里跑一个仅监听本机的 uvicorn，产出服务地址。"""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn 未能在 20 秒内启动")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _counting_app(profile: str = "normal") -> tuple[FastAPI, list[str]]:
    """靶场 + 请求计数：用来证明「拒绝时一个游戏请求都没发出」。"""
    hits: list[str] = []
    app = create_lab_app(profile)

    @app.middleware("http")
    async def count(request, call_next):  # type: ignore[no-untyped-def]
        hits.append(request.url.path)
        return await call_next(request)

    return app, hits


def _slow_actions_app(profile: str) -> FastAPI:
    """真实靶场，但动作接口故意慢：用来在真实 TCP 上验证单次超时。"""
    app = create_lab_app(profile)

    @app.middleware("http")
    async def slow(request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.endswith("/actions"):
            await asyncio.sleep(0.5)
        return await call_next(request)

    return app


def _run_args(base_url: str, output_dir: Path) -> list[str]:
    return [
        "run",
        "--base-url",
        base_url,
        "--goal",
        "healing",
        "--seed",
        "42",
        "--provider",
        "fake",
        "--script",
        HEALING_SCRIPT,
        "--output-dir",
        str(output_dir),
    ]


def _replay_args(base_url: str, report: Path, output_dir: Path) -> list[str]:
    return [
        "replay",
        "--base-url",
        base_url,
        "--report",
        str(report),
        "--output-dir",
        str(output_dir),
    ]


def _all_reports(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("*.json"))


def _agent_reports(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("*-agent.json"))


def _run_reports(output_dir: Path) -> list[Path]:
    """脚本格式的运行报告（schema=1.1），即可以被 replay 重跑的那一份。"""
    return [path for path in _all_reports(output_dir) if not path.stem.endswith("-agent")]


# ------------------------------------------------ 1. 真实 HTTP 上的完整闭环


def test_offline_run_and_replay_complete_over_real_http(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """离线（测试替身）跑完一条闭环，再交给既有 replay 重跑：两者都成功。"""
    with _serve(create_lab_app("normal")) as base_url:
        code = main(_run_args(base_url, tmp_path))
        output = capsys.readouterr().out

        assert code == EXIT_OK, output
        assert "测试替身" in output, "必须显式说明这是测试替身，不能当成能力成绩"
        assert "结论：goal_met=True completed=True" in output
        assert "费用：unknown" in output, "没给单价就不许写出金额"

        plain_reports = _run_reports(tmp_path)
        agent_reports = _agent_reports(tmp_path)
        assert len(plain_reports) == 1 and len(agent_reports) == 1

        # 两份报告用同一个 run_id 关联，且 Agent 报告里引用了运行报告的路径。
        agent_payload = json.loads(agent_reports[0].read_text(encoding="utf-8"))
        assert agent_payload["summary"]["exit_code"] == EXIT_OK
        assert agent_payload["provider"]["is_test_double"] is True
        assert Path(agent_payload["run_report"]["path"]).name == plain_reports[0].name
        assert agent_payload["run_report"]["run_id"] == agent_payload["run_id"]
        # 两个文件名共享同一个 run_id 前缀：跨报告可追溯。
        assert plain_reports[0].name == f"{agent_payload['run_id']}.json"

        # 重跑：换新会话逐字段比对，全部一致。
        replay_code = replay_main(_replay_args(base_url, plain_reports[0], tmp_path))
        replay_output = capsys.readouterr().out

    assert replay_code == EXIT_OK, replay_output
    assert "mismatch=0" in replay_output
    assert "not_comparable=0" in replay_output


def test_real_subprocess_entry_point_completes_over_real_http(tmp_path: Path) -> None:
    """真正的 `python -m gamepilot.agent run`：模块入口与网络层一起验证。

    只断言退出码与落盘文件，不断言控制台文本——控制台编码随平台变化，
    而退出码与报告内容是稳定的对外契约。
    """
    with _serve(create_lab_app("normal")) as base_url:
        # 两端都固定成 UTF-8：子进程输出的是中文，用平台默认编码解出乱码只是次要问题，
        # 解码失败会让读取线程抛异常并截断输出，测试结果就不再只取决于退出码与报告。
        completed = subprocess.run(
            [sys.executable, "-m", "gamepilot.agent", *_run_args(base_url, tmp_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=180,
            check=False,
        )

    assert completed.returncode == EXIT_OK, completed.stderr
    assert len(_agent_reports(tmp_path)) == 1
    assert len(_all_reports(tmp_path)) == 2


# ------------------------------------------------- 2. 缺陷与错误优先


def test_rule_failure_over_real_http_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """确认游戏违规时退出码 1：判定证据来自独立规则检查，不是模型自述。"""
    with _serve(create_lab_app("potion_overheal")) as base_url:
        code = main(_run_args(base_url, tmp_path))
        output = capsys.readouterr().out

    assert code == EXIT_GAME_DEFECT, output
    assert "R-POTION-CAP" in output
    assert "结论：goal_met=False completed=False" in output
    agent = json.loads(_agent_reports(tmp_path)[0].read_text(encoding="utf-8"))
    # 前缀里确实有一条 fail：这是「发现缺陷」而不是「没跑出结论」。
    assert agent["summary"]["exit_code"] == EXIT_GAME_DEFECT
    run_payload = json.loads(_run_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert any(case["status"] == "fail" for case in run_payload["cases"])


def test_unwritable_report_outranks_a_confirmed_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """错误优先：报告写不出去时退出码 2，即使这次运行确实发现了缺陷。

    这里刻意用会触发 R-POTION-CAP 的靶场：如果没有「错误优先」，
    退出码会是 1。一份写不出去的报告不能被当成成功，也不能被当成发现。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("这是文件，不是目录", encoding="utf-8")
    output_dir = blocked / "sub"

    with _serve(create_lab_app("potion_overheal")) as base_url:
        code = main(_run_args(base_url, output_dir))
        captured = capsys.readouterr()

    assert code == EXIT_ERROR
    assert "报告" in captured.err
    assert not output_dir.exists()


# ------------------------------------------------- 3. 拒绝路径不发请求


def test_missing_paid_flag_refuses_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没给 `--paid`：一次模型请求都不发，也**不碰游戏服务**，退出码 2。

    环境里放一个假密钥，用来证明「拒绝发生在读取密钥之前」：
    输出里只应有变量名，绝不能出现它的值。
    """
    fake_secret = "sk-cli-must-never-appear-0123456789"
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, fake_secret)
    app, hits = _counting_app()

    with _serve(app) as base_url:
        code = main(
            [
                "run",
                "--base-url",
                base_url,
                "--goal",
                "healing",
                "--seed",
                "42",
                "--output-dir",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()

    assert code == EXIT_ERROR
    assert hits == [], "未确认付费时不该向被测服务发出任何请求"
    assert _all_reports(tmp_path) == [], "拒绝时不该写出任何报告"
    assert DEFAULT_API_KEY_ENV in captured.err, "只展示变量名，便于操作者核对"
    assert fake_secret not in captured.err and fake_secret not in captured.out
    assert "--paid" in captured.err


def test_paid_without_a_key_reports_the_variable_name_only(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给了 `--paid` 但环境变量未设置：明确失败并只报变量名。

    这条就是「真实模型验收待完成」的证据：本机没有密钥，因此真实模型
    一次都没有被调用，也没有任何金额被消耗。
    """
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    app, hits = _counting_app()

    with _serve(app) as base_url:
        code = main(
            [
                "run",
                "--base-url",
                base_url,
                "--goal",
                "healing",
                "--seed",
                "42",
                "--paid",
                "--output-dir",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()

    assert code == EXIT_ERROR
    assert hits == [], "密钥缺失时同样不该发出任何请求"
    assert DEFAULT_API_KEY_ENV in captured.err
    assert _all_reports(tmp_path) == []


def test_fake_provider_without_script_is_rejected_early(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`--provider fake` 不给 `--script` 是输入错误：在建立连接之前就拒绝。"""
    app, hits = _counting_app()
    with _serve(app) as base_url:
        code = main(
            [
                "run",
                "--base-url",
                base_url,
                "--goal",
                "healing",
                "--seed",
                "42",
                "--provider",
                "fake",
                "--output-dir",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()

    assert code == EXIT_ERROR
    assert "--script" in captured.err
    assert hits == []
    assert _all_reports(tmp_path) == []


def test_unreachable_service_writes_no_run_report(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """连不上被测服务：会话没创建，因此**没有**可重跑的运行报告，任务结论仍落盘。"""
    with _closed_port() as base_url:
        code = main(_run_args(base_url, tmp_path))
        output = capsys.readouterr().out

    assert code == EXIT_ERROR
    assert "没有创建会话，因此没有可重跑的运行报告" in output
    agent = json.loads(_agent_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert agent["run_report"] is None
    assert agent["session_id"] is None
    assert agent["summary"]["stop_reason"] == "execution_error"
    assert agent["summary"]["exit_code"] == EXIT_ERROR
    assert len(_all_reports(tmp_path)) == 1, "只该写出 Agent 报告这一份"


# ------------------------------------------------- 4. 计数与部分证据


def test_action_budget_is_enforced_over_real_http(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """动作预算在真实 HTTP 上也生效：请求数不超过上限，结论是未完成。"""
    app, hits = _counting_app()
    with _serve(app) as base_url:
        code = main(
            [
                "run",
                "--base-url",
                base_url,
                "--goal",
                "healing",
                "--seed",
                "42",
                "--provider",
                "fake",
                "--script",
                "attack,attack,attack,attack,attack,attack,attack,attack",
                "--max-actions",
                "3",
                "--output-dir",
                str(tmp_path),
            ]
        )
        output = capsys.readouterr().out

    assert code == 3, output
    assert "停止原因：action_budget" in output
    action_hits = [path for path in hits if path.endswith("/actions")]
    assert len(action_hits) == 3, "超限后不得再发动作请求"
    agent = json.loads(_agent_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert agent["summary"]["actions_attempted"] == 3
    # 前缀本身没有违规，但任务未完成——两件事分开表达。
    run_payload = json.loads(_run_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert run_payload["cases"][0]["status"] == "pass"
    assert agent["summary"]["goal_met"] is False


def test_real_http_timeout_is_an_execution_error_with_partial_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """真实 TCP 上的单次超时：归为执行错误（退出码 2），部分证据仍然落盘。

    进程内 ASGITransport 会忽略超时参数，所以这条只能在真实网络上验证。
    """
    with _serve(_slow_actions_app("normal")) as base_url:
        code = main(
            [
                "run",
                "--base-url",
                base_url,
                "--goal",
                "healing",
                "--seed",
                "42",
                "--provider",
                "fake",
                "--script",
                HEALING_SCRIPT,
                "--http-timeout",
                "0.05",
                "--output-dir",
                str(tmp_path),
            ]
        )
        output = capsys.readouterr().out

    assert code == EXIT_ERROR, output
    assert "停止原因：execution_error" in output
    assert "timeout" in output
    agent = json.loads(_agent_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert agent["summary"]["rule_failures"] == [], "超时不得被记成规则缺陷"
    # 部分证据：会话与那次失败的观测都留在运行报告里。
    run_payload = json.loads(_run_reports(tmp_path)[0].read_text(encoding="utf-8"))
    assert run_payload["cases"][0]["status"] == "error"
    assert run_payload["cases"][0]["executed_actions"]
