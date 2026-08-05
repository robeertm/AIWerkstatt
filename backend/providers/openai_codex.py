"""OpenAI — via the Codex CLI (`codex`).

Auth: an OpenAI API key (`OPENAI_API_KEY`) OR a ChatGPT sign-in (OAuth). The run
adapter lives in ``agent-runner/providers/openai_codex.py``. Model list is
easily edited here as OpenAI ships new coding models.
"""
from .base import ModelChoice, ProviderSpec, register

register(ProviderSpec(
    id="codex",
    label="OpenAI (Codex)",
    cli="codex",
    models=(
        ModelChoice("gpt-5.1-codex", "GPT-5.1 Codex (coding)", 400_000),
        ModelChoice("gpt-5.1", "GPT-5.1 (general)", 400_000),
        ModelChoice("o4-mini", "o4-mini (fast reasoning)", 200_000),
    ),
    default_model="gpt-5.1-codex",
    efforts=("low", "medium", "high"),
    auth_modes=("api_key", "oauth"),
    key_env="OPENAI_API_KEY",
    key_prefix="sk-",
    key_help_url="https://platform.openai.com/api-keys",
    key_help="Create an API key at platform.openai.com, or sign in with ChatGPT.",
))
