"""Provider registry + metadata (control-plane side).

A ProviderSpec describes an AI coding-agent backend for the UI: which models it
offers, how you authenticate (API key and/or OAuth), and where to get a key.
The actual *run* adapters (that spawn the CLI and parse its stream) live in
``agent-runner/providers`` — this side only needs metadata + a cheap key check.

Adding a provider = drop a module in this package that calls ``register(...)``
and a matching run-adapter in ``agent-runner/providers``. Nothing else changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelChoice:
    id: str
    label: str
    context_window: int = 200_000


@dataclass(frozen=True)
class ProviderSpec:
    id: str                                  # "claude" | "codex" | "gemini"
    label: str                               # human name for the UI
    cli: str                                 # binary the agent-runner invokes
    models: tuple[ModelChoice, ...]
    default_model: str
    efforts: tuple[str, ...] = ()            # reasoning-effort steps, if any
    auth_modes: tuple[str, ...] = ("api_key",)   # "api_key" and/or "oauth"
    key_env: str = ""                        # env var the runner expects for the key
    key_prefix: str = ""                     # cheap format check (empty = skip)
    key_help_url: str = ""                   # "where do I get a key" link
    key_help: str = ""                       # one-line hint shown in the UI

    def validate_key(self, key: str) -> bool:
        """Cheap format check only — real validation happens on first run."""
        key = (key or "").strip()
        if len(key) < 20:
            return False
        return key.startswith(self.key_prefix) if self.key_prefix else True

    def public(self) -> dict:
        """JSON-safe metadata for the UI (never includes secrets)."""
        return {
            "id": self.id,
            "label": self.label,
            "models": [{"id": m.id, "label": m.label, "context_window": m.context_window}
                       for m in self.models],
            "default_model": self.default_model,
            "efforts": list(self.efforts),
            "auth_modes": list(self.auth_modes),
            "key_help_url": self.key_help_url,
            "key_help": self.key_help,
        }


_REGISTRY: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    _REGISTRY[spec.id] = spec
    return spec


def all_specs() -> list[ProviderSpec]:
    return list(_REGISTRY.values())


def get_spec(pid: str) -> ProviderSpec | None:
    return _REGISTRY.get(pid)
