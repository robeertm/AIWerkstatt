"""Demo provider — zero configuration, no API key, no network.

Turns each request into a small real web page in the workspace so a fresh
`docker compose up` produces a working live app immediately. Great for trying
AIWerkstatt out; connect a real provider for full apps.
"""
from .base import ModelChoice, ProviderSpec, register

register(ProviderSpec(
    id="demo",
    label="Demo (no key needed)",
    cli="",
    models=(ModelChoice("demo", "Demo builder", 200_000),),
    default_model="demo",
    efforts=(),
    auth_modes=(),          # no credentials required — always ready
    key_env="",
    key_help_url="",
    key_help="No key needed — builds a small demo page so you can see the flow. "
             "Connect Claude, OpenAI or Gemini for real apps.",
))
