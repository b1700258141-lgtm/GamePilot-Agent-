"""Pytest 全局配置。

集成测试使用 @pytest.mark.anyio 标记异步测试。anyio 的 pytest 插件默认会在
所有已安装的后端上各跑一遍；这里固定为 asyncio，避免安装 trio 后测试重复执行。

`postgres` 标记集中在根 conftest 决定跳过策略：未显式提供 TEST_DATABASE_URL
时跳过并说明原因；已提供时一律不跳过——连接失败或未迁移都属于失败
（见 tests/integration/conftest.py 的夹具）。
"""

import os
from collections.abc import Sequence

import pytest

POSTGRES_SKIP_REASON = (
    "PostgreSQL 集成测试已跳过：未设置 TEST_DATABASE_URL。"
    "请先创建专用测试库并显式提供该变量（见 README「PostgreSQL 集成测试」）。"
)


def pytest_collection_modifyitems(config: pytest.Config, items: Sequence[pytest.Item]) -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    skip_marker = pytest.mark.skip(reason=POSTGRES_SKIP_REASON)
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture
def anyio_backend() -> str:
    """限定异步测试只在 asyncio 后端运行。"""
    return "asyncio"
