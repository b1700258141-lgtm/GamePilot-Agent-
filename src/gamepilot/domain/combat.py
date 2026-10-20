"""回合制战斗领域逻辑：状态转换、伤害计算与随机。

本模块不依赖 FastAPI 或其他 Web 框架，可独立于 API 直接做单元测试。

确定性来源：每个 CombatSession 持有私有的 random.Random(seed)，
不使用模块级全局随机，因此不同会话的随机状态互不影响；
相同 seed 加相同合法动作序列，会产生完全相同的伤害、事件与最终状态。
"""

import random
import secrets
import uuid
from typing import Literal

from .errors import InvalidCombatAction
from .models import BattleStatus, CombatantState, CombatEvent, GameSnapshot, PlayerState

PLAYER_MAX_HP = 100
SLIME_MAX_HP = 60
INITIAL_POTIONS = 3
POTION_HEAL = 25
PLAYER_DAMAGE_RANGE = (18, 25)
# 让失败分支能通过公开动作真实到达，同时保留固定种子的可回放性。
SLIME_DAMAGE_RANGE = (12, 18)
ENEMY_SPECS = {
    "slime": (60, (12, 18)),
    "goblin": (80, (18, 27)),
    "skeleton": (95, (20, 30)),
    "wolf": (110, (24, 34)),
    "orc": (135, (28, 38)),
    "troll": (170, (35, 45)),
    "wraith": (210, (40, 52)),
    "dragon": (280, (50, 65)),
}
ENEMY_LEVELS = {
    "slime": 1,
    "goblin": 2,
    "skeleton": 3,
    "wolf": 4,
    "orc": 4,
    "troll": 5,
    "wraith": 6,
    "dragon": 8,
}
ENEMY_EXP = {
    "slime": 10,
    "goblin": 30,
    "skeleton": 35,
    "wolf": 40,
    "orc": 50,
    "troll": 60,
    "wraith": 75,
    "dragon": 100,
}
NEW_ENEMY_GOLD = {
    "skeleton": (4, 8),
    "wolf": (6, 10),
    "orc": (12, 20),
    "wraith": (20, 30),
    "dragon": (35, 50),
}
V4_ENEMY_WEIGHTS = (
    ("slime", 35),
    ("goblin", 22),
    ("skeleton", 14),
    ("wolf", 10),
    ("orc", 8),
    ("troll", 5),
    ("wraith", 4),
    ("dragon", 2),
)
EXP_PER_LEVEL = 15
CAMPAIGN_RULESETS = ("v3", "v4", "v5")
MAGIC_RULESETS = ("v4", "v5")
LEVEL_DAMAGE_BONUS = 10
LEVEL_HP_BONUS = 20
MAX_BATTLES = 6
POWER_STRIKE_BONUS = 12
POWER_STRIKE_COOLDOWN = 2
INITIAL_EP = 100
INITIAL_MAGIC = 10
LEVEL_EP_BONUS = 50
LEVEL_MAGIC_BONUS = 5
SPELL_DAMAGE = {
    "cast_spark": (20, (18, 24)),
    "cast_flame": (40, (32, 40)),
    "cast_meteor": (70, (50, 60)),
}
HEAL_SPELL_COST = 30
HEAL_SPELL_BASE = 20


