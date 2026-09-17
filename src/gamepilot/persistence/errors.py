"""持久化错误：仓储实现向调用方暴露的稳定错误类型。

这些错误只描述持久化层的事实，消息由仓储自己构造，
**不携带** SQLAlchemy 原始异常文本、连接串或凭据，
因此可以安全地映射成 HTTP 响应（见 `gamepilot.api.errors`）。
"""

from gamepilot.domain.errors import GamePilotError


class PersistenceError(GamePilotError):
    """持久化相关错误的基类。"""

    code = "persistence_error"


class PersistenceConflictError(PersistenceError):
    """已存历史与待保存内容冲突：分叉、缩短或并发创建。映射为 HTTP 409。

    这是「明确拒绝」而不是「临时故障」：重试同样的写入不会成功，
    调用方需要重新读取最新状态。
    """

    code = "persistence_conflict"


class PersistenceUnavailableError(PersistenceError):
    """数据库暂时不可用（无法建立连接、连接中断）。映射为 HTTP 503。

    不要把 SQL 编程错误等确定性故障归入此类。
    """

    code = "persistence_unavailable"


class PersistenceInconsistentError(PersistenceError):
    """存储的数据无法确定性恢复：种子非法、事件不连续或与状态不符。映射为 HTTP 500。

    仓储遇到这种情况会停止读取并报错，不会自动修复、跳过事件或返回猜测结果。
    """

    code = "persistence_inconsistent"
