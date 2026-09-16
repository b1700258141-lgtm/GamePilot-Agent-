"""API 层异常处理：把领域错误映射为稳定的 HTTP 错误结构。

错误响应只包含错误码与消息，不向客户端暴露堆栈信息。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gamepilot.domain.errors import InvalidCombatAction, SessionNotFoundError


def register_exception_handlers(app: FastAPI) -> None:
    """注册领域错误到 HTTP 状态码的映射。"""

    @app.exception_handler(SessionNotFoundError)
    async def handle_session_not_found(
        _request: Request, exc: SessionNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(InvalidCombatAction)
    async def handle_invalid_action(_request: Request, exc: InvalidCombatAction) -> JSONResponse:
        return JSONResponse(status_code=409, content={"code": exc.code, "message": exc.message})
