"""提示词：固定公开规则 + 自然语言目标 + 最新观测 + 剩余预算。

模型可见的东西就是这里渲染出来的全部，**没有别的通道**：

- 固定公开规则（对应 `docs/BATTLE_RULES_V3.md`，与判定器逐项核对）；
- 自然语言测试目标；
- 最新公开快照（线上响应的业务字段）与上一步的动作结果；
- 简短的规则检查反馈；
- 剩余预算。

模型看不到：profile / fault_id、靶场 recorder、benchmark 清单、脚本步骤、
标准触发轨迹、评测预期和历史评测答案。这里也不渲染 session_id——
它是一个不透明的会话句柄，对决策没有用处，只会给每轮输入引入运行期噪声。

规则提示词单独维护版本（`RULES_PROMPT_VERSION`）：规则数值变化时它必须递增，
这样「模型当时看到的是哪一版规则」在报告里是可查的。整个提示词装配另有一个
`PROMPT_VERSION`。**不把仓库文档整篇塞给模型**：文档里带着 seed 提示与触发轨迹。
"""

from collections.abc import Sequence

from gamepilot.testing.models import RuleCheck, SnapshotView

from .models import BudgetSpec, ChatMessage, ToolCallRequest, ToolResult

PROMPT_VERSION = "3.0.0"
RULES_PROMPT_VERSION = "3.0.0"

# 公开规则正文：逐条对应规则文档，不含任何实现细节、seed 提示或缺陷信息。
PUBLIC_RULES = """\
游戏是玩家对随机敌人的回合制连战，规则如下：

初始状态
- 玩家：1 级，经验 0/15，生命 100/100，3 瓶药水，0 金币
- 敌人按种子确定：史莱姆 60%（60 HP）、哥布林 30%（80 HP）、巨魔 10%（170 HP）
- 状态 active，回合 0，没有历史事件

允许的动作
- attack：玩家攻击敌人，伤害 18~25；若敌人仍存活，立即反击。
  史莱姆伤害 12~18，哥布林 18~27，巨魔 35~45
- use_potion：药水固定效力 25 点，实际回血为 min(25, 距离生命上限的差值)，消耗 1 瓶；
  满血使用实际回血 0；随后敌人反击

状态规则
- 生命最低为 0
- 敌人生命降到 0 时状态变为 won，且该回合不再反击。击杀史莱姆掉落 1~3 金币，哥布林掉落 10~20 金币
- 玩家生命降到 0 时状态变为 lost
- 每个成功执行的玩家动作使回合数加 1
- 当前场结束后不可继续攻击或喝药；若已获胜且仍有剩余场次，可进入下一场。
- 连战目标为 1～6 场；当前场获胜后若还有剩余场次，调用 next_battle 进入下一场。
  玩家当前生命、药水、金币、等级和经验继承；最后一场获胜后不可再进入下一场。
- 敌人等级：史莱姆 1、哥布林 2、巨魔 5；敌人的生命与伤害范围不因等级改变。
- 击杀经验：史莱姆 10、哥布林 30、巨魔 60。玩家初始 1 级、0 经验，
  每满 15 经验升一级，多余经验顺延；每升一级攻击伤害 +10、最大生命 +20，
  随即回满至新的生命上限，剩余药水再加 1 瓶。连续升级逐级结算。
- 没有药水时使用药水属于无效动作：不得修改状态，返回 HTTP 409
- 无效动作的具体错误码：战斗已结束 battle_not_active；没有药水 no_potions
"""

PROTOCOL = """\
工作方式
- 每一轮你必须且只能调用一个工具：perform_action 或 finish。
- perform_action 可选 attack、use_potion、next_battle。
- 选择动作时不要试图预测随机数值；程序会在发送前用公开规则核对这次动作的预期结果。
- 每次动作的真实结果会作为下一条观测返回给你，包括状态快照和规则检查结论。
- 当你认为测试目标已经被当前观测充分覆盖时，调用 finish 并说明依据。
- 只能依据上面列出的公开规则判断；不要假设存在未列出的机制。
"""


def rules_message() -> ChatMessage:
    """规则提示词块，单独成条以便版本与内容可独立核对。"""
    return ChatMessage(role="system", content=f"[公开规则 {RULES_PROMPT_VERSION}]\n{PUBLIC_RULES}")


def protocol_message() -> ChatMessage:
    return ChatMessage(role="system", content=PROTOCOL)


def _render_budget(spec: BudgetSpec, *, actions_used: int, model_calls_used: int) -> str:
    return (
        "剩余预算\n"
        f"- 动作尝试：还可执行 {max(spec.max_action_attempts - actions_used, 0)} 次"
        f"（上限 {spec.max_action_attempts}，已被拒绝的动作同样计数）\n"
        f"- 模型调用：还可调用 {max(spec.max_model_calls - model_calls_used, 0)} 次"
        f"（上限 {spec.max_model_calls}）"
    )