class CombatSession:
    """按固定种子运行的单场或多场回合制战斗。"""

    def __init__(
        self, session_id: str, seed: int, *, ruleset: str = "v3", battle_count: int = 1
    ) -> None:
        self._session_id = session_id
        self._seed = seed
        if ruleset not in ("v1", "v2", *CAMPAIGN_RULESETS):
            raise ValueError(f"unsupported ruleset: {ruleset}")
        if not 1 <= battle_count <= MAX_BATTLES or (
            ruleset not in CAMPAIGN_RULESETS and battle_count != 1
        ):
            raise ValueError("battle_count must be 1..6 for v3 and 1 for legacy rules")
        self._ruleset = ruleset
        self._battle_count = battle_count
        self._battle_index = 1
        self._rng = random.Random(seed)
        self._enemy_type = self._choose_enemy(1)
        enemy_hp, self._enemy_damage_range = ENEMY_SPECS[self._enemy_type]
        if ruleset == "v1":
            self._enemy_damage_range = (20, 35)
        self._player = PlayerState(
            hp=PLAYER_MAX_HP,
            max_hp=PLAYER_MAX_HP,
            potions=2 if ruleset == "v1" else INITIAL_POTIONS,
            ep=INITIAL_EP,
            max_ep=INITIAL_EP,
            magic=INITIAL_MAGIC,
        )
        self._slime = CombatantState(hp=enemy_hp, max_hp=enemy_hp)
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
        """玩家攻击当前敌人；若敌人存活，立即反击。"""
        self._ensure_active()
        self._strike("attack", 0)
        self._advance_power_strike_cooldown()

    def power_strike(self) -> None:
        """重击追加 12 点伤害；随后需要完成两次其他战斗动作。"""
        self._ensure_active()
        if self._ruleset not in CAMPAIGN_RULESETS:
            raise InvalidCombatAction("skill_unavailable", "skill is unavailable in legacy battles")
        if self._player.power_strike_cooldown:
            raise InvalidCombatAction("skill_cooldown", "power strike is cooling down")
        self._strike("power_strike", POWER_STRIKE_BONUS)
        self._player.power_strike_cooldown = POWER_STRIKE_COOLDOWN

    def _strike(self, kind: Literal["attack", "power_strike"], bonus: int) -> None:
        """普通攻击和重击共用击杀、反击、掉落与升级顺序。"""
        turn = self._next_turn()
        damage = self._rng.randint(*PLAYER_DAMAGE_RANGE) + bonus
        if self._ruleset in CAMPAIGN_RULESETS:
            damage += LEVEL_DAMAGE_BONUS * (self._player.level - 1)
        if kind == "power_strike":
            damage = self._skill_damage(kind, damage)
        self._slime.hp = max(0, self._slime.hp - damage)
        self._append_event(
            CombatEvent(
                turn=turn,
                actor="player",
                kind=kind,
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
        if enemy_defeated and self._ruleset in ("v2", *CAMPAIGN_RULESETS):
            self._award_drop(turn)
        if enemy_defeated and self._ruleset in CAMPAIGN_RULESETS:
            self._award_experience(turn)
        self._turn = turn

    def next_battle(self) -> None:
        """获胜后主动进入下一场；玩家状态完整继承。"""
        if self._ruleset not in CAMPAIGN_RULESETS or self._status is not BattleStatus.WON:
            raise InvalidCombatAction("battle_not_won", "win the current battle first")
        if self._battle_index >= self._battle_count:
            raise InvalidCombatAction("campaign_complete", "all battles are complete")
        self._battle_index += 1
        self._enemy_type = self._choose_enemy(self._battle_index)
        enemy_hp, self._enemy_damage_range = ENEMY_SPECS[self._enemy_type]
        self._slime = CombatantState(hp=enemy_hp, max_hp=enemy_hp)
        self._status = BattleStatus.ACTIVE
        turn = self._next_turn()
        self._append_event(
            CombatEvent(
                turn=turn,
                actor="player",
                kind="next_battle",
                value=self._battle_index,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
                potions=self._player.potions,
            )
        )
        self._turn = turn

    def use_potion(self) -> None:
        """玩家使用药水恢复 25 点生命（不超过上限），随后敌人反击。"""
        self._ensure_active()
        if self._ruleset == "v1" and self._player.hp == self._player.max_hp:
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
                value=healed if self._ruleset == "v1" else POTION_HEAL,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
                potions=self._player.potions,
            )
        )
        self._retaliate(turn)
        self._turn = turn
        self._advance_power_strike_cooldown()

    def cast_spell(
        self, action: Literal["cast_spark", "cast_flame", "cast_meteor", "cast_heal"]
    ) -> None:
        """消耗 EP 施放伤害或治疗魔法；魔法无冷却。"""
        self._ensure_active()
        if self._ruleset not in MAGIC_RULESETS:
            raise InvalidCombatAction("spell_unavailable", "spells require v4 or later rules")
        cost = HEAL_SPELL_COST if action == "cast_heal" else SPELL_DAMAGE[action][0]
        if self._player.ep < cost:
            raise InvalidCombatAction("not_enough_ep", "not enough EP")
        turn = self._next_turn()
        self._player.ep -= cost
        if action == "cast_heal":
            power = HEAL_SPELL_BASE + self._player.magic
            self._player.hp += min(power, self._player.max_hp - self._player.hp)
            self._append_event(
                CombatEvent(
                    turn=turn,
                    actor="player",
                    kind=action,
                    value=power,
                    player_hp=self._player.hp,
                    slime_hp=self._slime.hp,
                    potions=self._player.potions,
                )
            )
            self._retaliate(turn)
        else:
            low, high = SPELL_DAMAGE[action][1]
            damage = self._skill_damage(action, self._rng.randint(low, high) + self._player.magic)
            self._slime.hp = max(0, self._slime.hp - damage)
            self._append_event(
                CombatEvent(
                    turn=turn,
                    actor="player",
                    kind=action,
                    value=damage,
                    player_hp=self._player.hp,
                    slime_hp=self._slime.hp,
                    potions=self._player.potions,
                )
            )
            enemy_defeated = self._slime.hp == 0
            if enemy_defeated:
                self._status = BattleStatus.WON
            if self._should_retaliate(enemy_defeated):
                self._retaliate(turn)
            if enemy_defeated:
                self._award_drop(turn)
                self._award_experience(turn)
        self._turn = turn
        self._advance_power_strike_cooldown()

    def _advance_power_strike_cooldown(self) -> None:
        if self._ruleset in CAMPAIGN_RULESETS and self._player.power_strike_cooldown:
            self._player.power_strike_cooldown -= 1

    def snapshot(self) -> GameSnapshot:
        """返回当前状态的深拷贝快照，避免外部持有可变内部状态。"""
        return GameSnapshot(
            session_id=self._session_id,
            seed=self._seed,
            status=self._status,
            turn=self._turn,
            player=self._player.model_copy(deep=True),
            slime=self._slime.model_copy(deep=True),
            enemy_type=self._enemy_type,
            enemy_level=ENEMY_LEVELS[self._enemy_type],
            ruleset=self._ruleset,
            battle_count=self._battle_count,
            battle_index=self._battle_index,
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

    def _skill_damage(self, action: str, damage: int) -> int:
        """战技与伤害魔法的正常威力；靶场可覆盖此窄扩展点。"""
        return damage

    def _retaliate(self, turn: int) -> None:
        """当前敌人反击；玩家生命降至 0 则战斗失败。"""
        damage = self._rng.randint(*self._enemy_damage_range)
        self._player.hp = max(0, self._player.hp - damage)
        self._append_event(
            CombatEvent(
                turn=turn,
                actor=self._enemy_type,
                kind="retaliate",
                value=damage,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
            )
        )
        if self._player.hp == 0:
            self._status = BattleStatus.LOST

    def _drop_reward(self) -> tuple[str, int] | None:
        """击杀奖励；随机数来自会话私有生成器，重放时结果一致。"""
        if self._enemy_type == "slime":
            return "gold", self._rng.randint(1, 3)
        if self._enemy_type == "goblin":
            return "gold", self._rng.randint(10, 20)
        if self._ruleset in MAGIC_RULESETS and self._enemy_type in NEW_ENEMY_GOLD:
            return "gold", self._rng.randint(*NEW_ENEMY_GOLD[self._enemy_type])
        return None

    def _experience_reward(self) -> tuple[str, int] | None:
        """v3 击杀经验奖励；靶场只可在此窄扩展点制造掉落异常。"""
        return "exp", ENEMY_EXP[self._enemy_type]

    def _experience_requirement(self, level: int) -> int:
        """v5 的下一级门槛随当前等级线性增加；旧规则保持固定 15。"""
        return EXP_PER_LEVEL * level if self._ruleset == "v5" else EXP_PER_LEVEL

    def _levels_after_experience(self, gained: int) -> int:
        """返回本次应升级次数，供靶场注入升级异常。"""
        remaining = self._player.exp + gained
        level = self._player.level
        while remaining >= self._experience_requirement(level):
            remaining -= self._experience_requirement(level)
            level += 1
        return level - self._player.level

    def _award_experience(self, turn: int) -> None:
        reward = self._experience_reward()
        if reward is None:
            return
        item, amount = reward
        self._append_event(
            CombatEvent(
                turn=turn,
                actor=self._enemy_type,
                kind="drop",
                value=amount,
                item=item,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
            )
        )
        if item != "exp":
            return
        levels = self._levels_after_experience(amount)
        total_exp = self._player.exp + amount
        # 经验余量按真实达到的门槛顺延；异常靶场可故意升级次数不符。
        remaining = total_exp
        for level in range(self._player.level, self._player.level + levels):
            threshold = self._experience_requirement(level)
            if remaining >= threshold:
                remaining -= threshold
        self._player.exp = remaining
        for _ in range(levels):
            self._player.level += 1
            self._player.max_hp += LEVEL_HP_BONUS
            self._player.hp = self._player.max_hp
            self._player.potions += 1
            if self._ruleset in MAGIC_RULESETS:
                self._player.max_ep += LEVEL_EP_BONUS
                self._player.ep = self._player.max_ep
                self._player.magic += LEVEL_MAGIC_BONUS
            self._append_event(
                CombatEvent(
                    turn=turn,
                    actor="player",
                    kind="level_up",
                    value=self._player.level,
                    player_hp=self._player.hp,
                    slime_hp=self._slime.hp,
                    potions=self._player.potions,
                )
            )

    def _choose_enemy(self, battle_index: int) -> str:
        if self._ruleset == "v1":
            return "slime"
        roll = random.Random(self._seed + 7 + (battle_index - 1) * 104729).random()
        if self._ruleset in MAGIC_RULESETS:
            scaled = roll * 100
            cumulative = 0
            for enemy, weight in V4_ENEMY_WEIGHTS:
                cumulative += weight
                if scaled < cumulative:
                    return enemy
            return "dragon"
        return "slime" if roll < 0.6 else "goblin" if roll < 0.9 else "troll"

    def _award_drop(self, turn: int) -> None:
        reward = self._drop_reward()
        if reward is None:
            return
        item, amount = reward
        if item == "gold":
            self._player.gold += amount
        self._append_event(
            CombatEvent(
                turn=turn,
                actor=self._enemy_type,
                kind="drop",
                value=amount,
                item=item,
                player_hp=self._player.hp,
                slime_hp=self._slime.hp,
            )
        )

    def _append_event(self, event: CombatEvent) -> None:
        self._events.append(event)


def new_session_identity(seed: int | None) -> tuple[str, int]:
    """生成新会话的标识与随机种子。

    未提供种子时生成一个随机种子；种子随快照返回，因此无论是否显式指定，
    任何一场战斗都可回放。标识与种子的生成只有这一处实现，
    应用装配与缺陷靶场都从这里取，避免两处各写一份而逐渐不一致。
    """
    return uuid.uuid4().hex, secrets.randbits(31) if seed is None else seed


def create_combat_session(seed: int | None = None, battle_count: int = 1) -> CombatSession:
    """创建新战斗会话（正常规则）。"""
    session_id, resolved_seed = new_session_identity(seed)
    return CombatSession(
        session_id=session_id, seed=resolved_seed, ruleset="v5", battle_count=battle_count
    )
