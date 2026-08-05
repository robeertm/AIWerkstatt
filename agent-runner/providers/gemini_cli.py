"""Google Gemini run adapter — drives the `gemini` CLI.

Scaffold: argv + stdin encoding are in place; the exact stdout stream shape is
finalised against the live CLI on first key-connect (that is the only place that
changes — the driver stays untouched). Until then Claude is the proven path.
"""
from __future__ import annotations

import json

from .base import AgentAdapter, RunContext, register


class GeminiAdapter(AgentAdapter):
    id = "gemini"

    def __init__(self):
        self.model = "gemini-2.5-pro"

    def build_argv(self, ctx: RunContext) -> list[str]:
        self.model = ctx.model or self.model
        # Headless, JSON stream, auto-approved edits (YOLO). Pinned at build time.
        return ["gemini", "--model", ctx.model or self.model,
                "--output-format", "stream-json", "--yolo"]

    def encode_user_message(self, text: str) -> bytes:
        return (json.dumps({"type": "user", "content": text},
                           ensure_ascii=False) + "\n").encode("utf-8")

    def compact_message(self) -> bytes | None:
        # `/compress` exists in the interactive CLI; wire it once verified on connect.
        return None

    def default_context_window(self, model: str) -> int:
        return 1_000_000

    def parse_line(self, raw: str) -> list[dict]:
        raw = raw.strip()
        if not raw:
            return []
        try:
            o = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(o, dict):
            return []
        out: list[dict] = []
        if o.get("session_id"):
            out.append({"kind": "session", "id": str(o["session_id"])})
        t = o.get("type")
        if t in ("result", "final", "response"):
            usage = o.get("usage") or o.get("usageMetadata") or {}
            out.append({"kind": "result",
                        "text": o.get("text") or o.get("response") or o.get("result") or "",
                        "error": bool(o.get("error")), "limited": False, "reset": 0,
                        "out_tokens": int(usage.get("candidatesTokenCount",
                                          usage.get("output_tokens", 0)) or 0),
                        "cost": 0.0, "window": None})
        return out


register(GeminiAdapter())
