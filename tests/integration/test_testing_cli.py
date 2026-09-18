"""CLI 端到端测试：真实本地 HTTP 服务 + 真实命令入口。

这里的 CLI 用例开真的监听端口（仅 127.0.0.1，测试结束即关闭）：
退出码是被验收的对外行为，必须连同网络层一起验证，
否则「命令能跑」和「命令在真实 HTTP 上能跑」会是两回事。

缺陷服务只存在于测试代码里：它返回一个初始 HP 超上限的固定快照，
用来验证「规则不符 → 退出码 1」。真实游戏的战斗规则没有被改动。
"""

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from gamepilot.main import create_app
from gamepilot.repositories.memory import InMemorySessionRepository
from gamepilot.testing.cli import (
    EXIT_INPUT_OR_EXECUTION_ERROR,
    EXIT_OK,
    EXIT_RULE_FAILURE,
    main,
)
from gamepilot.testing.scenarios import BASELINE_SCENARIOS, SUITE_BASELINE

SCENARIO_COUNT = len(BASELINE_SCENARIOS)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _closed_port() -> Iterator[str]:
    """产出一个确定连不上的地址。

    直接写死「某个没人用的端口号」不可靠：本机上就可能有服务监听在上面
    （例如 127.0.0.1:1 会返回 502）。这里先占用一个端口且不调用 listen，
    连接会被拒绝，同时别的进程也抢不走它。
    """
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


def _healthy_app() -> FastAPI:
    return create_app(InMemorySessionRepository())


def _defective_app() -> FastAPI:
    """构造的缺陷服务：玩家初始 HP 120 超过上限 100。

    除此之外一切正常，因此它只会触发规则失败（退出码 1），
    而不是解析失败或连接失败（退出码 2）。
    """
    snapshot = {
        "session_id": "constructed-session",
        "seed": 42,
        "status": "active",
        "turn": 0,
        "player": {"hp": 120, "max_hp": 100, "potions": 2},
        "slime": {"hp": 60, "max_hp": 60},
        "events": [],
    }
    app = FastAPI()

    @app.post("/api/v1/game-sessions", status_code=201)
    async def create_session(payload: dict) -> dict:
        return {**snapshot, "seed": payload["seed"]}

    @app.get("/api/v1/game-sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return snapshot

    return app


def _run_args(base_url: str, tmp_path: Path) -> list[str]:
    return [
        "run",
        "--base-url",
        base_url,
        "--suite",
        SUITE_BASELINE,
        "--output-dir",
        str(tmp_path),
    ]


def _replay_args(base_url: str, report: Path, tmp_path: Path) -> list[str]:
    return [
        "replay",
        "--base-url",
        base_url,
        "--report",
        str(report),
        "--output-dir",
        str(tmp_path),
    ]


def test_run_exits_0_and_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with _serve(_healthy_app()) as base_url:
        code = main(_run_args(base_url, tmp_path))

    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert f"pass={SCENARIO_COUNT} fail=0 error=0" in output
    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    assert reports[0].stem in output


def test_replay_exits_0_when_everything_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    with _serve(_healthy_app()) as base_url:
        assert main(_run_args(base_url, tmp_path)) == EXIT_OK
        run_report = next(tmp_path.glob("*.json"))
        code = main(_replay_args(base_url, run_report, tmp_path))

    output = capsys.readouterr().out
    assert code == EXIT_OK
    assert f"match={SCENARIO_COUNT} mismatch=0 not_comparable=0" in output
    # 重跑报告是独立文件，不覆盖原报告。
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_replay_exits_2_for_unsupported_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    with _serve(_healthy_app()) as base_url:
        assert main(_run_args(base_url, tmp_path)) == EXIT_OK
        run_report = next(tmp_path.glob("*.json"))
        payload = json.loads(run_report.read_text(encoding="utf-8"))
        payload["schema_version"] = "9.9"
        run_report.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code = main(_replay_args(base_url, run_report, tmp_path))

    assert code == EXIT_INPUT_OR_EXECUTION_ERROR
    assert "schema_version='9.9'" in capsys.readouterr().err


def test_run_exits_1_when_a_rule_is_violated(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with _serve(_defective_app()) as base_url:
        code = main(_run_args(base_url, tmp_path))

    output = capsys.readouterr().out
    assert code == EXIT_RULE_FAILURE
    assert f"fail={SCENARIO_COUNT} error=0" in output
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    failures = {case["failure"]["rule_id"] for case in report["cases"]}
    assert failures == {"R-INIT-STATE"}
    assert all(case["status"] == "fail" for case in report["cases"])


def test_replay_exits_2_for_missing_report(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """输入错误在建立 HTTP 客户端之前就被拒绝（地址是坏地址也不会去连）。"""
    code = main(_replay_args("http://127.0.0.1:1", tmp_path / "missing.json", tmp_path))

    assert code == EXIT_INPUT_OR_EXECUTION_ERROR
    assert "输入错误" in capsys.readouterr().err


def test_run_exits_2_when_service_is_unreachable(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """连不上时全部场景记 error（不是 fail），退出码 2，且报告仍然落盘。"""
    with _closed_port() as base_url:
        code = main(_run_args(base_url, tmp_path))

    output = capsys.readouterr().out
    assert code == EXIT_INPUT_OR_EXECUTION_ERROR
    assert f"pass=0 fail=0 error={SCENARIO_COUNT}" in output
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert all(case["status"] == "error" for case in report["cases"])
    assert {case["create_observation"]["error_kind"] for case in report["cases"]} == {"connection"}


def test_run_exits_2_when_requests_time_out(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """超时同样属于执行错误：结果未知，不能记成规则缺陷。"""
    with _serve(_healthy_app()) as base_url:
        args = _run_args(base_url, tmp_path)
        args += ["--timeout", "0.000001"]
        code = main(args)

    output = capsys.readouterr().out
    assert code == EXIT_INPUT_OR_EXECUTION_ERROR
    assert f"error={SCENARIO_COUNT}" in output


def test_run_exits_2_for_unknown_suite(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "run",
                "--base-url",
                "http://127.0.0.1:1",
                "--suite",
                "no-such-suite",
                "--output-dir",
                str(tmp_path),
            ]
        )

    assert error.value.code == EXIT_INPUT_OR_EXECUTION_ERROR


def test_run_requires_base_url() -> None:
    with pytest.raises(SystemExit) as error:
        main(["run"])

    assert error.value.code == EXIT_INPUT_OR_EXECUTION_ERROR


def test_output_directory_is_a_file_returns_2_without_overwrite(tmp_path, capsys):
    output_file = tmp_path / "existing-file"
    output_file.write_text("keep", encoding="utf-8")
    with _serve(_healthy_app()) as base_url:
        assert main(_run_args(base_url, output_file)) == EXIT_INPUT_OR_EXECUTION_ERROR
    captured = capsys.readouterr()
    assert "报告写入失败" in captured.err
    assert "报告：" not in captured.out
    assert output_file.read_text(encoding="utf-8") == "keep"
