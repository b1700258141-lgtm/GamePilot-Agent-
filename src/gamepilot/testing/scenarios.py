"""内置场景：固定 seed 与固定动作序列，覆盖正常路径、边界与被拒绝动作。

场景只是数据，不含执行逻辑。每个场景都在真实 HTTP 上跑一遍，
因此这里的 seed 与动作序列必须能在当前规则下确定性地到达预期分支。

seed=42：三次攻击获胜；满血喝药被拒；前两次喝药成功、第三次 no_potions。
seed=1252：三次攻击阵亡（lost 分支同样通过公开动作到达，不修改私有 HP）。
seed=12345：攻击 → 喝药 → 攻击，全程 active，用作对照运行的样本。
"""

from .models import ActionStep, ControlSpec, Scenario

SUITE_BASELINE = "baseline"

BASELINE_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        case_id="initial-state",
        description="创建会话并查询初始状态：数值与空事件符合规则文档",
        seed=42,
        steps=[],
        expected_final_status="active",
    ),
    Scenario(
        case_id="potion-at-full-hp-rejected",
        description="满血时喝药被拒绝：409 player_full_hp，且状态与事件不变",
        seed=42,
        steps=[
            ActionStep(action="use_potion", expected_status=409, expected_code="player_full_hp")
        ],
        expected_final_status="active",
    ),
    Scenario(
        case_id="attack-then-potion",
        description="攻击后喝药：治疗量受上限约束，随后仍有一次反击",
        seed=42,
        steps=[
            ActionStep(action="attack"),
            ActionStep(action="use_potion"),
        ],
        expected_final_status="active",
    ),
    Scenario(
        case_id="potion-exhaustion",
        description="攻击后连续喝药：前两次成功，第三次 409 no_potions",
        seed=42,
        steps=[
            ActionStep(action="attack"),
            ActionStep(action="use_potion"),
            ActionStep(action="use_potion"),
            ActionStep(action="use_potion", expected_status=409, expected_code="no_potions"),
        ],
        expected_final_status="active",
    ),
    Scenario(
        case_id="win-then-rejected",
        description="三次攻击获胜，继续动作返回 409 battle_not_active",
        seed=42,
        steps=[
            ActionStep(action="attack"),
            ActionStep(action="attack"),
            ActionStep(action="attack"),
            ActionStep(action="attack", expected_status=409, expected_code="battle_not_active"),
        ],
        expected_final_status="won",
    ),
    Scenario(
        case_id="lose-then-rejected",
        description="用公开动作打到阵亡（lost），之后动作被 409 拒绝",
        seed=1252,
        steps=[
            ActionStep(action="attack"),
            ActionStep(action="attack"),
            ActionStep(action="attack"),
            ActionStep(action="attack", expected_status=409, expected_code="battle_not_active"),
        ],
        expected_final_status="lost",
    ),
    Scenario(
        case_id="rejected-action-keeps-rng",
        description="被拒绝的动作不消耗随机数：与不含该次尝试的对照会话逐事件一致",
        seed=42,
        steps=[
            ActionStep(action="use_potion", expected_status=409, expected_code="player_full_hp"),
            ActionStep(action="attack"),
            ActionStep(action="use_potion"),
            ActionStep(action="attack"),
        ],
        expected_final_status="active",
        control=ControlSpec(
            description="同一 seed，只执行被接受的动作，用于比对随机结果",
            actions=["attack", "use_potion", "attack"],
        ),
    ),
    Scenario(
        case_id="reproducible-duplicate-run",
        description="两个新会话同 seed 同动作：规范化后的状态与事件完全相同",
        seed=12345,
        steps=[
            ActionStep(action="attack"),
            ActionStep(action="use_potion"),
            ActionStep(action="attack"),
        ],
        expected_final_status="active",
        control=ControlSpec(
            description="同 seed 同动作的第二个会话",
            actions=["attack", "use_potion", "attack"],
        ),
    ),
)

SUITES: dict[str, tuple[Scenario, ...]] = {
    SUITE_BASELINE: BASELINE_SCENARIOS,
}


def get_suite(name: str) -> tuple[Scenario, ...]:
    """按名称取出场景集合；未知名由调用方转成输入错误。"""
    if name not in SUITES:
        raise KeyError(name)
    return SUITES[name]
