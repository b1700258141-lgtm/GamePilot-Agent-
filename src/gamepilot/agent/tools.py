"""工具白名单：模型只能选择动作或申请结束，别的什么都不行。

白名单只有两项：

- `perform_action(action)`：`action` 只能是 `attack` 或 `use_potion`；
- `finish(summary)`：申请结束；`summary` 只是模型自述，不是判定事实。

每轮最多接受**一个**工具调用，且必须通过下面的校验才可能触发游戏请求。
未知工具、多工具调用、非法参数、空输出一律在本地被拒绝，
既不发游戏请求，也不把拒绝当成「模型发现了缺陷」。

模型不可能通过工具改变的东西（因此这里根本没有对应工具）：
session_id、URL、请求方法、seed、输出路径、预算、规则数值。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from gamepilot.testing.models import ActionName

from .models import ToolCallRequest

TOOL_PERFORM_ACTION = "perform_action"
TOOL_FINISH = "finish"

# 拒绝原因码：稳定字符串，报告与测试都引用它。
REJECT_NO_TOOL_CALL = "no_tool_call"
REJECT_MULTIPLE_TOOL_CALLS = "multiple_tool_calls"
REJECT_UNKNOWN_TOOL = "unknown_tool"
REJECT_INVALID_ARGUMENTS = "invalid_arguments"

PERFORM_ACTION_SCHEMA: dict[str, object] = {
    "name": TOOL_PERFORM_ACTION,
    "description": (
        "对当前战斗会话执行一个游戏动作。每次只允许一个动作，"
        "程序会在发送前用公开规则核对它的预期结果。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["attack", "use_potion"],
                "description": (
                    "attack：攻击敌人，敌人存活时会反击；"
                    "use_potion：恢复 25 点生命（不超过上限）并消耗 1 瓶药水，随后敌人反击。"
                ),
            }
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}

FINISH_SCHEMA: dict[str, object] = {
    "name": TOOL_FINISH,
    "description": (
        "申请结束本次测试任务。summary 说明你观察到的现象，"
        "但任务是否完成由程序的确定性覆盖条件判定，不由 summary 决定。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "你判断任务是否完成的依据。"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
}

TOOL_SCHEMAS: tuple[dict[str, object], ...] = (PERFORM_ACTION_SCHEMA, FINISH_SCHEMA)

KNOWN_TOOLS = frozenset({TOOL_PERFORM_ACTION, TOOL_FINISH})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PerformActionArgs(_StrictModel):
    action: ActionName


class FinishArgs(_StrictModel):
    summary: str


class ToolChoice(BaseModel):
    """校验通过的工具选择。"""

    model_config = ConfigDict(extra="forbid")

    tool: Literal["perform_action", "finish"]
    action: ActionName | None = None
    summary: str | None = None
    tool_call_id: str


class RejectedChoice(BaseModel):
    """未通过校验的工具选择；`reason` 是稳定原因码，`detail` 用于提示模型。"""

    model_config = ConfigDict(extra="forbid")

    reason: str
    detail: str
    tool: str | None = None
    tool_call_id: str | None = None


def validate_choice(
    tool_calls: list[ToolCallRequest],
) -> ToolChoice | RejectedChoice:
    """校验一次模型输出里的工具选择。

    返回 `ToolChoice` 表示可以继续；返回 `RejectedChoice` 表示本轮不产生任何游戏请求。
    调用方在被拒绝时可以选择重发一次（占用格式纠正预算）或直接结束。
    """
    if not tool_calls:
        return RejectedChoice(
            reason=REJECT_NO_TOOL_CALL,
            detail=(
                "本轮没有返回任何工具调用。请只调用一个工具："
                f"{TOOL_PERFORM_ACTION} 或 {TOOL_FINISH}。"
            ),
        )
    if len(tool_calls) > 1:
        names = ", ".join(call.name for call in tool_calls)
        return RejectedChoice(
            reason=REJECT_MULTIPLE_TOOL_CALLS,
            detail=f"本轮返回了 {len(tool_calls)} 个工具调用（{names}），每轮只允许一个。",
        )
    call = tool_calls[0]
    if call.name not in KNOWN_TOOLS:
        return RejectedChoice(
            reason=REJECT_UNKNOWN_TOOL,
            detail=(f"未知工具 {call.name!r}；只能使用 {TOOL_PERFORM_ACTION} 或 {TOOL_FINISH}。"),
            tool=call.name,
            tool_call_id=call.tool_call_id,
        )
    if call.name == TOOL_PERFORM_ACTION:
        try:
            args = PerformActionArgs.model_validate(call.arguments)
        except ValidationError as exc:
            return RejectedChoice(
                reason=REJECT_INVALID_ARGUMENTS,
                detail=(
                    f"{TOOL_PERFORM_ACTION} 的参数不合法（{len(exc.errors())} 处）："
                    "只接受 action=attack|use_potion，不要附加其他字段。"
                ),
                tool=call.name,
                tool_call_id=call.tool_call_id,
            )
        return ToolChoice(
            tool=TOOL_PERFORM_ACTION, action=args.action, tool_call_id=call.tool_call_id
        )

    try:
        finish = FinishArgs.model_validate(call.arguments)
    except ValidationError as exc:
        return RejectedChoice(
            reason=REJECT_INVALID_ARGUMENTS,
            detail=(
                f"{TOOL_FINISH} 的参数不合法（{len(exc.errors())} 处）："
                "需要一个字符串字段 summary。"
            ),
            tool=call.name,
            tool_call_id=call.tool_call_id,
        )
    return ToolChoice(tool=TOOL_FINISH, summary=finish.summary, tool_call_id=call.tool_call_id)


__all__ = [
    "FINISH_SCHEMA",
    "KNOWN_TOOLS",
    "PERFORM_ACTION_SCHEMA",
    "REJECT_INVALID_ARGUMENTS",
    "REJECT_MULTIPLE_TOOL_CALLS",
    "REJECT_NO_TOOL_CALL",
    "REJECT_UNKNOWN_TOOL",
    "TOOL_FINISH",
    "TOOL_PERFORM_ACTION",
    "TOOL_SCHEMAS",
    "RejectedChoice",
    "ToolChoice",
    "validate_choice",
]
