"""回合制战斗领域逻辑：状态转换、伤害计算与随机。

本模块不依赖 FastAPI 或其他 Web 框架，可独立于 API 直接做单元测试。

确定性来源：每个 CombatSession 持有私有的 random.Random(seed)，
不使用模块级全局随机，因此不同会话的随机状态互不影响；
相同 seed 加相同合法动作序列，会产生完全相同的伤害、事件与最终状态。
"""

import random
import secrets
import uuid

from .errors import InvalidCombatAction
from .models import BattleStatus, CombatantState, CombatEvent, GameSnapshot, PlayerState

PLAYER_MAX_HP = 100
SLIME_MAX_HP = 60
INITIAL_POTIONS = 2
POTION_HEAL = 25
PLAYER_DAMAGE_RANGE = (18, 25)
# 让失败分支能通过公开动作真实到达，同时保留固定种子的可回放性。
SLIME_DAMAGE_RANGE = (20, 35)


class CombatSession:
    """一场玩家对战史莱姆的回合制战斗。"""

    def __init__(self, session_id: str, seed: int) -> None:
        self._session_id = session_id
        self._seed = seed
        self._rng = random.Random(seed)
        self._player = PlayerState(hp=PLAYER_MAX_HP, max_hp=PLAYER_MAX_HP, potions=INITIAL_POTIONS)
        self._slime = CombatantState(hp=SLIME_MAX_HP, max_hp=SLIME_MAX_HP)
        self._status = BattleStatus.ACTIVE
        self._turn = 0
        self._events: list[CombatEvent] = []

    @property
    def session_id(self) -> str:
        """会话唯一标识，由创建方生成。"""
        return self._session_id

    @property
    def seed(self) -> int:
        """创建会话时使用的随机种子，用于回放。"""
        return self._seed

    def attack(self) -> None:
        """玩家攻击史莱姆；若史莱姆存活，立即反击。"""
        self._ensure_active()
        turn = self._next_turn()
        damage = self._rng.randint(*PLAYER_DAMAGE_RANGE)
        self._slime.hp = max(0, self._slime.hp - damage)
        self._append_event(
            CombatEvent(
                turn=turn,
                actor="player",
                kind="attack",
                value=damage,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
                potions=self._player.potions,
            )
        )
        # 先按规则判定敌人是否死亡，再由扩展点决定本回合是否反击。
        enemy_defeated = self._slime.hp == 0
        if enemy_defeated:
            self._status = BattleStatus.WON
        if self._should_retaliate(enemy_defeated):
            self._retaliate(turn)
        self._turn = turn

    def use_potion(self) -> None:
        """玩家使用药水恢复 25 点生命（不超过上限），随后史莱姆反击。"""
        self._ensure_active()
        if self._player.hp == self._player.max_hp:
            raise InvalidCombatAction("player_full_hp", "player is already at full HP")
        if self._player.potions == 0:
            raise InvalidCombatAction("no_potions", "no potions left")
        turn = self._next_turn()
        healed = self._heal_amount(self._player.max_hp - self._player.hp)
        self._player.hp += healed
        self._player.potions = self._potions_after_use()
        self._append_event(
            CombatEvent(
                turn=turn,
                actor="player",
                kind="potion",
                value=healed,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
                potions=self._player.potions,
            )
        )
        self._retaliate(turn)
        self._turn = turn

    def snapshot(self) -> GameSnapshot:
        """返回当前状态的深拷贝快照，避免外部持有可变内部状态。"""
        return GameSnapshot(
            session_id=self._session_id,
            seed=self._seed,
            status=self._status,
            turn=self._turn,
            player=self._player.model_copy(deep=True),
            slime=self._slime.model_copy(deep=True),
            events=[event.model_copy(deep=True) for event in self._events],
        )

    def _ensure_active(self) -> None:
        """战斗必须处于 active 状态才能执行动作。"""
        if self._status is not BattleStatus.ACTIVE:
            raise InvalidCombatAction(
                "battle_not_active",
                f"battle already finished with status {self._status.value}",
            )

    def _next_turn(self) -> int:
        """成功动作后的新回合号（每个玩家动作 +1）。"""
        return self._turn + 1

    # ------------------------------------------------------------ 规则扩展点
    #
    # 下面三个方法就是正常规则本身，默认实现保持既有数值行为不变。
    # 它们存在的意义是给「可控缺陷靶场」（gamepilot.lab）一个窄的覆盖点：
    # 缺陷会话只替换其中一条规则，完整动作流程、随机数推进、事件生成
    # 与快照仍然只有 CombatSession 这一份实现。

    def _heal_amount(self, missing_hp: int) -> int:
        """喝药的实际治疗量；默认不超过缺失的生命值。"""
        return min(POTION_HEAL, missing_hp)

    def _potions_after_use(self) -> int:
        """成功喝药后的剩余药水数量；默认消耗 1 瓶。"""
        return self._player.potions - 1

    def _should_retaliate(self, enemy_defeated: bool) -> bool:
        """本回合敌人是否反击；默认只在敌人存活时反击。"""
        return not enemy_defeated

    def _retaliate(self, turn: int) -> None:
        """史莱姆反击；玩家生命降至 0 则战斗失败。"""
        damage = self._rng.randint(*SLIME_DAMAGE_RANGE)
        self._player.hp = max(0, self._player.hp - damage)
        self._append_event(
            CombatEvent(
                turn=turn,
                actor="slime",
                kind="retaliate",
                value=damage,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
            )
        )
        if self._player.hp == 0:
            self._status = BattleStatus.LOST

    def _append_event(self, event: CombatEvent) -> None:
        self._events.append(event)


def new_session_identity(seed: int | None) -> tuple[str, int]:
    """生成新会话的标识与随机种子。

    未提供种子时生成一个随机种子；种子随快照返回，因此无论是否显式指定，
    任何一场战斗都可回放。标识与种子的生成只有这一处实现，
    应用装配与缺陷靶场都从这里取，避免两处各写一份而逐渐不一致。
    """
    return uuid.uuid4().hex, secrets.randbits(31) if seed is None else seed


def create_combat_session(seed: int | None = None) -> CombatSession:
    """创建新战斗会话（正常规则）。"""
    session_id, resolved_seed = new_session_identity(seed)
    return CombatSession(session_id=session_id, seed=resolved_seed)
