"""规则规格：判定所用的数值与规则编号，独立于被测实现。

来源是规则文档 `docs/PHASE_1_PLAN.md` 第 5 节「最小游戏规则」，
不是 `gamepilot.domain.combat`：如果判定器导入被测实现的常量，
就等于让被测代码生成自己的「正确答案」，缺陷会一起被搬进期望值里。

整个 `gamepilot.testing` 包都不导入 gamepilot 的其他模块，
`tests/unit/test_testing_independence.py` 会强制检查这条边界。

规则编号（rule_id）稳定不变，报告、测试和后续缺陷评测集都引用它们。
"""

# 规则规格版本：数值或判定语义变化时必须递增，报告用它判断能否重跑比较。
RULES_VERSION = "1.1.0"
RULES_SOURCE = "docs/PHASE_1_PLAN.md#5-最小游戏规则"

# 初始状态（规则文档「初始状态」）。
PLAYER_MAX_HP = 100
SLIME_MAX_HP = 60
INITIAL_POTIONS = 2

# 数值范围（规则文档「初始状态」与「允许的动作」）。
PLAYER_DAMAGE_MIN = 18
PLAYER_DAMAGE_MAX = 25
SLIME_DAMAGE_MIN = 20
SLIME_DAMAGE_MAX = 35
POTION_HEAL = 25

# 执行器约束。
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_ACTION_ATTEMPTS = 20

# 报告 schema 版本；重跑只接受明确支持的版本。
SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = ("1.1",)

# 基础设施类错误码：出现这些码说明问题不在游戏规则本身，
# 必须归类为执行错误（error），不能记成「已确认的游戏缺陷」（fail）。
INFRASTRUCTURE_ERROR_CODES = frozenset(
    {
        "persistence_unavailable",
        "persistence_conflict",
        "persistence_inconsistent",
        "internal_error",
    }
)

# 稳定规则编号。
R_INIT_STATE = "R-INIT-STATE"
R_CREATE_SEED = "R-CREATE-SEED"
R_GET_STATUS = "R-GET-STATUS"
R_CREATE_STATUS = "R-CREATE-STATUS"
R_CREATE_GET_IDENTICAL = "R-CREATE-GET-IDENTICAL"
R_INVARIANTS = "R-INVARIANTS"
R_EXPECTED_STATUS = "R-EXPECTED-STATUS"
R_EXPECTED_CODE = "R-EXPECTED-CODE"
R_TURN_INCREMENT = "R-TURN-INCREMENT"
R_HISTORY_PREFIX = "R-HISTORY-PREFIX"
R_EVENT_COUNT = "R-EVENT-COUNT"
R_EVENT_ACTOR_ORDER = "R-EVENT-ACTOR-ORDER"
R_EVENT_TURN = "R-EVENT-TURN"
R_ATTACK_DAMAGE = "R-ATTACK-DAMAGE"
R_ATTACK_HP_APPLY = "R-ATTACK-HP-APPLY"
R_NO_RETALIATE_ON_KILL = "R-NO-RETALIATE-ON-KILL"
R_RETALIATE_ONCE = "R-RETALIATE-ONCE"
R_RETALIATE_POTIONS_NULL = "R-RETALIATE-POTIONS-NULL"
R_POTION_HEAL = "R-POTION-HEAL"
R_POTION_CAP = "R-POTION-CAP"
R_POTION_DECREMENTS = "R-POTION-DECREMENTS"
R_REJECT_NO_STATE_CHANGE = "R-REJECT-NO-STATE-CHANGE"
R_STATUS_CONSISTENT = "R-STATUS-CONSISTENT"
R_LAST_EVENT_MATCHES_SNAPSHOT = "R-LAST-EVENT-MATCHES-SNAPSHOT"
R_FINAL_STATUS = "R-FINAL-STATUS"
R_CONTROL_MATCH = "R-CONTROL-MATCH"
R_STEP_LIMIT = "R-STEP-LIMIT"

# rule_id → 规则含义。判定器只使用这里的编号，测试用它检查覆盖完整性。
RULE_CATALOG: dict[str, str] = {
    R_CREATE_SEED: "创建响应 seed 等于本次请求",
    R_GET_STATUS: "查询会话返回 HTTP 200",
    R_INIT_STATE: "初始状态：玩家 HP/上限 100、2 瓶药水、敌人 HP/上限 60、active、turn 0、无事件",
    R_CREATE_STATUS: "创建会话返回 201",
    R_CREATE_GET_IDENTICAL: "创建后 GET 返回与创建响应完全一致的状态",
    R_INVARIANTS: "HP 在 [0, max_hp] 内、药水非负递减，且动作不改变 session_id/seed/max_hp",
    R_EXPECTED_STATUS: "步骤 HTTP 状态符合场景预期",
    R_EXPECTED_CODE: "步骤错误码符合场景预期",
    R_TURN_INCREMENT: "成功动作使 turn 恰好 +1",
    R_HISTORY_PREFIX: "旧事件完整保留，仅在其后追加",
    R_EVENT_COUNT: "每个成功动作新增的事件数量符合规则",
    R_EVENT_ACTOR_ORDER: "新事件中玩家事件在前，且类型与动作一致",
    R_EVENT_TURN: "新事件的回合号等于新 turn",
    R_ATTACK_DAMAGE: "攻击伤害在 18～25 之间",
    R_ATTACK_HP_APPLY: "攻击按事件值扣减敌人 HP 并截断到 0，玩家 HP 与药水不变",
    R_NO_RETALIATE_ON_KILL: "致死攻击本回合不反击",
    R_RETALIATE_ONCE: "敌人存活时恰好一次反击，伤害 20～35 且按值扣血",
    R_RETALIATE_POTIONS_NULL: "反击事件的 potions 为 null",
    R_POTION_HEAL: "喝药治疗量为 min(25, max_hp - 动作前 HP)",
    R_POTION_CAP: "喝药后 HP 不超过上限",
    R_POTION_DECREMENTS: "成功喝药后药水恰好减 1",
    R_REJECT_NO_STATE_CHANGE: "预期 409 后由 GET 验证状态与完整事件未变",
    R_STATUS_CONSISTENT: "won 对应敌人 HP=0、lost 对应玩家 HP=0，否则 active",
    R_LAST_EVENT_MATCHES_SNAPSHOT: "最后事件的双方 HP 与最终快照一致",
    R_FINAL_STATUS: "场景结束时状态符合场景声明的预期",
    R_CONTROL_MATCH: "对照会话与主会话的状态和事件一致",
    R_STEP_LIMIT: "未超过每场景动作尝试上限（含被拒绝动作）",
}
