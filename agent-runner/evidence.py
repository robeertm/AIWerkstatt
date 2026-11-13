"""Per-run evidence: did the agent really read and update the knowledge base, how many subagents did it start?

Measured from the real tool calls in the stream (not from what the agent says about itself):
  * knowledge READ    - Read/Grep/Glob on a path inside the knowledge folder, or a Bash read command that
                        names it (heredoc/redirect writes, curl and `git commit` messages do not count),
  * knowledge UPDATED - Write/Edit on a path inside it, or a Bash write (redirect, tee, cp, mv, ...) to it,
  * subagents         - Agent/Task calls.
Only counters and paths of the configured folder, never file contents.
"""
from __future__ import annotations

import re

_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_READ_TOOLS = ("Read", "Grep", "Glob")
_READ_CMD = re.compile(r"\b(cat|sed|head|tail|grep|rg|less|more|ls|find|awk|wc|jq|diff|stat|file|tree|python3?)\b")
_HEREDOC = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?[^\n]*\n.*?\n[ \t]*\1[ \t]*(?:\n|$)", re.S)
_IGNORE = re.compile(r"\bcurl\b|\bwget\b|\bgit\s+commit\b|api\.github\.com")


def _write_cmd(cmd: str, path: str) -> bool:
    """Does the command write to ``path`` (redirect, tee, cp/mv/install/sed -i with it as target)?"""
    p = re.escape(path)
    return bool(re.search(r">>?\s*[\"']?[^\s\"'|;&]*" + p, cmd)
                or re.search(r"\btee\b[^|;&\n]*" + p, cmd)
                or re.search(r"\b(cp|mv|install|rsync|ln|touch|truncate)\b[^|;&\n]*" + p, cmd)
                or re.search(r"\bsed\b[^|;&\n]*\s-i\b[^|;&\n]*" + p, cmd))


class Evidence:
    def __init__(self, knowledge_dirs):
        self.dirs = [d.rstrip("/") for d in knowledge_dirs if d and d.strip("/")]
        self.read = False
        self.updated = False
        self.subagents = 0

    def _inside(self, text: str) -> bool:
        return any(d in text for d in self.dirs)

    def tool(self, name: str, inp: dict) -> None:
        inp = inp if isinstance(inp, dict) else {}
        if name in ("Agent", "Task"):
            self.subagents += 1
            return
        if not self.dirs:
            return
        if name in _WRITE_TOOLS:
            if self._inside(str(inp.get("file_path") or inp.get("notebook_path") or "")):
                self.updated = True
        elif name in _READ_TOOLS:
            if self._inside(" ".join(str(inp.get(k) or "") for k in ("file_path", "path", "pattern", "glob"))):
                self.read = True
        elif name == "Bash":
            cmd = _HEREDOC.sub(" ", str(inp.get("command") or ""))   # text inside a heredoc is not a command
            if not self._inside(cmd) or _IGNORE.search(cmd):
                return
            if any(_write_cmd(cmd, d) for d in self.dirs if d in cmd):
                self.updated = True
            elif _READ_CMD.search(cmd):
                self.read = True

    def as_dict(self) -> dict:
        return {"knowledge_read": self.read, "knowledge_updated": self.updated, "subagents": self.subagents}

    def line(self) -> str:
        mark = lambda b: "✓" if b else "✗"   # noqa: E731
        return "Evidence: knowledge read %s · knowledge updated %s · subagents %d" % (
            mark(self.read), mark(self.updated), self.subagents)
