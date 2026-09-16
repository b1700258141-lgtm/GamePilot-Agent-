"""Pytest 全局配置。

集成测试使用 @pytest.mark.anyio 标记异步测试。anyio 的 pytest 插件默认会在
所有已安装的后端上各跑一遍；这里固定为 asyncio，避免安装 trio 后测试重复执行。
"""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """限定异步测试只在 asyncio 后端运行。"""
    return "asyncio"
