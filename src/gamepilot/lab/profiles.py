"""缺陷靶场的 profile 定义。

profile 表示「这个靶场实例把哪一条规则替换成缺陷版本」，`normal` 表示
原样正确行为。它由靶场启动者在**启动时**选定，每个应用实例固定一种，
运行中不能切换或叠加：

- 它不是环境开关（不读 `.env`、不读 Settings）；
- 它不是 HTTP 参数（游戏 API 上没有读取或切换 profile 的端点）。

`profile` 只影响「新会话由谁创建」这一个注入点（见 `api.routes` 的
`SessionFactory`），因此正常服务的配置、仓储与既有行为都不受影响。
"""

from dataclasses import dataclass

PROFILE_NORMAL = "normal"
FAULT_POTION_OVERHEAL = "potion_overheal"
FAULT_POTION_NOT_CONSUMED = "potion_not_consumed"
FAULT_RETALIATE_AFTER_DEATH = "retaliate_after_death"

# 缺陷编号：与 profile 同名，评测清单用它标记「这一列属于哪个缺陷」。
FAULT_IDS: tuple[str, ...] = (
    FAULT_POTION_OVERHEAL,
    FAULT_POTION_NOT_CONSUMED,
    FAULT_RETALIATE_AFTER_DEATH,
)

# 合法 profile 全集：normal 在前，其后是三个单缺陷变体。
PROFILES: tuple[str, ...] = (PROFILE_NORMAL, *FAULT_IDS)


class UnknownProfileError(ValueError):
    """非法的 profile 名称：明确报错，不退回 normal。"""

    def __init__(self, profile: str) -> None:
        super().__init__(f"未知的靶场 profile：{profile!r}；合法取值：{', '.join(PROFILES)}")
        self.profile = profile


@dataclass(frozen=True)
class FaultProfile:
    """一种靶场 profile：profile 名、对应缺陷编号与一句话说明。"""

    profile_id: str
    fault_id: str | None
    summary: str


PROFILE_CATALOG: dict[str, FaultProfile] = {
    PROFILE_NORMAL: FaultProfile(
        profile_id=PROFILE_NORMAL,
        fault_id=None,
        summary="原始正确行为：全部规则与正常服务一致，不植入任何缺陷",
    ),
    FAULT_POTION_OVERHEAL: FaultProfile(
        profile_id=FAULT_POTION_OVERHEAL,
        fault_id=FAULT_POTION_OVERHEAL,
        summary="喝药始终治疗 25 点，不按最大生命值截断（可超过上限）",
    ),
    FAULT_POTION_NOT_CONSUMED: FaultProfile(
        profile_id=FAULT_POTION_NOT_CONSUMED,
        fault_id=FAULT_POTION_NOT_CONSUMED,
        summary="成功喝药仍正确治疗并反击，但药水数量不减少",
    ),
    FAULT_RETALIATE_AFTER_DEATH: FaultProfile(
        profile_id=FAULT_RETALIATE_AFTER_DEATH,
        fault_id=FAULT_RETALIATE_AFTER_DEATH,
        summary="致死攻击后敌人仍执行一次真实反击并扣玩家生命值",
    ),
}


def resolve_profile(profile_id: str) -> FaultProfile:
    """按名称取出 profile；非法名称抛出 UnknownProfileError。"""
    try:
        return PROFILE_CATALOG[profile_id]
    except KeyError:
        raise UnknownProfileError(profile_id) from None
