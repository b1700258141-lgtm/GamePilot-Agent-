"""模型适配层：一个最小接口，两个实现。

接口只做一件事：接收消息与工具定义，返回标准化的工具选择、用量与安全错误分类。
不做多供应商路由、不做重试、不做对话记忆——这些都不属于本任务范围。

两个实现：

- `FakeProvider`：**测试替身**，明确标记，只用于控制流程测试，
  它的结果不计入任何 Agent 能力成绩；
- `AnthropicCompatibleProvider`：唯一一家真实供应商，走 Anthropic 兼容入口
  （`https://api.deepseek.com/anthropic`）。**显式关闭 SDK 隐式重试**，
  否则应用侧的调用计数会与实际请求数不一致。

错误分类只记录异常类型名与简短原因，不记录异常文本：httpx 与 SDK 的异常消息可能
带上请求地址甚至请求头，而报告是长期保存的证据文件。
"""

from collections.abc import Callable, Sequence
from typing import Protocol

from gamepilot.testing.client import redact_url

from .models import ChatMessage, TokenUsage, ToolCallRequest
from .tools import TOOL_FINISH, TOOL_PERFORM_ACTION

# 错误分类：与 testing 的 ErrorKind 分开，因为模型侧的原因集合不同。
ModelErrorKind = str  # "timeout" | "rate_limit" | "server_error" | "connection" | "overloaded"
#              "invalid_response" | "refusal" | "unexpected_error"

PROVIDER_FAKE = "fake"
PROVIDER_ANTHROPIC = "anthropic-compatible"

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_API_KEY_ENV = "GAMEPILOT_AGENT_API_KEY"


class ModelReply:
    """一次模型调用的标准化结果。

    `error_kind` 非空时表示这次调用失败：`tool_calls` 与 `text` 都不参与后续判断，
    调用方必须按「模型错误」处理，而不是当成「模型选择了不动作」。
    """

    __slots__ = ("tool_calls", "text", "usage", "error_kind", "error_detail", "stop_reason")

    def __init__(
        self,
        *,
        tool_calls: Sequence[ToolCallRequest] = (),
        text: str | None = None,
        usage: TokenUsage | None = None,
        error_kind: str | None = None,
        error_detail: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        self.tool_calls = list(tool_calls)
        self.text = text
        self.usage = usage or TokenUsage.unknown()
        self.error_kind = error_kind
        self.error_detail = error_detail
        self.stop_reason = stop_reason

    @property
    def failed(self) -> bool:
        return self.error_kind is not None


class ModelProvider(Protocol):
    """模型供应方的最小接口。"""

    @property
    def provider_id(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def base_url(self) -> str | None: ...

    @property
    def is_test_double(self) -> bool: ...

    @property
    def sampling(self) -> dict[str, object]: ...

    @property
    def api_key_env(self) -> str | None: ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]],
        max_output_tokens: int,
        timeout: float,
    ) -> ModelReply: ...


# --------------------------------------------------------------------- 测试替身


class FakeProvider:
    """测试替身：把「下一步」交给注入的响应函数，本身不含任何模型能力。

    它显式标记 `is_test_double=True`，报告里也会写出来，
    因此 fake 运行永远不会被当成 Agent 能力成绩。

    `responder` 收到**当前完整对话**并返回下一次回复：
    测试可以据此断言「模型看到的观测变了，下一步选择也变了」，
    而不是预先录好一整条脚本。
    """

    def __init__(
        self,
        responder: Callable[[Sequence[ChatMessage]], ModelReply],
        *,
        model: str = "fake-agent-double",
    ) -> None:
        self._responder = responder
        self._model = model

    @property
    def provider_id(self) -> str:
        return PROVIDER_FAKE

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str | None:
        return None

    @property
    def is_test_double(self) -> bool:
        return True

    @property
    def sampling(self) -> dict[str, object]:
        return {"temperature": None, "note": "测试替身不采样，结果由 responder 决定"}

    @property
    def api_key_env(self) -> str | None:
        return None

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]],
        max_output_tokens: int,
        timeout: float,
    ) -> ModelReply:
        return self._responder(messages)


class ScriptedProvider(FakeProvider):
    """按序号返回预置回复的测试替身；跑完队列后返回「空输出」。

    只用于「连续格式错误」「多工具调用」这类控制路径测试。
    """

    def __init__(
        self, replies: Sequence[ModelReply], *, model: str = "fake-agent-scripted"
    ) -> None:
        self._replies = list(replies)
        self._index = 0
        super().__init__(self._next, model=model)

    def _next(self, messages: Sequence[ChatMessage]) -> ModelReply:  # noqa: ARG002 - 由协议决定
        if self._index >= len(self._replies):
            return ModelReply()
        reply = self._replies[self._index]
        self._index += 1
        return reply


def scripted_responder(steps: Sequence[str]) -> Callable[[Sequence[ChatMessage]], ModelReply]:
    """把动作序列变成测试替身的响应函数。

    `steps` 里除了 `attack` / `use_potion`，还可以出现 `finish`（申请结束）。
    序列用完之后返回空输出——那会被本地校验拒绝并计入格式纠正，
    **不会被解释成「模型选择不动作」**。这是测试替身，不是模型能力基线。
    """
    plan = list(steps)

    def respond(messages: Sequence[ChatMessage]) -> ModelReply:
        used = sum(1 for item in messages if item.role == "assistant" and item.tool_calls)
        if used >= len(plan):
            return ModelReply(text="（测试替身的动作计划已用尽）")
        step = plan[used]
        if step == TOOL_FINISH:
            return ModelReply(
                tool_calls=[
                    ToolCallRequest(
                        tool_call_id=f"fake-{used}",
                        name=TOOL_FINISH,
                        arguments={"summary": "测试替身的动作计划已执行完，申请结束。"},
                    )
                ]
            )
        return ModelReply(
            tool_calls=[
                ToolCallRequest(
                    tool_call_id=f"fake-{used}",
                    name=TOOL_PERFORM_ACTION,
                    arguments={"action": step},
                )
            ]
        )

    return respond


