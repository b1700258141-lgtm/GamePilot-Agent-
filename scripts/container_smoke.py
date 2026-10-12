"""验证指定本地 Compose 实例；创建两场战斗并重启 api/db，保留所有数据。"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="专用于本次验证的 Compose 项目名")
    parser.add_argument("--base-url", required=True, help="已启动的本地服务地址")
    parser.add_argument("--output-dir", default="artifacts/container-smoke")
    args = parser.parse_args()
    parts = urlsplit(args.base_url)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost"}
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        parser.error("base-url 必须是无凭据、路径或查询串的本地 HTTP 地址")
    docker = shutil.which("docker")
    if docker is None:
        parser.error("找不到 Docker 命令")
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["GAMEPILOT_API_PORT"] = str(parts.port or 80)
    if os.name == "nt":
        entries = [entry for entry in env.get("WSLENV", "").split(":") if entry]
        if not any(entry.split("/")[0] == "GAMEPILOT_API_PORT" for entry in entries):
            entries.append("GAMEPILOT_API_PORT/u")
        env["WSLENV"] = ":".join(entries)
    # 相对配置路径兼容 Windows 原生 Docker 和通过 WSL 转发的 docker.cmd。
    compose = [docker, "compose", "-f", "compose.local.yaml", "-p", args.project]
    opener = build_opener(ProxyHandler({}))
    base = args.base_url.rstrip("/")
    result = {"project": args.project, "base_url": base, "checks": [], "exit_code": 2}

    def request(path: str, body: dict | None = None) -> dict:
        req = Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=3) as response:
            return json.load(response)

    def wait_read(path: str) -> dict:
        deadline = time.monotonic() + 45
        while True:
            try:
                return request(path)
            except (URLError, TimeoutError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def operation(*command: str) -> None:
        subprocess.run([*compose, *command], cwd=root, env=env, check=True, timeout=120)

    def require(condition: bool) -> None:
        if not condition:
            raise AssertionError("container state does not match expected evidence")

    try:
        result["stage"] = "create_control_sessions"
        require(wait_read("/ready") == {"status": "ready"})
        actual = request("/api/v1/game-sessions", {"seed": 42})
        control = request("/api/v1/game-sessions", {"seed": 42})
        path = f"/api/v1/game-sessions/{actual['session_id']}"
        control_path = f"/api/v1/game-sessions/{control['session_id']}"
        first = request(path + "/actions", {"action": "attack"})
        request(control_path + "/actions", {"action": "attack"})
        expected = request(control_path + "/actions", {"action": "attack"})
        result["session_id"] = actual["session_id"]
        for service in ("api", "db"):
            result["stage"] = f"{service}_restart"
            operation("restart", service)
            require(wait_read(path) == first)
            result["checks"].append(f"{service}_restart_restores_snapshot")
        result["stage"] = "api_recreate"
        operation("up", "-d", "--no-deps", "--no-build", "--force-recreate", "api")
        require(wait_read(path) == first)
        result["checks"].append("api_recreate_restores_snapshot")
        result["stage"] = "resume_action"
        resumed = request(path + "/actions", {"action": "attack"})
        require(
            {k: v for k, v in resumed.items() if k != "session_id"}
            == {k: v for k, v in expected.items() if k != "session_id"}
        )
        result["checks"].append("next_action_matches_uninterrupted_control")
        result["stage"] = "complete"
        result["exit_code"] = 0
    except Exception as exc:  # noqa: BLE001 - 验收错误必须留下失败记录
        result["error_kind"] = type(exc).__name__
    output = Path(args.output_dir) / f"smoke-{uuid.uuid4().hex}.json"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"验收报告写入失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"容器验收 exit={result['exit_code']}；报告：{output}")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
