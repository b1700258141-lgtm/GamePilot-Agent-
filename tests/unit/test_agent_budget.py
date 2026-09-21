"""预算账本：调用前检查、计数不可重置、时钟可注入。

时间相关用例全部使用可控时钟把时间推到期满，**不做真实 sleep**：
`time.sleep(120)` 那样的测试既慢又不稳定，推时钟才能精确验证
「到期前一刻还允许、到期即拒绝」这条边界。
"""

import pytest
from pydantic import ValidationError

from gamepilot.agent.budget import Budget, BudgetExceeded
from gamepilot.agent.models import BudgetSpec
from gamepilot.testing.rules import MAX_ACTION_ATTEMPTS


class Clock:
    """可控时钟：只有显式推进才走时间。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ------------------------------------------------------------------ 默认值契约


def test_default_budget_matches_the_agreed_limits() -> None:
    """保守默认值是被验收的对外契约，改动必须是有意的。"""
    spec = BudgetSpec()
    assert spec.max_action_attempts == 10
    assert spec.max_model_calls == 12
    assert spec.max_format_retries == 2
    assert spec.model_timeout_seconds == 30.0
    assert spec.http_timeout_seconds == 5.0
    assert spec.total_timeout_seconds == 120.0
    assert spec.max_output_tokens == 512


def test_action_limit_cannot_exceed_the_existing_case_limit() -> None:
    """动作上限不得放宽到超过 testing 的既有上限：报告必须仍可被 replay 读取。"""
    with pytest.raises(ValidationError):
        BudgetSpec(max_action_attempts=MAX_ACTION_ATTEMPTS + 1)
    assert BudgetSpec(max_action_attempts=MAX_ACTION_ATTEMPTS).max_action_attempts == (
        MAX_ACTION_ATTEMPTS
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_action_attempts", 0),
        ("max_model_calls", 0),
        ("max_output_tokens", 0),
        ("max_format_retries", -1),
        ("model_timeout_seconds", 0),
        ("http_timeout_seconds", -1),
        ("total_timeout_seconds", 0),
    ],
)
def test_non_positive_limits_are_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        BudgetSpec(**{field: value})


# ------------------------------------------------------------------ 计数预算


def test_action_budget_refuses_before_the_extra_call() -> None:
    budget = Budget(BudgetSpec(max_action_attempts=2))
    assert [budget.start_action(), budget.start_action()] == [1, 2]
    assert budget.actions == 2
    with pytest.raises(BudgetExceeded) as exc:
        budget.start_action()
    assert exc.value.reason == "action_budget"
    # 被拒绝的那次不计入：计数只反映真正批准过的额度。
    assert budget.actions == 2


def test_model_call_budget_refuses_before_the_extra_call() -> None:
    budget = Budget(BudgetSpec(max_model_calls=1))
    assert budget.start_model_call() == 1
    with pytest.raises(BudgetExceeded) as exc:
        budget.start_model_call()
    assert exc.value.reason == "model_call_budget"
    assert budget.model_calls == 1


def test_format_retries_are_global_and_separate_from_decisions() -> None:
    """格式纠正次数是全任务累计的，且只统计「因上次不合规而重发」的调用。"""
    budget = Budget(BudgetSpec(max_model_calls=10, max_format_retries=2))
    budget.start_model_call()  # 正常决策，不占格式额度
    assert budget.format_retries == 0
    budget.start_model_call(repair=True)
    budget.start_model_call(repair=True)
    assert (budget.format_retries, budget.model_calls) == (2, 3)
    with pytest.raises(BudgetExceeded) as exc:
        budget.start_model_call(repair=True)
    assert exc.value.reason == "model_format_error"
    # 格式额度用完不等于模型额度用完：一次正常决策仍然允许。
    assert budget.start_model_call() == 4


def test_counts_are_not_reset_by_re_entry() -> None:
    """重新「进入」流程不会重置计数：计数住在 Budget 上，不在工作流状态里。"""
    budget = Budget(BudgetSpec(max_action_attempts=3))
    for _ in range(3):
        budget.start_action()
    for _ in range(5):  # 模拟节点被重复进入
        with pytest.raises(BudgetExceeded):
            budget.start_action()
    assert budget.actions == 3


# ------------------------------------------------------------------ 时间预算


def test_time_budget_boundary_is_exact() -> None:
    clock = Clock()
    budget = Budget(BudgetSpec(total_timeout_seconds=120.0), clock=clock)
    assert budget.remaining_seconds() == pytest.approx(120.0)
    clock.advance(119.999)
    budget.check_time()  # 到期前一刻仍然允许
    clock.advance(0.001)
    with pytest.raises(BudgetExceeded) as exc:
        budget.check_time()
    assert exc.value.reason == "time_budget"


def test_request_timeout_shrinks_to_the_remaining_time() -> None:
    """剩余时间不多时，单次超时必须收紧，否则一次「正常」超时会拖过总时限。"""
    clock = Clock()
    budget = Budget(BudgetSpec(total_timeout_seconds=120.0), clock=clock)
    assert budget.request_timeout(30.0) == pytest.approx(30.0)  # 取单次上限
    clock.advance(115.0)
    assert budget.request_timeout(30.0) == pytest.approx(5.0)  # 取剩余时间
    clock.advance(4.999)
    assert budget.request_timeout(30.0) == pytest.approx(0.001)  # 始终为正
    clock.advance(0.001)
    with pytest.raises(BudgetExceeded) as exc:
        budget.request_timeout(30.0)
    assert exc.value.reason == "time_budget"


def test_time_budget_does_not_touch_the_other_counters() -> None:
    """时间到期不会被记成动作或模型调用：三种预算互不冒充。"""
    clock = Clock()
    budget = Budget(BudgetSpec(total_timeout_seconds=1.0), clock=clock)
    clock.advance(2.0)
    with pytest.raises(BudgetExceeded) as exc:
        budget.check_time()
    assert exc.value.reason == "time_budget"
    assert (budget.actions, budget.model_calls, budget.format_retries) == (0, 0, 0)


def test_elapsed_uses_the_injected_clock() -> None:
    clock = Clock(start=0.0)
    budget = Budget(BudgetSpec(), clock=clock)
    clock.advance(7.5)
    assert budget.elapsed_seconds() == pytest.approx(7.5)
    assert budget.remaining_seconds() == pytest.approx(112.5)
