"""Agent benchmark 的整批 Token/费用口径。"""

import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph", reason='未安装 agent extra：pip install -e ".[dev,agent]"')

from gamepilot.agent.models import TokenUsage
from gamepilot.benchmark.agent_eval import summarize_spend
from gamepilot.benchmark.agent_models import AgentPricing, AgentUsageCell


def test_all_known_usage_and_explicit_prices_produce_a_recomputable_total() -> None:
    pricing = AgentPricing(
        currency="TEST",
        input_price_per_million=2.0,
        output_price_per_million=4.0,
    )
    spend = [
        AgentUsageCell(usage=TokenUsage.of(100, 20)),
        AgentUsageCell(usage=TokenUsage.of(50, 30)),
    ]

    known, unknown, usage, cost = summarize_spend(
        spend,
        cells_planned=2,
        pricing=pricing,
    )

    assert (known, unknown) == (2, 0)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (150, 50, 200)
    assert cost.currency == "TEST"
    assert cost.amount == pytest.approx((150 * 2.0 + 50 * 4.0) / 1_000_000)


def test_total_tokens_preserves_the_provider_totals_instead_of_rederiving_them() -> None:
    _, _, usage, _ = summarize_spend(
        [
            AgentUsageCell(usage=TokenUsage.of(100, 20, total=125)),
            AgentUsageCell(usage=TokenUsage.of(50, 30, total=90)),
        ],
        cells_planned=2,
        pricing=None,
    )

    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (150, 50, 215)


def test_one_unknown_cell_makes_the_batch_usage_and_cost_unknown() -> None:
    pricing = AgentPricing(
        currency="TEST",
        input_price_per_million=2.0,
        output_price_per_million=4.0,
    )
    spend = [
        AgentUsageCell(usage=TokenUsage.of(100, 20)),
        AgentUsageCell(usage=TokenUsage.unknown()),
    ]

    known, unknown, usage, cost = summarize_spend(
        spend,
        cells_planned=2,
        pricing=pricing,
    )

    assert (known, unknown) == (1, 1)
    assert usage == TokenUsage.unknown()
    assert cost.amount is None
    assert cost.method == "tokens-unknown"


def test_known_usage_without_prices_keeps_only_the_token_total() -> None:
    known, unknown, usage, cost = summarize_spend(
        [AgentUsageCell(usage=TokenUsage.of(10, 5))],
        cells_planned=1,
        pricing=None,
    )

    assert (known, unknown, usage.total_tokens) == (1, 0, 15)
    assert cost.amount is None
    assert cost.method == "none"


@pytest.mark.parametrize(
    "payload",
    [
        {"currency": "", "input_price_per_million": 1.0, "output_price_per_million": 2.0},
        {"currency": "   ", "input_price_per_million": 1.0, "output_price_per_million": 2.0},
        {"currency": "TEST", "input_price_per_million": -1.0, "output_price_per_million": 2.0},
        {"currency": "TEST", "input_price_per_million": 1.0, "output_price_per_million": -2.0},
        {
            "currency": "TEST",
            "input_price_per_million": float("inf"),
            "output_price_per_million": 2.0,
        },
        {
            "currency": "TEST",
            "input_price_per_million": 1.0,
            "output_price_per_million": float("nan"),
        },
        {
            "currency": "TEST",
            "input_price_per_million": "1.0",
            "output_price_per_million": 2.0,
        },
    ],
)
def test_pricing_rejects_blank_currency_and_negative_prices(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentPricing.model_validate(payload)
