"""领域错误：携带机器可识别的错误码，由 API 层映射为 HTTP 状态码。"""


class GamePilotError(Exception):
    """所有领域错误的基类。"""

    code: str = "game_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidCombatAction(GamePilotError):
    """在非法状态下执行动作（战斗已结束、满血吃药、无药水）。映射为 HTTP 409。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SessionNotFoundError(GamePilotError):
    """找不到游戏会话。映射为 HTTP 404。"""

    code = "session_not_found"

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"game session not found: {session_id}")
