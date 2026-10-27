"""公开预览标签页的登记、心跳与关闭通知。"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gamepilot.preview_visitors import preview_manager

router = APIRouter(prefix="/api/v1/preview-visitor", tags=["公开预览访客"])


class VisitorPage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    visitor_id: str = Field(min_length=36, max_length=36)
    page_id: str = Field(min_length=36, max_length=36)

    @field_validator("visitor_id", "page_id")
    @classmethod
    def canonical_uuid4(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError:
            raise ValueError("必须是标准 UUID4") from None
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("必须是标准 UUID4")
        return value

    def ids(self) -> tuple[str, str]:
        # 与请求头共用同一严格格式；只接受浏览器生成的标准 UUID4。
        return self.visitor_id, self.page_id


@router.post("/open")
async def open_visitor(payload: VisitorPage, request: Request) -> dict[str, bool]:
    manager = preview_manager(request)
    if manager is None:
        return {"preview_enabled": False}
    await manager.open(*payload.ids())
    return {"preview_enabled": True}


@router.post("/touch", status_code=204)
def touch_visitor(request: Request) -> None:
    manager = preview_manager(request)
    if manager is not None:
        manager.require(request)


@router.post("/close", status_code=204)
async def close_visitor(payload: VisitorPage, request: Request) -> None:
    manager = preview_manager(request)
    if manager is not None:
        await manager.close(*payload.ids())
