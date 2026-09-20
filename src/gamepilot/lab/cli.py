"""命令行入口：`serve` 显式启动一个独立内存靶场。

默认只监听 `127.0.0.1`：靶场是给本机测试用的，不是对外服务。
profile 只能在这里选择，游戏 API 上没有任何读取或切换缺陷的端点。

退出码：0 正常结束；2 输入错误（非法 profile、非法端口等），
与执行器/评测命令的约定保持一致。
"""

import argparse
from collections.abc import Sequence

import uvicorn

from .app import create_lab_app
from .profiles import PROFILE_CATALOG, PROFILE_NORMAL, PROFILES

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m gamepilot.lab",
        description="可控缺陷靶场：把一个真实游戏缺陷固定装进一个独立内存服务。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动靶场服务（前台运行）")
    serve.add_argument(
        "--profile",
        default=PROFILE_NORMAL,
        choices=sorted(PROFILES),
        help=f"靶场 profile（默认 {PROFILE_NORMAL}）："
        + "；".join(f"{name}={PROFILE_CATALOG[name].summary}" for name in sorted(PROFILES)),
    )
    serve.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _serve(args)


def _serve(args: argparse.Namespace) -> int:
    profile = PROFILE_CATALOG[args.profile]
    app = create_lab_app(profile.profile_id)
    print(f"缺陷靶场启动：profile={profile.profile_id}（{profile.summary}）")
    print(f"监听 http://{args.host}:{args.port}；仅使用内存仓储，不连接数据库。")
    print("按 Ctrl+C 停止；进程退出后靶场内的会话不会保留。")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


__all__ = ["main", "build_parser"]
