"""命令行配置必须到达 provider，付费开关与离线边界不变。"""

import pytest

from gamepilot.agent import cli as agent_cli
from gamepilot.agent import provider
from gamepilot.benchmark import cli as benchmark_cli


def cli_args(entry: str, *extra: str):
    if entry == "agent":
        parser = agent_cli.build_parser()
        argv = ["run", "--base-url", "http://game.invalid", "--goal", "healing", "--seed", "42"]
    else:
        parser = benchmark_cli.build_parser()
        argv = ["agent"]
    return parser.parse_args([*argv, *extra])


def make_provider(entry: str, args):
    if entry == "agent":
        return agent_cli._make_provider(args)
    return benchmark_cli._make_agent_provider_factory(args)("healing")


@pytest.mark.parametrize("entry", ["agent", "benchmark"])
@pytest.mark.parametrize("thinking", [None, "disabled"])
def test_cli_thinking_reaches_provider(entry, thinking, monkeypatch):
    # 本测试只注入自己的无效占位值，客户端构造也被替换，不读取真实密钥。
    monkeypatch.setenv("GAMEPILOT_TEST_KEY", "test-only")
    monkeypatch.setattr(provider, "_build_client", lambda *_args: object())
    extra = ["--thinking", thinking] if thinking else []
    args = cli_args(
        entry,
        "--provider",
        "anthropic-compatible",
        "--paid",
        "--api-key-env",
        "GAMEPILOT_TEST_KEY",
        *extra,
    )
    configured = make_provider(entry, args)
    assert configured.sampling["thinking"] == (thinking or "provider-default")
    assert configured.model == "deepseek-v4-flash"


@pytest.mark.parametrize("entry", ["agent", "benchmark"])
def test_thinking_does_not_bypass_paid_gate(entry, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("付费开关关闭时不得建立供应商客户端")

    monkeypatch.setattr(provider, "_build_client", forbidden)
    args = cli_args(entry, "--provider", "anthropic-compatible", "--thinking", "disabled")
    with pytest.raises(ValueError, match="--paid"):
        make_provider(entry, args)


@pytest.mark.parametrize("entry", ["agent", "benchmark"])
def test_offline_cannot_silently_ignore_explicit_thinking(entry):
    args = cli_args(
        entry, "--provider", "fake" if entry == "agent" else "offline", "--thinking", "disabled"
    )
    with pytest.raises(ValueError, match="--thinking"):
        make_provider(entry, args)


def test_invalid_mode_is_rejected_before_client_creation(monkeypatch):
    def forbidden(*_args):
        pytest.fail("非法模式不得建立客户端")

    monkeypatch.setattr(provider, "_build_client", forbidden)
    with pytest.raises(ValueError, match="thinking"):
        provider.AnthropicCompatibleProvider(api_key="test-only", thinking="enabled")
