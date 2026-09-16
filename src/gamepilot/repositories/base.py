"""游戏会话仓储抽象。

第一阶段只有进程内内存实现；第二阶段将以 PostgreSQL 实现替换本协议，
领域层与 API 层不需要改动。
"""

from typing import Protocol

from gamepilot.domain.combat import CombatSession


class SessionRepository(Protocol):
    """保存与读取战斗会话的最小协议。"""

    def save(self, session: CombatSession) -> None:
        """保存或更新会话。"""
        ...

    def get(self, session_id: str) -> CombatSession:
        """按 ID 获取会话；不存在时抛出 SessionNotFoundError。"""
        ...
