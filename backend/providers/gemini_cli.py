"""Google Gemini — via the Gemini CLI (`gemini`).

Auth: a Google AI Studio API key (`GEMINI_API_KEY`) OR a Google sign-in (OAuth).
The run adapter lives in ``agent-runner/providers/gemini_cli.py``.
"""
from .base import ModelChoice, ProviderSpec, register

register(ProviderSpec(
    id="gemini",
    label="Google Gemini",
    cli="gemini",
    models=(
        ModelChoice("gemini-2.5-pro", "Gemini 2.5 Pro (most capable)", 1_000_000),
        ModelChoice("gemini-2.5-flash", "Gemini 2.5 Flash (fast & cheap)", 1_000_000),
    ),
    default_model="gemini-2.5-pro",
    efforts=(),
    auth_modes=("api_key", "oauth"),
    key_env="GEMINI_API_KEY",
    key_prefix="AIza",
    key_help_url="https://aistudio.google.com/apikey",
    key_help="Create an API key at aistudio.google.com, or sign in with Google.",
))
