"""公开预览的浏览器标签页生命周期与访客隔离。

浏览器关闭通知只能尽力送达；短暂宽限保护刷新，心跳过期负责兜底。
每位访客持有独立内存仓储，其他访客无法通过猜测会话 ID 读取其战斗。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from gamepilot.agent.skills import DEFAULT_SKILL, Skill
from gamepilot.repositories.memory import InMemorySessionRepository

if TYPE_CHECKING:
    from gamepilot.web_runs import AgentWebRunManager

VISITOR_HEADER = "X-GamePilot-Visitor"
PAGE_HEADER = "X-GamePilot-Page"
CLOSE_GRACE_SECONDS = 5.0
IDLE_SECONDS = 300.0
SWEEP_SECONDS = 30.0
MAX_SKILLS_PER_VISITOR = 10
LOGGER = logging.getLogger(__name__)


@dataclass
class _Visitor:
    page_id: str
    repository: InMemorySessionRepository = field(default_factory=InMemorySessionRepository)
    skills: dict[str, Skill] = field(default_factory=dict)
    last_seen: float = 0.0
    close_task: asyncio.Task[None] | None = None


class PreviewVisitorManager:
    """仅供公开预览单进程使用；token 是每个标签页的随机 bearer 凭据。"""

    def __init__(
        self,
        runs: AgentWebRunManager,
        *,
        grace_seconds: float = CLOSE_GRACE_SECONDS,
        idle_seconds: float = IDLE_SECONDS,
        sweep_seconds: float = SWEEP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runs = runs
        self._visitors: dict[str, _Visitor] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._idle_seconds = idle_seconds
        self._sweep_seconds = sweep_seconds
        self._sweep_task: asyncio.Task[None] | None = None

    @staticmethod
    def _token(value: str | None) -> str:
        try:
            parsed = UUID(value or "")
        except ValueError:
            raise HTTPException(status_code=403, detail="访客会话无效，请刷新页面") from None
        if parsed.version != 4 or str(parsed) != value:
            raise HTTPException(status_code=403, detail="访客会话无效，请刷新页面")
        return value

    async def open(self, visitor_id: str, page_id: str) -> None:
        visitor_id = self._token(visitor_id)
        page_id = self._token(page_id)
        async with self._lock:
            visitor = self._visitors.get(visitor_id)
            if visitor is None:
                visitor = _Visitor(page_id=page_id)
                self._visitors[visitor_id] = visitor
            else:
                if visitor.close_task is not None:
                    visitor.close_task.cancel()
                    visitor.close_task = None
                visitor.page_id = page_id
            visitor.last_seen = self._clock()

    def require(self, request: Request) -> tuple[str, InMemorySessionRepository]:
        visitor_id = self._token(request.headers.get(VISITOR_HEADER))
        page_id = self._token(request.headers.get(PAGE_HEADER))
        visitor = self._visitors.get(visitor_id)
        if visitor is None or visitor.page_id != page_id:
            raise HTTPException(status_code=403, detail="访客会话已结束，请刷新页面")
        visitor.last_seen = self._clock()
        return visitor_id, visitor.repository

    def list_skills(self, visitor_id: str) -> list[Skill]:
        visitor = self._visitors.get(visitor_id)
        if visitor is None:
            raise HTTPException(status_code=403, detail="访客会话已结束，请刷新页面")
        return [DEFAULT_SKILL, *visitor.skills.values()]

    def add_skill(self, visitor_id: str, name: str, instructions: str) -> Skill:
        visitor = self._visitors.get(visitor_id)
        if visitor is None:
            raise HTTPException(status_code=403, detail="访客会话已结束，请刷新页面")
        if len(visitor.skills) >= MAX_SKILLS_PER_VISITOR:
            raise HTTPException(status_code=409, detail="当前标签页最多保存 10 个自定义 skill")
        skill = Skill(skill_id=str(uuid4()), name=name, instructions=instructions)
        visitor.skills[skill.skill_id] = skill
        return skill

    async def close(self, visitor_id: str, page_id: str) -> None:
        visitor_id = self._token(visitor_id)
        page_id = self._token(page_id)
        async with self._lock:
            visitor = self._visitors.get(visitor_id)
            if visitor is None or visitor.page_id != page_id:
                return
            if visitor.close_task is not None:
                visitor.close_task.cancel()
            visitor.close_task = asyncio.create_task(self._close_after_grace(visitor_id, page_id))

    async def _close_after_grace(self, visitor_id: str, page_id: str) -> None:
        try:
            await asyncio.sleep(self._grace_seconds)
            await self.clear(visitor_id, page_id=page_id)
        except asyncio.CancelledError:
            return
        except OSError as exc:
            LOGGER.warning("preview cleanup will retry: %s", type(exc).__name__)

    async def clear(self, visitor_id: str, *, page_id: str | None = None) -> None:
        """只清除目标访客；先停止其 Agent，再释放战斗与报告。"""
        async with self._lock:
            visitor = self._visitors.get(visitor_id)
            if visitor is None or (page_id is not None and visitor.page_id != page_id):
                return
            current = asyncio.current_task()
            if visitor.close_task is not None and visitor.close_task is not current:
                visitor.close_task.cancel()
            await self._runs.clear_visitor(visitor_id)
            del self._visitors[visitor_id]

    async def sweep_once(self) -> None:
        expired = [
            (visitor_id, visitor.page_id)
            for visitor_id, visitor in list(self._visitors.items())
            if self._clock() - visitor.last_seen >= self._idle_seconds
        ]
        for visitor_id, page_id in expired:
            try:
                await self.clear(visitor_id, page_id=page_id)
            except OSError as exc:
                LOGGER.warning("preview idle cleanup will retry: %s", type(exc).__name__)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_seconds)
            await self.sweep_once()

    def start(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
            self._sweep_task = None
        for visitor_id in list(self._visitors):
            await self.clear(visitor_id)


def preview_manager(request: Request) -> PreviewVisitorManager | None:
    return getattr(request.app.state, "preview_visitors", None)
