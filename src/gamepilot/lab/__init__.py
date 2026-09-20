"""可控缺陷靶场：把真实游戏缺陷装进独立的内存应用实例。

设计要点：

- 缺陷是**真实的游戏状态变异**，由公开 HTTP 动作触发：靶场只覆盖
  `CombatSession` 的三个窄规则扩展点，动作流程、随机数推进、事件生成
  与快照仍然只有领域层那一份实现；
- 靶场与正常服务互不影响：独立应用、独立内存仓储，且不读取正常配置、
  不创建数据库 Engine；
- profile 在启动时固定，游戏 API 上没有任何读取或切换缺陷的接口；
- 触发记录只对宿主可见，判定器看不到它。

`gamepilot.testing` 保持独立：它不导入本包，只通过 HTTP 观测被测服务。
"""

from .app import LAB_API_TITLE, create_lab_app, lab_profile_of
from .profiles import (
    FAULT_IDS,
    FAULT_POTION_NOT_CONSUMED,
    FAULT_POTION_OVERHEAL,
    FAULT_RETALIATE_AFTER_DEATH,
    PROFILE_CATALOG,
    PROFILE_NORMAL,
    PROFILES,
    FaultProfile,
    UnknownProfileError,
    resolve_profile,
)
from .recorder import FaultTrigger, TriggerRecorder
from .session import LabCombatSession, make_session_factory

__all__ = [
    "FAULT_IDS",
    "FAULT_POTION_NOT_CONSUMED",
    "FAULT_POTION_OVERHEAL",
    "FAULT_RETALIATE_AFTER_DEATH",
    "LAB_API_TITLE",
    "PROFILES",
    "PROFILE_CATALOG",
    "PROFILE_NORMAL",
    "FaultProfile",
    "FaultTrigger",
    "LabCombatSession",
    "TriggerRecorder",
    "UnknownProfileError",
    "create_lab_app",
    "lab_profile_of",
    "make_session_factory",
    "resolve_profile",
]