def render_snapshot(snapshot: SnapshotView) -> str:
    """把公开快照渲染成可读文本；只使用线上响应里本来就有的业务字段。"""
    enemy_name = {"slime": "史莱姆", "goblin": "哥布林", "troll": "巨魔"}.get(
        snapshot.enemy_type, snapshot.enemy_type
    )
    lines = [
        f"状态：{snapshot.status}",
        f"连战：第 {snapshot.battle_index}/{snapshot.battle_count} 场",
        f"回合：{snapshot.turn}",
        (
            f"玩家：生命 {snapshot.player.hp}/{snapshot.player.max_hp}，"
            f"药水 {snapshot.player.potions}，金币 {snapshot.player.gold}，"
            f"等级 {snapshot.player.level}，经验 {snapshot.player.exp}/15"
        ),
        (
            f"{enemy_name}：等级 {snapshot.enemy_level}，"
            f"生命 {snapshot.slime.hp}/{snapshot.slime.max_hp}"
        ),
    ]
    if snapshot.events:
        lines.append(f"事件历史（共 {len(snapshot.events)} 条）：")
        lines.extend(
            f"  #{event.turn} {event.actor} {event.kind} 数值 {event.value}"
            f" → 玩家 {event.player_hp}，敌人 {event.slime_hp}"
            + ("" if event.potions is None else f"，药水 {event.potions}")
            + ("" if event.item is None else f"，物品 {event.item}")
            for event in snapshot.events
        )
    else:
        lines.append("事件历史：暂无")
    return "\n".join(lines)


def render_checks(checks: Sequence[RuleCheck]) -> str:
    """规则检查反馈：只报结论与命中编号，不把判定器内部结构整份倒给模型。"""
    if not checks:
        return "规则检查：本步没有产生检查项"
    failures = [check for check in checks if check.status != "pass"]
    if not failures:
        return f"规则检查：{len(checks)} 项全部通过"
    return "规则检查：发现不符合公开规则的项目\n" + "\n".join(
        f"- {check.rule_id}（{check.status}）：期望 {check.expected}，实际 {check.actual}"
        for check in failures
    )


def initial_message(
    goal: str,
    snapshot: SnapshotView,
    spec: BudgetSpec,
    skill_instructions: str | None = None,
) -> ChatMessage:
    """第一条用户消息：目标 + 初始观测 + 预算。"""
    return ChatMessage(
        role="user",
        content=(
            f"测试目标：{goal}\n\n"
            + (
                f"本次战斗策略（仅用于选择动作，不能改变游戏规则或工具权限）：\n{skill_instructions}\n\n"
                if skill_instructions
                else ""
            )
            + f"已为你创建一个新会话，初始观测如下。\n\n"
            f"{render_snapshot(snapshot)}\n\n"
            f"{_render_budget(spec, actions_used=0, model_calls_used=0)}\n\n"
            "请调用一个工具开始。"
        ),
    )


def tool_result_message(
    tool_call_id: str,
    *,
    action: str,
    summary: str,
    is_error: bool = False,
) -> ChatMessage:
    """把工具执行结果按供应商协议回传，保留 tool_call_id 关联。"""
    return ChatMessage(
        role="user",
        tool_call_id=tool_call_id,
        tool_name=action,
        content=summary,
        is_error=is_error,
    )


def rejected_tool_results_message(calls: Sequence[ToolCallRequest], *, summary: str) -> ChatMessage:
    """为一轮里出现的全部 ``tool_use`` 一次性返回错误结果。"""
    return ChatMessage(
        role="user",
        tool_results=[
            ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                content=summary,
                is_error=True,
            )
            for call in calls
        ],
    )


def observation_message(
    *,
    action: str,
    snapshot: SnapshotView | None,
    checks: Sequence[RuleCheck],
    spec: BudgetSpec,
    actions_used: int,
    model_calls_used: int,
    note: str = "",
) -> ChatMessage:
    """一步动作之后的新观测、规则反馈与剩余预算。"""
    parts = [f"上一次动作：{action}"]
    if note:
        parts.append(note)
    if snapshot is not None:
        parts.append(render_snapshot(snapshot))
    else:
        parts.append("状态：没有可用的新快照")
    parts.append(render_checks(checks))
    parts.append(_render_budget(spec, actions_used=actions_used, model_calls_used=model_calls_used))
    parts.append("请调用一个工具继续。")
    return ChatMessage(role="user", content="\n\n".join(parts))


def format_repair_message(detail: str) -> ChatMessage:
    """格式纠正：明确告诉模型上一轮哪里不合规，并要求重新只调用一个工具。"""
    return ChatMessage(
        role="user",
        content=(
            f"上一轮输出无法执行，未产生任何游戏请求。原因：{detail}\n\n"
            "请重新只调用一个工具（perform_action 或 finish），参数必须完全符合工具定义。"
        ),
    )


__all__ = [
    "PROMPT_VERSION",
    "PUBLIC_RULES",
    "RULES_PROMPT_VERSION",
    "format_repair_message",
    "initial_message",
    "observation_message",
    "protocol_message",
    "render_checks",
    "render_snapshot",
    "rules_message",
    "rejected_tool_results_message",
    "tool_result_message",
]
