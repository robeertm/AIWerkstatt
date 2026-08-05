"""OpenAI Codex run adapter — drives the `codex` CLI.

Scaffold: argv + stdin encoding are in place; the exact stdout stream shape is
finalised against the live CLI on first key-connect (that is the only place that
changes — the driver stays untouched). Until then Claude is the proven path.
"""
from __future__ import annotations

import json

from .base import AgentAdapter, RunContext, register

_VALID_EFFORT = {"low", "medium", "high"}


class CodexAdapter(AgentAdapter):
    id = "codex"

    def __init__(self):
        self.model = "gpt-5.1-codex"

    def build_argv(self, ctx: RunContext) -> list[str]:
        self.model = ctx.model or self.model
        # Headless, JSON-streamed, auto-approved edits. Flags are pinned against
        # the installed codex version at build time.
        argv = ["codex", "exec", "--json", "--model", ctx.model or self.model,
                "--dangerously-bypass-approvals-and-sandbox"]
        if (ctx.effort or "").lower() in _VALID_EFFORT:
            argv += ["--config", f"model_reasoning_effort={ctx.effort.lower()}"]
        return argv

    def encode_user_message(self, text: str) -> bytes:
        return (json.dumps({"type": "user_message", "text": text},
                           ensure_ascii=False) + "\n").encode("utf-8")

    def compact_message(self) -> bytes | None:
        # Codex CLI manages its own context; no explicit compaction hook yet.
        return None

    def default_context_window(self, model: str) -> int:
        return 400_000

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
        sid = o.get("session_id") or o.get("thread_id")
        if sid:
            out.append({"kind": "session", "id": str(sid)})
        t = o.get("type") or o.get("msg", {}).get("type")
        # Final assistant turn → result. Shape confirmed against live CLI on connect.
        if t in ("turn_complete", "task_complete", "result", "final"):
            usage = o.get("usage") or {}
            out.append({"kind": "result",
                        "text": o.get("text") or o.get("message") or o.get("result") or "",
                        "error": bool(o.get("error")), "limited": False, "reset": 0,
                        "out_tokens": int(usage.get("output_tokens", 0) or 0),
                        "cost": float(o.get("cost_usd", 0) or 0), "window": None})
        return out


register(CodexAdapter())
