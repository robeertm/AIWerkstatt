"""Anthropic Claude — via the Claude Code CLI (`claude`).

Auth: an Anthropic API key (`ANTHROPIC_API_KEY`) OR a Claude subscription token
(`CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`). The run adapter lives in
``agent-runner/providers/claude_code.py``.
"""
from .base import ModelChoice, ProviderSpec, register

register(ProviderSpec(
    id="claude",
    label="Anthropic Claude",
    cli="claude",
    models=(
        ModelChoice("auto", "🤖 Auto (AI picks model + intensity)", 1_000_000),
        ModelChoice("claude-opus-5-5", "Claude Opus 5.5 (most capable)", 1_000_000),
        ModelChoice("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced)", 200_000),
        ModelChoice("claude-haiku-5-5", "Claude Haiku 5.5 (fast & cheap)", 200_000),
        ModelChoice("claude-fable-5-1", "Claude Fable 5.1", 200_000, in_auto=False),
        # Older IDs stay valid so existing project settings keep working.
        ModelChoice("claude-opus-4-8", "Claude Opus 4.8 (default)", 1_000_000, in_auto=False),
        ModelChoice("claude-sonnet-5", "Claude Sonnet 5 (older)", 200_000, in_auto=False),
        ModelChoice("claude-haiku-4-5", "Claude Haiku 4.5 (older)", 200_000, in_auto=False),
    ),
    default_model="claude-opus-4-8",
    efforts=("low", "medium", "high", "xhigh", "max"),
    auth_modes=("api_key", "oauth"),
    key_env="ANTHROPIC_API_KEY",
    key_prefix="sk-ant-",
    key_help_url="https://console.anthropic.com/settings/keys",
    key_help="Create an API key at console.anthropic.com, or use a Claude "
             "subscription via 'claude setup-token'.",
))
