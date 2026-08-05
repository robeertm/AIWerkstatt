"""Claude run adapter — drives the Claude Code CLI (`claude`) in stream-json mode.

Ported 1:1 from the proven production session driver: same argv, same stdin
encoding, same stream-json parsing (session_id / assistant usage / rate-limit /
result). Auth is inherited from the environment (ANTHROPIC_API_KEY or
CLAUDE_CODE_OAUTH_TOKEN), sourced by the entrypoint — never on the command line.
"""
from __future__ import annotations

import json

from .base import AgentAdapter, RunContext, register

_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}


class ClaudeAdapter(AgentAdapter):
    id = "claude"

    def __init__(self):
        self.model = "claude-opus-4-8"

    def build_argv(self, ctx: RunContext) -> list[str]:
        self.model = ctx.model or self.model
        argv = ["claude", "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose"]
        if ctx.resume_id:
            argv += ["--resume", ctx.resume_id]
        if (ctx.effort or "").lower() in _VALID_EFFORT:
            argv += ["--effort", ctx.effort.lower()]
        argv += ["--model", ctx.model or self.model,
                 "--permission-mode", "acceptEdits"]
        if ctx.allowed_tools:
            argv += ["--allowedTools", ctx.allowed_tools]
        return argv

    def encode_user_message(self, text: str) -> bytes:
        line = json.dumps({"type": "user",
                           "message": {"role": "user", "content": text}},
                          ensure_ascii=False) + "\n"
        return line.encode("utf-8")

    def compact_message(self) -> bytes | None:
        return self.encode_user_message("/compact")

    def default_context_window(self, model: str) -> int:
        return 1_000_000 if "opus" in (model or "") else 200_000

    def _ctx_tokens(self, usage) -> int | None:
        if not isinstance(usage, dict):
            return None
        try:
            return (int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0))
        except (TypeError, ValueError):
            return None

    def _window(self, o) -> int | None:
        mu = o.get("modelUsage") or {}
        entry = mu.get(self.model) or (next(iter(mu.values()), None) if mu else None)
        try:
            cw = int((entry or {}).get("contextWindow", 0) or 0)
            return cw if cw > 0 else None
        except (TypeError, ValueError):
            return None

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
            out.append({"kind": "session", "id": o["session_id"]})
        t = o.get("type")
        if t == "assistant":
            c = self._ctx_tokens((o.get("message") or {}).get("usage"))
            if c:
                out.append({"kind": "ctx", "tokens": c, "window": None})
        elif t == "rate_limit_event":
            ri = o.get("rate_limit_info") or {}
            if ri.get("status") == "rejected":
                reset = 0
                try:
                    reset = int(ri.get("resetsAt") or 0)
                except (TypeError, ValueError):
                    reset = 0
                out.append({"kind": "limit", "reset": reset})
        elif t == "result":
            is_err = bool(o.get("is_error")) or str(o.get("subtype", "")).startswith("error")
            limited = (o.get("error") == "rate_limit" or o.get("api_error_status") == 429
                       or o.get("terminal_reason") == "api_error")
            reset = 0
            ri = o.get("rate_limit_info") or {}
            try:
                reset = int(ri.get("resetsAt") or 0)
            except (TypeError, ValueError):
                reset = 0
            usage = o.get("usage") or {}
            try:
                out_tokens = int(usage.get("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                out_tokens = 0
            try:
                cost = float(o.get("total_cost_usd", 0) or 0)
            except (TypeError, ValueError):
                cost = 0.0
            out.append({"kind": "result", "text": o.get("result") or "",
                        "error": is_err, "limited": limited, "reset": reset,
                        "out_tokens": out_tokens, "cost": cost,
                        "window": self._window(o)})
        return out


register(ClaudeAdapter())
