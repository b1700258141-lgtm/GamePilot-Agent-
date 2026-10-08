"""真实 SDK + 内存 HTTP 响应，验证兼容层解析；不访问模型服务。"""

import json

import pytest

from gamepilot.agent.models import ChatMessage
from gamepilot.agent.provider import AnthropicCompatibleProvider
from gamepilot.agent.tools import TOOL_SCHEMAS

anthropic = pytest.importorskip("anthropic")
# 测试运输层须使用 SDK 自身的 HTTP 包；新版 SDK 已切换为 httpx2。
from anthropic import _base_client  # noqa: E402 - 可选依赖通过检查后再导入

httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("thinking", ["provider-default", "disabled"])
@pytest.mark.anyio
async def test_sdk_preserves_safe_block_diagnostics_and_tools(
    with_tool: bool, thinking: str
) -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        blocks = [
            {"type": "thinking", "thinking": "private-reasoning-marker", "signature": "private"},
            {"type": "redacted_thinking", "data": "private-redacted-marker"},
            {"type": "provider-secret-block", "value": "private-block-marker"},
        ]
        if with_tool:
            blocks.extend(
                [
                    {"type": "text", "text": "visible-text"},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "perform_action",
                        "input": {"action": "attack"},
                    },
                ]
            )
        return httpx.Response(
            200,
            json={
                "id": "mock-message",
                "type": "message",
                "role": "assistant",
                "model": "deepseek-v4-flash",
                "content": blocks,
                "stop_reason": "tool_use" if with_tool else "max_tokens",
                "stop_sequence": None,
                "usage": {"input_tokens": 158, "output_tokens": 512},
            },
        )

    async with anthropic.AsyncAnthropic(
        api_key="test-only",
        base_url="https://provider.invalid/anthropic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        provider = AnthropicCompatibleProvider(api_key="test-only", client=sdk, thinking=thinking)
        reply = await provider.complete(
            [ChatMessage(role="system", content="rules"), ChatMessage(role="user", content="go")],
            tools=TOOL_SCHEMAS,
            max_output_tokens=512,
            timeout=30,
        )

    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 512
    assert requests[0]["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert requests[0]["system"] == "rules"
    assert requests[0]["messages"] == [{"role": "user", "content": "go"}]
    assert provider.sampling["thinking"] == thinking
    assert provider.sampling["max_retries"] == 0
    if thinking == "disabled":
        assert requests[0]["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in requests[0]
    assert not reply.failed
    assert reply.usage.total_tokens == 670
    assert reply.response_block_types == ["thinking", "redacted_thinking", "other"] + (
        ["text", "tool_use"] if with_tool else []
    )
    assert reply.text == ("visible-text" if with_tool else None)
    assert reply.stop_reason == ("tool_use" if with_tool else "max_tokens")
    assert len(reply.tool_calls) == int(with_tool)
    if with_tool:
        assert reply.tool_calls[0].arguments == {"action": "attack"}
    assert "private" not in str((reply.text, reply.tool_calls, reply.response_block_types))
