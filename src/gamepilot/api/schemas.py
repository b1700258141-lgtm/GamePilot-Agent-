"""API 请求与错误响应的 Schema。

游戏状态响应直接复用领域模型 GameSnapshot，避免维护两份重复定义。
"""

from typing import Literal

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    """创建会话请求；seed 可选，未提供时由服务生成并在响应中返回。"""

    seed: int | None = None


class ActionRequest(BaseModel):
    """动作请求；非法动作值由 Pydantic 校验为 HTTP 422。"""

    action: Literal["attack", "use_potion"]


class ErrorResponse(BaseModel):
    """稳定的错误结构：机器可识别的错误码 + 可阅读消息。"""

    code: str
    message: str
