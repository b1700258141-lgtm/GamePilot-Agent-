"""触发记录器：只保留首个记录、按会话标识检索、不记录正常分支。"""

from gamepilot.lab import TriggerRecorder


def test_record_then_get_returns_the_trigger() -> None:
    recorder = TriggerRecorder()

    recorder.record("session-a", "fault-x", "第一次偏离")

    trigger = recorder.get("session-a")
    assert trigger is not None
    assert (trigger.session_id, trigger.fault_id, trigger.detail) == (
        "session-a",
        "fault-x",
        "第一次偏离",
    )


def test_first_record_wins_within_a_session() -> None:
    """同一会话的多次偏离只作为一条证据，避免「记录条数」被当成发现次数。"""
    recorder = TriggerRecorder()

    recorder.record("session-a", "fault-x", "第一次")
    recorder.record("session-a", "fault-x", "第二次")
    recorder.record("session-a", "fault-y", "另一个缺陷")

    assert len(recorder) == 1
    trigger = recorder.get("session-a")
    assert trigger is not None
    assert trigger.detail == "第一次"


def test_get_returns_none_for_unknown_or_missing_session() -> None:
    recorder = TriggerRecorder()
    recorder.record("session-a", "fault-x", "偏离")

    assert recorder.get("session-b") is None
    assert recorder.get(None) is None
    assert recorder.get("") is None


def test_all_returns_every_record_in_insertion_order() -> None:
    recorder = TriggerRecorder()

    recorder.record("session-a", "fault-x", "甲")
    recorder.record("session-b", "fault-x", "乙")

    assert [(item.session_id, item.detail) for item in recorder.all()] == [
        ("session-a", "甲"),
        ("session-b", "乙"),
    ]
    assert len(recorder) == 2


def test_empty_recorder_reports_nothing() -> None:
    recorder = TriggerRecorder()

    assert recorder.all() == ()
    assert len(recorder) == 0
