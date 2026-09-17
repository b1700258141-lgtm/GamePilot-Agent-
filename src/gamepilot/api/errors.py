"""API 层异常处理：把领域错误与持久化错误映射为稳定的 HTTP 错误结构。

错误响应只包含错误码与消息，不向客户端暴露堆栈或数据库细节。

日志约定：只记录操作、稳定错误码、异常类型，以及仓储自己构造的安全消息；
不记录连接串、凭据或带参数的原始 SQLAlchemy 异常文本。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from gamepilot.domain.errors import InvalidCombatAction, SessionNotFoundError
from gamepilot.persistence.errors import (
    PersistenceConflictError,
    PersistenceError,
    PersistenceUnavailableError,
)

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """注册领域错误与持久化错误到 HTTP 状态码的映射。"""

    @app.exception_handler(SessionNotFoundError)
    async def handle_session_not_found(
        _request: Request, exc: SessionNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(InvalidCombatAction)
    async def handle_invalid_action(_request: Request, exc: InvalidCombatAction) -> JSONResponse:
        return JSONResponse(status_code=409, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(PersistenceConflictError)
    async def handle_persistence_conflict(
        _request: Request, exc: PersistenceConflictError
    ) -> JSONResponse:
        logger.info("persistence conflict: code=%s detail=%s", exc.code, exc.message)
        return JSONResponse(status_code=409, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(PersistenceUnavailableError)
    async def handle_persistence_unavailable(
        _request: Request, exc: PersistenceUnavailableError
    ) -> JSONResponse:
        logger.warning("persistence unavailable: code=%s detail=%s", exc.code, exc.message)
        return JSONResponse(status_code=503, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(PersistenceError)
    async def handle_persistence_error(_request: Request, exc: PersistenceError) -> JSONResponse:
        """兜底仓储错误，包含无法确定性恢复的数据不一致（persistence_inconsistent）。"""
        logger.error(
            "persistence error: code=%s error_type=%s detail=%s",
            exc.code,
            type(exc).__name__,
            exc.message,
        )
        return JSONResponse(status_code=500, content={"code": exc.code, "message": exc.message})

    @app.exception_handler(SQLAlchemyError)
    async def handle_sqlalchemy_error(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
        """处理未分类的数据库错误，不让原始 SQLAlchemy 异常逃逸到服务器日志。"""
        logger.error("database internal error: error_type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "internal server error"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        """未预期错误统一返回 500。

        只记录异常类型：SQLAlchemy 的原始异常文本可能包含连接信息或带参数的 SQL。
        """
        logger.error("unexpected error: error_type=%s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "internal server error"},
        )
