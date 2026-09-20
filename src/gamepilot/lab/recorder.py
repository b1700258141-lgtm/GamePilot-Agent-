"""缺陷触发记录：只对宿主可见的最小证据。

记录只在缺陷行为**真正偏离正常规则**时产生（例如缺失生命值小于 25 时才
可能治疗超限、敌人已经死亡时才可能「死后反击」），正常分支不记录命中。

它的用途只有一个：让评测器核对「这次运行里标准答案是否真的成立」。
它不能代替 oracle 判定，两者必须相互独立：

- 记录不进入 HTTP 响应，不进入 GameClient，也不被执行器或判定器读取；
- 判定器只看线上观测，因此「lab 记录到触发」与「oracle 判定规则失败」
  是两条独立证据，一致才有意义。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultTrigger:
    """一次缺陷触发：按会话与缺陷编号标识，`detail` 只是简短的触发说明。"""

    session_id: str
    fault_id: str
    detail: str


class TriggerRecorder:
    """按 session_id 保存触发记录；同一会话只保留首个记录。"""

    def __init__(self) -> None:
        self._records: dict[str, FaultTrigger] = {}

    def record(self, session_id: str, fault_id: str, detail: str) -> None:
        self._records.setdefault(
            session_id, FaultTrigger(session_id=session_id, fault_id=fault_id, detail=detail)
        )

    def get(self, session_id: str | None) -> FaultTrigger | None:
        """取出某次会话的触发记录；没有触发时返回 None。"""
        if session_id is None:
            return None
        return self._records.get(session_id)

    def all(self) -> tuple[FaultTrigger, ...]:
        return tuple(self._records.values())

    def __len__(self) -> int:
        return len(self._records)
