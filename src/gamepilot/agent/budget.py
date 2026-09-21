"""预算账本：动作、模型调用、格式纠正与总时限。

三条硬规则：

1. **调用前检查**。每次动作/模型调用之前先申请额度；额度不足直接抛出
   `BudgetExceeded`，不会先发请求再判断——超限后不得再发模型或游戏请求。
2. **计数只在这里**。计数器属于这个对象，不属于工作流状态：LangGraph 重新进入
   某个节点不会把计数重置回 0，`recursion_limit` 也不替代这些预算。
3. **时钟可注入**。测试用可控时钟把时间推到临近总时限，不需要真的 sleep。

超时的取值是 `min(单次上限, 剩余时间)`：剩余时间已经不多时，一次「正常」的
30 秒模型超时会把整个任务拖过总时限，所以必须按剩余时间收紧。
"""

import time
from collections.abc import Callable

from .models import BudgetSpec


class BudgetExceeded(Exception):
    """预算不足：在**发起请求之前**被拦下。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class Budget:
    """一次任务的预算账本；所有计数都在这里，节点不自行记账。"""

    def __init__(self, spec: BudgetSpec, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._spec = spec
        self._clock = clock
        self._started = clock()
        self._actions = 0
        self._model_calls = 0
        self._format_retries = 0

    @property
    def spec(self) -> BudgetSpec:
        return self._spec

    @property
    def actions(self) -> int:
        return self._actions

    @property
    def model_calls(self) -> int:
        return self._model_calls

    @property
    def format_retries(self) -> int:
        return self._format_retries

    # ------------------------------------------------------------------ 时间

    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def remaining_seconds(self) -> float:
        return self._spec.total_timeout_seconds - self.elapsed_seconds()

    def check_time(self) -> None:
        """总时限检查：在每一次外部调用之前调用它。"""
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise BudgetExceeded(
                "time_budget",
                f"任务总时限 {self._spec.total_timeout_seconds:g} 秒已耗尽",
            )

    def request_timeout(self, cap: float) -> float:
        """单次请求超时：到期先拒绝，否则取单次上限与剩余时间的较小值。"""
        self.check_time()
        return min(cap, self.remaining_seconds())

    # ------------------------------------------------------------------ 计数

    def start_action(self) -> int:
        """申请一次游戏动作尝试的额度，返回本次尝试的序号（从 1 开始）。"""
        if self._actions >= self._spec.max_action_attempts:
            raise BudgetExceeded(
                "action_budget",
                f"动作尝试预算 {self._spec.max_action_attempts} 次已用尽",
            )
        self._actions += 1
        return self._actions

    def start_model_call(self, *, repair: bool = False) -> int:
        """申请一次模型调用的额度，返回调用序号（从 1 开始）。

        `repair=True` 表示这次调用是因为上一次输出不合规而重发：
        它既占用模型调用预算，也占用全任务累计的格式纠正预算。
        """
        if repair and self._format_retries >= self._spec.max_format_retries:
            raise BudgetExceeded(
                "model_format_error",
                f"格式纠正次数 {self._spec.max_format_retries} 次已用尽",
            )
        if self._model_calls >= self._spec.max_model_calls:
            raise BudgetExceeded(
                "model_call_budget",
                f"模型调用预算 {self._spec.max_model_calls} 次已用尽",
            )
        self._model_calls += 1
        if repair:
            self._format_retries += 1
        return self._model_calls


__all__ = ["Budget", "BudgetExceeded"]
