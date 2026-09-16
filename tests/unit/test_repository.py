"""内存仓储单元测试。"""

import pytest

from gamepilot.domain.combat import create_combat_session
from gamepilot.domain.errors import SessionNotFoundError
from gamepilot.repositories.memory import InMemorySessionRepository


def test_save_and_get_roundtrip() -> None:
    repo = InMemorySessionRepository()
    session = create_combat_session(seed=1)
    repo.save(session)
    assert repo.get(session.session_id) is session


def test_save_overwrites_existing_session() -> None:
    repo = InMemorySessionRepository()
    session = create_combat_session(seed=1)
    repo.save(session)
    session.attack()
    repo.save(session)
    assert repo.get(session.session_id).snapshot().turn == 1


def test_get_missing_session_raises() -> None:
    repo = InMemorySessionRepository()
    with pytest.raises(SessionNotFoundError) as exc_info:
        repo.get("no-such-id")
    assert exc_info.value.code == "session_not_found"
