"""缺陷会话：只覆盖领域层的三个窄规则扩展点。

缺陷必须是**真实的游戏状态变异**：会话仍然使用同一套动作流程、同一份
随机数推进、同一份事件生成与快照，只把其中一条数值规则换成缺陷版本。
因此 GET 读到的是真实已变更的状态与历史，后续合法动作也从该真实状态继续,
而不是重写 HTTP 响应或返回预写结果。

这里刻意不复制 `CombatSession`：一旦复制整份战斗流程，缺陷版本就会慢慢
偏离正常版本（随机数调用顺序、事件字段、回合推进都会各自演化），
那时「正常规则」就不止一份了。
"""

from gamepilot.api.routes import SessionFactory
from gamepilot.domain.combat import (
    POTION_HEAL,
    CombatSession,
    new_session_identity,
)

from .profiles import (
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    FaultProfile,
)
from .recorder import TriggerRecorder


class LabCombatSession(CombatSession):
    """把某一个缺陷叠加在正常战斗流程上的会话。"""

    def __init__(
        self,
        *,
        session_id: str,
        seed: int,
        profile: FaultProfile,
        recorder: TriggerRecorder,
    ) -> None:
        super().__init__(session_id=session_id, seed=seed)
        self._fault_profile = profile
        self._recorder = recorder

    @property
    def fault_profile(self) -> FaultProfile:
        """本会话所属的 profile；仅宿主可见，不进入任何响应。"""
        return self._fault_profile

    def _heal_amount(self, missing_hp: int) -> int:
        if self._fault_profile.fault_id == FAULT_POTION_OVERHEAL and missing_hp < POTION_HEAL:
            # 只有缺失生命值小于治疗量时才真的会超过上限；缺失等于治疗量时
            # 缺陷值与正常值相同，不算偏离，因此不记录。
            self._recorder.record(
                self.session_id,
                FAULT_POTION_OVERHEAL,
                f"缺失生命值 {missing_hp} < {POTION_HEAL}，仍治疗 {POTION_HEAL}",
            )
            return POTION_HEAL
        return super()._heal_amount(missing_hp)

    def _potions_after_use(self) -> int:
        if self._fault_profile.fault_id == FAULT_POTION_NOT_CONSUMED:
            self._recorder.record(
                self.session_id,
                FAULT_POTION_NOT_CONSUMED,
                f"成功喝药后药水仍为 {self._player.potions} 瓶",
            )
            return self._player.potions
        return super()._potions_after_use()

    def _should_retaliate(self, enemy_defeated: bool) -> bool:
        if self._fault_profile.fault_id == FAULT_RETALIATE_AFTER_DEATH and enemy_defeated:
            self._recorder.record(
                self.session_id,
                FAULT_RETALIATE_AFTER_DEATH,
                f"敌人生命值已为 {self._slime.hp} 仍执行反击",
            )
            return True
        return super()._should_retaliate(enemy_defeated)


def make_session_factory(profile: FaultProfile, recorder: TriggerRecorder) -> SessionFactory:
    """生成「新会话固定绑定某个 profile」的工厂。"""

    def factory(seed: int | None = None) -> CombatSession:
        session_id, resolved_seed = new_session_identity(seed)
        return LabCombatSession(
            session_id=session_id,
            seed=resolved_seed,
            profile=profile,
            recorder=recorder,
        )

    return factory
