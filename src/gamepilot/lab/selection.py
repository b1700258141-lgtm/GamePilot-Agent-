"""网页靶场的确定性缺陷选择；选择结果不进入 Agent 观测。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .profiles import FAULT_IDS

RANDOM_FAULT_PERCENT = 30
_HASH_PREFIX = "gamepilot-random-fault-v1:"


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
