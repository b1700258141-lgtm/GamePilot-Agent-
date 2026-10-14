"""网页靶场的确定性缺陷选择；选择结果不进入 Agent 观测。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .profiles import FAULT_IDS

RANDOM_FAULT_PERCENT = 30
_HASH_PREFIX = "gamepilot-random-fault-v1:"
_TIMING_PREFIX = "gamepilot-fault-timing-v1:"
_REPEATABLE_FAULTS = frozenset(("potion_overheal", "potion_not_consumed", "retaliate_after_death"))


@dataclass(frozen=True)
class FaultSelection:
    mode: Literal["random", "forced"]
    fault_ids: tuple[str, ...]


def select_faults(seed: int, forced_faults: tuple[str, ...] = ()) -> FaultSelection:
    """未勾选时 70% 正常、30% 随机一种缺陷；同一 seed 永远得到同一选择。"""
    if len(set(forced_faults)) != len(forced_faults) or not set(forced_faults) <= set(FAULT_IDS):
        raise ValueError("forced_faults must contain distinct known fault IDs")
    if forced_faults:
        ordered = tuple(fault for fault in FAULT_IDS if fault in forced_faults)
        return FaultSelection("forced", ordered)

    digest = hashlib.sha256(f"{_HASH_PREFIX}{seed}".encode("ascii")).digest()
    roll = int.from_bytes(digest[:8], "big")
    if roll >= (1 << 64) * RANDOM_FAULT_PERCENT // 100:
        return FaultSelection("random", ())
    index = int.from_bytes(digest[8:16], "big") % len(FAULT_IDS)
    return FaultSelection("random", (FAULT_IDS[index],))


def activation_opportunity(seed: int, battle_count: int, fault_id: str) -> int:
    """返回缺陷第几次真正具备触发条件时生效，不消耗战斗随机数。"""
    if fault_id not in FAULT_IDS:
        raise ValueError("unknown fault ID")
    if not 1 <= battle_count <= 6:
        raise ValueError("battle_count must be 1..6")
    # 喝药与死亡后动作在单场内仍可能出现多个候选节点；击杀掉落/升级每场最多一次。
    ceiling = max(2, battle_count) if fault_id in _REPEATABLE_FAULTS else battle_count
    digest = hashlib.sha256(
        f"{_TIMING_PREFIX}{seed}:{battle_count}:{fault_id}".encode("ascii")
    ).digest()
    return 1 + int.from_bytes(digest[:8], "big") % ceiling