# ------------------------------------------------------------------- 真实供应商


class AnthropicCompatibleProvider:
    """Anthropic 兼容入口的模型适配器（当前唯一真实供应商）。

    `max_retries=0` 是硬要求：SDK 的隐式重试会让「我们记了几次调用」与
    「实际发生了几次请求」不一致，预算与用量都会失真。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        temperature: float | None = None,
        client: object | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._temperature = temperature
        self._client = client if client is not None else _build_client(api_key, base_url)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ANTHROPIC

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str | None:
        # 报告只保留脱敏地址：入口可能带查询串或用户信息。
        return redact_url(self._base_url)

    @property
    def is_test_double(self) -> bool:
        return False

    @property
    def sampling(self) -> dict[str, object]:
        sampling: dict[str, object] = {"max_retries": 0}
        if self._temperature is not None:
            sampling["temperature"] = self._temperature
        return sampling

    @property
    def api_key_env(self) -> str | None:
        return self._api_key_env

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, object]],
        max_output_tokens: int,
        timeout: float,
    ) -> ModelReply:
        system = "\n\n".join(item.content for item in messages if item.role == "system")
        conversation = [_to_api_message(item) for item in messages if item.role != "system"]
        request: dict[str, object] = {
            "model": self._model,
            "max_tokens": max_output_tokens,
            "messages": conversation,
            "tools": list(tools),
            "timeout": timeout,
        }
        if system:
            request["system"] = system
        if self._temperature is not None:
            request["temperature"] = self._temperature
        try:
            response = await self._client.messages.create(**request)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 供应商异常必须被分类，不能把运行打崩
            kind, detail = _classify(exc)
            return ModelReply(error_kind=kind, error_detail=detail)
        return _from_api_response(response)


def _build_client(api_key: str, base_url: str) -> object:
    """建立 SDK 客户端；`max_retries=0` 显式关闭隐式重试。"""
    import anthropic  # 只有真正跑真实模型时才需要装 agent extra

    return anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)


def _to_api_message(message: ChatMessage) -> dict[str, object]:
    """把内部消息转成供应商协议格式，保留 tool_use ↔ tool_result 的关联。"""
    if message.tool_results:
        blocks: list[dict[str, object]] = []
        for result in message.tool_results:
            block: dict[str, object] = {
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.content,
            }
            if result.is_error:
                block["is_error"] = True
            blocks.append(block)
        return {"role": "user", "content": blocks}
    if message.tool_call_id is not None:
        block: dict[str, object] = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id,
            "content": message.content,
        }
        if message.is_error:
            block["is_error"] = True
        return {"role": "user", "content": [block]}
    if message.tool_calls:
        content: list[dict[str, object]] = []
        if message.content:
            content.append({"type": "text", "text": message.content})
        content.extend(
            {
                "type": "tool_use",
                "id": call.tool_call_id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in message.tool_calls
        )
        return {"role": "assistant", "content": content}
    return {"role": message.role, "content": message.content}


def _from_api_response(response: object) -> ModelReply:
    """解析供应商响应：只取工具调用、文本与用量，其余一律不解释。"""
    blocks = getattr(response, "content", None)
    if not isinstance(blocks, list):
        return ModelReply(
            error_kind="invalid_response", error_detail="response has no content blocks"
        )
    tool_calls: list[ToolCallRequest] = []
    texts: list[str] = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "tool_use":
            arguments = getattr(block, "input", None)
            tool_calls.append(
                ToolCallRequest(
                    tool_call_id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        elif kind == "text":
            texts.append(str(getattr(block, "text", "")))
    return ModelReply(
        tool_calls=tool_calls,
        text="".join(texts) or None,
        usage=_usage_of(response),
        stop_reason=_stop_reason(response),
    )


def _stop_reason(response: object) -> str | None:
    value = getattr(response, "stop_reason", None)
    return value if isinstance(value, str) else None


def _usage_of(response: object) -> TokenUsage:
    """读取用量；缺字段就记 unknown，绝不把缺失当成 0。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage.unknown()
    prompt = getattr(usage, "input_tokens", None)
    completion = getattr(usage, "output_tokens", None)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return TokenUsage.unknown()
    return TokenUsage.of(prompt, completion)


def _classify(exc: Exception) -> tuple[str, str]:
    """把 SDK 异常映射成稳定分类；只保留类型名，不带异常文本。"""
    name = type(exc).__name__
    mapping = {
        "APITimeoutError": "timeout",
        "APIConnectionError": "connection",
        "RateLimitError": "rate_limit",
        "InternalServerError": "server_error",
        "APIStatusError": "server_error",
        "BadRequestError": "invalid_request",
        "AuthenticationError": "authentication",
        "PermissionDeniedError": "permission",
    }
    for suffix, kind in mapping.items():
        if name.endswith(suffix):
            return kind, name
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 500:
        return "server_error", name
    return "unexpected_error", name


__all__ = [
    "DEFAULT_API_KEY_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_FAKE",
    "AnthropicCompatibleProvider",
    "FakeProvider",
    "ModelErrorKind",
    "ModelProvider",
    "ModelReply",
    "ScriptedProvider",
    "scripted_responder",
]
