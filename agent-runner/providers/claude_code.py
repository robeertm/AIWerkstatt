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
_MAX_ACT = 300           # compact one-liner (default live view)
# Full, unclipped text per entry (detail / full-screen view): every line of a tool
# result, the whole thought, the whole multi-line command. Capped only against a
# pathological mega-dump (20 000 chars ≈ several hundred lines of output).
_FULL_MAX = 20000


def _clip(s: str) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= _MAX_ACT else s[:_MAX_ACT] + " …"


def _tool_line(name: str, inp: dict) -> str:
    """A tool call as one human-readable line (matches the live-ticker style)."""
    if name == "Bash":
        cmd = str(inp.get("command", "")).strip().split("\n")[0]
        return "$ " + cmd
    for key in ("file_path", "path", "pattern", "url", "query", "prompt"):
        if inp.get(key):
            return "%s: %s" % (name, inp[key])
    return name


def _result_line(content) -> str:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    text = str(content or "")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return "(empty)"
    return lines[0] if len(lines) == 1 else "%s   … (%d lines)" % (lines[0], len(lines))


def _tool_full(name: str, inp: dict) -> str:
    """The WHOLE tool call — multi-line bash commands in full, otherwise the full
    input parameters as readable JSON."""
    if name == "Bash":
        return "$ " + str(inp.get("command", "")).strip()
    if inp:
        return name + "\n" + json.dumps(inp, ensure_ascii=False, indent=2)
    return name


def _result_full(content) -> str:
    """The WHOLE tool result, newlines preserved (not collapsed to the first line)."""
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def _act(act: str, compact: str, full: str | None = None) -> dict:
    """One live-activity event: always the compact one-liner, PLUS the full text when
    it genuinely adds detail (multi-line / longer). Full is capped only against a
    pathological dump — otherwise every last bit is kept, newlines and all."""
    d = {"kind": "activity", "act": act, "text": compact}
    full = (full or "").rstrip()
    if full:
        if len(full) > _FULL_MAX:
            full = full[:_FULL_MAX] + " …"
        if " ".join(full.split()) != compact:
            d["full"] = full
    return d


# Behaviour rule injected on every turn (--append-system-prompt). Core: real parallelism goes ONLY
# through the Agent tool (subagents run synchronously within the same run and return their results) —
# never through detached background jobs, whose output is lost when the run ends.
_AGENT_RULES = (
    "You run as a single non-interactive pass. For parallelism, use the Agent tool: subagents run "
    "SYNCHRONOUSLY within THIS run and return their results, so you can reassemble them. NEVER use "
    "background processes (nohup, &, disown, daemons or self-invoked CLI calls) — their output is lost "
    "the moment your run ends. Do EVERYTHING within this run: wait for subagents, reassemble, verify "
    "and commit BEFORE you reply. Never announce that you will report back later — after your reply "
    "the run ends, there is no later."
)


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
        # Parallel subagents: ensure the Agent tool is available (Claude Code spawns subagents that
        # run SYNCHRONOUSLY within this same run). Scoped to the Claude adapter — other providers keep
        # their own tool set untouched.
        tools = ctx.allowed_tools or "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,TodoWrite"
        if "Agent" not in [x.strip() for x in tools.split(",")]:
            tools += ",Agent"
        argv += ["--allowedTools", tools,
                 "--append-system-prompt", _AGENT_RULES]
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
            msg = o.get("message") or {}
            # Only the MAIN conversation drives the context bar. Subagent messages carry a
            # parent_tool_use_id (≠ null) and have their own context — counting them would skew
            # the parent agent's ctx%.
            if not o.get("parent_tool_use_id"):
                c = self._ctx_tokens(msg.get("usage"))
                if c:
                    out.append({"kind": "ctx", "tokens": c, "window": None})
            # live activity: what the agent is saying / thinking / doing
            content = msg.get("content")
            if isinstance(content, list):
                for bl in content:
                    if not isinstance(bl, dict):
                        continue
                    bt = bl.get("type")
                    if bt == "text" and bl.get("text", "").strip():
                        out.append(_act("say", _clip(bl["text"]), bl["text"]))
                    elif bt == "thinking":
                        txt = bl.get("thinking") or bl.get("text") or ""
                        if txt.strip():
                            out.append(_act("think", _clip(txt), txt))
                    elif bt == "tool_use":
                        name, inp = bl.get("name", "?"), bl.get("input") or {}
                        out.append(_act("tool", _clip(_tool_line(name, inp)), _tool_full(name, inp)))
        elif t == "user":
            content = (o.get("message") or {}).get("content")
            if isinstance(content, list):
                for bl in content:
                    if isinstance(bl, dict) and bl.get("type") == "tool_result":
                        cont = bl.get("content")
                        out.append(_act("result", _clip(_result_line(cont)), _result_full(cont)))
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
            # Only a REAL usage limit pauses the whole workshop — NOT every API error.
            # `terminal_reason == "api_error"` also covers transient failures (server_error,
            # "Connection closed mid-response"); those must fall through to the normal
            # failed/retry path, otherwise a single network hiccup triggers a bogus
            # "usage limit reached" pause for every project. Real limits carry
            # error == "rate_limit" / api_error_status == 429 (plus the rate_limit_event
            # with status == "rejected", handled separately above).
            limited = (o.get("error") == "rate_limit" or o.get("api_error_status") == 429)
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
