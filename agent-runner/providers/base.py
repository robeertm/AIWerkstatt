"""Run-adapter interface (agent-runner side).

The session driver (``session.py``) is provider-neutral: it owns the long-lived
process, the ordered turn queue, auto-compaction, usage tracking, limit handling
and the follow-up inbox. Everything provider-specific lives behind ``AgentAdapter``:

  * how to build the CLI argv (models, effort, resume, tools, stream flags),
  * how to encode a user message onto stdin,
  * how to turn one raw stdout line into NORMALISED events the driver understands,
  * how (if at all) to trigger context compaction.

Normalised events returned by ``parse_line`` are plain dicts with a ``kind``:

  {"kind": "session", "id": "<session-id>"}
  {"kind": "ctx", "tokens": <int current context size>, "window": <int|None>}
  {"kind": "limit", "reset": <epoch int>}                # provider refused: rate limit
  {"kind": "result", "text": str, "error": bool,
                     "limited": bool, "reset": <epoch int>,
                     "out_tokens": int, "cost": float, "window": <int|None>}

Exactly one ``result`` is expected per user message fed in (the driver relies on
this for turn↔task mapping). If a provider folds a mid-turn follow-up into the
same turn, the driver resolves the folded tasks together — same as production.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunContext:
    model: str
    effort: str = ""
    resume_id: str = ""
    allowed_tools: str = ""


class AgentAdapter:
    id: str = "base"

    def build_argv(self, ctx: RunContext) -> list[str]:
        raise NotImplementedError

    def encode_user_message(self, text: str) -> bytes:
        raise NotImplementedError

    def parse_line(self, raw: str) -> list[dict]:
        raise NotImplementedError

    def compact_message(self) -> bytes | None:
        """Encoded line that triggers compaction, or None if unsupported."""
        return None

    def default_context_window(self, model: str) -> int:
        return 200_000


_REGISTRY: dict[str, AgentAdapter] = {}


def register(adapter: AgentAdapter) -> AgentAdapter:
    _REGISTRY[adapter.id] = adapter
    return adapter


def get_adapter(pid: str) -> AgentAdapter | None:
    return _REGISTRY.get(pid)
