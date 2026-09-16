"""进程内内存仓储实现。

数据只保存在当前进程内，服务重启后丢失——这是第一阶段明确接受的取舍，
第二阶段将替换为数据库实现。
"""

from gamepilot.domain.combat import CombatSession
from gamepilot.domain.errors import SessionNotFoundError


class InMemorySessionRepository:
    """以 dict 保存会话的内存仓储。"""

    def __init__(self) -> None:
        self._sessions: dict[str, CombatSession] = {}

    def save(self, session: CombatSession) -> None:
        """保存或更新会话。"""
        self._sessions[session.session_id] = session

    def get(self, session_id: str) -> CombatSession:
        """按 ID 获取会话；不存在时抛出 SessionNotFoundError。"""
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(session_id) from None
