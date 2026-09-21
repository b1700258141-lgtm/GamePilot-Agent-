"""工具白名单：模型只能选动作或申请结束，其余一律在本地被拒绝。

这一层是「不许产生未授权游戏请求」的第一道闸门，因此每个拒绝分支都要
有稳定的原因码，且拒绝结果**永远不是** `ToolChoice`——调用方据此保证
被拒绝的请求不会走到 HTTP。
"""

import pytest

from gamepilot.agent.models import ToolCallRequest
from gamepilot.agent.tools import (
    KNOWN_TOOLS,
    REJECT_INVALID_ARGUMENTS,
    REJECT_MULTIPLE_TOOL_CALLS,
    REJECT_NO_TOOL_CALL,
    REJECT_UNKNOWN_TOOL,
    TOOL_FINISH,
    TOOL_PERFORM_ACTION,
    TOOL_SCHEMAS,
    RejectedChoice,
    ToolChoice,
    validate_choice,
)


def _call(name: str, **arguments: object) -> ToolCallRequest:
    return ToolCallRequest(tool_call_id="call-1", name=name, arguments=dict(arguments))


def _reject(tool_calls: list[ToolCallRequest]) -> RejectedChoice:
    choice = validate_choice(tool_calls)
    assert isinstance(choice, RejectedChoice), f"本应被拒绝，却返回了 {choice!r}"
    return choice


def test_schemas_only_expose_the_two_allowed_tools() -> None:
    """白名单本身就是契约：对外只暴露两个工具，没有别的入口。"""
    names = {schema["name"] for schema in TOOL_SCHEMAS}
    assert names == KNOWN_TOOLS == {TOOL_PERFORM_ACTION, TOOL_FINISH}
    # 每个工具的输入结构都禁止附加字段：模型不能靠多余字段夹带参数。
    for schema in TOOL_SCHEMAS:
        assert schema["input_schema"]["additionalProperties"] is False  # type: ignore[index]


def test_accepts_valid_perform_action() -> None:
    choice = validate_choice([_call(TOOL_PERFORM_ACTION, action="attack")])
    assert isinstance(choice, ToolChoice)
    assert choice.tool == TOOL_PERFORM_ACTION
    assert choice.action == "attack"
    assert choice.tool_call_id == "call-1"


@pytest.mark.parametrize("action", ["attack", "use_potion"])
def test_accepts_both_game_actions(action: str) -> None:
    choice = validate_choice([_call(TOOL_PERFORM_ACTION, action=action)])
    assert isinstance(choice, ToolChoice)
    assert choice.action == action


def test_accepts_valid_finish() -> None:
    choice = validate_choice([_call(TOOL_FINISH, summary="观察完成")])
    assert isinstance(choice, ToolChoice)
    assert choice.tool == TOOL_FINISH
    assert choice.summary == "观察完成"
    # finish 不携带动作：它不产生游戏请求。
    assert choice.action is None


def test_empty_output_is_rejected() -> None:
    assert _reject([]).reason == REJECT_NO_TOOL_CALL


def test_multiple_tool_calls_are_rejected() -> None:
    rejected = _reject(
        [
            _call(TOOL_PERFORM_ACTION, action="attack"),
            _call(TOOL_FINISH, summary="结束"),
        ]
    )
    assert rejected.reason == REJECT_MULTIPLE_TOOL_CALLS
    assert "2" in rejected.detail


def test_unknown_tool_is_rejected_and_not_treated_as_finish() -> None:
    rejected = _reject([_call("admin_tool", action="attack")])
    assert rejected.reason == REJECT_UNKNOWN_TOOL
    assert rejected.tool == "admin_tool"


@pytest.mark.parametrize("action", ["", "ATTACK", "flee", "use_potion "])
def test_invalid_action_is_rejected(action: str) -> None:
    rejected = _reject([_call(TOOL_PERFORM_ACTION, action=action)])
    assert rejected.reason == REJECT_INVALID_ARGUMENTS
    assert rejected.tool == TOOL_PERFORM_ACTION


def test_extra_argument_is_rejected() -> None:
    """多余字段一律拒绝：模型不能靠附加字段夹带 session_id 之类的参数。"""
    rejected = _reject([_call(TOOL_PERFORM_ACTION, action="attack", session_id="other-session")])
    assert rejected.reason == REJECT_INVALID_ARGUMENTS


def test_missing_argument_is_rejected() -> None:
    assert _reject([_call(TOOL_PERFORM_ACTION)]).reason == REJECT_INVALID_ARGUMENTS


def test_non_string_summary_is_rejected() -> None:
    assert _reject([_call(TOOL_FINISH, summary=42)]).reason == REJECT_INVALID_ARGUMENTS


def test_no_rejection_produces_a_tool_choice() -> None:
    """穷举拒绝分支：任何一个都不得返回可执行的选择。"""
    samples = [
        [],
        [_call(TOOL_PERFORM_ACTION, action="attack"), _call(TOOL_PERFORM_ACTION, action="attack")],
        [_call("admin_tool")],
        [_call(TOOL_PERFORM_ACTION, action="teleport")],
        [_call(TOOL_FINISH)],
    ]
    for tool_calls in samples:
        assert isinstance(validate_choice(tool_calls), RejectedChoice)
