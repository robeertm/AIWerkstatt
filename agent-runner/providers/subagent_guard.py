#!/usr/bin/env python3
"""Subagent guard — a Claude Code PreToolUse/PostToolUse hook (AIWerkstatt 0.15.0).

Enforced in code, not only in the prompt:
  * at most ``--max`` (default 2) subagents run at the same time per agent run; a further one is denied
    with a clear message so the agent continues serially,
  * depth 1: a subagent may not start its own subagent (Claude Code passes ``agent_id`` then),
  * every subagent gets a model: none given → ``haiku`` (input is rewritten); a model more expensive
    than the run's own model (``--model``) is denied,
  * subagents run synchronously (``run_in_background`` forced to false) so counting and results stay
    inside the run.

State: ``<--state>/state.json`` = {"running": {tool_use_id: {model, description, since}}} under flock.
Any internal error → exit 0 without output (Claude keeps running); only the rules above deny.
"""
import argparse
import fcntl
import json
import os
import sys
import time

RANK = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 4}
STALE_S = 4 * 3600


def family(model):
    m = str(model or "").lower()
    for k in RANK:
        if k in m:
            return k
    return ""


def reply(decision, reason=None, new_input=None):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    if reason:
        out["permissionDecisionReason"] = reason
    if new_input is not None:
        out["updatedInput"] = new_input
    print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


class State:
    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, "state.json")

    def __enter__(self):
        os.makedirs(self.folder, exist_ok=True)
        self.lock = open(os.path.join(self.folder, ".lock"), "a")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            self.d = {}
        run = self.d.get("running") if isinstance(self.d.get("running"), dict) else {}
        now = time.time()
        self.d["running"] = {k: v for k, v in run.items() if now - float(v.get("since", 0)) < STALE_S}
        return self

    def save(self):
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(self.path + ".tmp", self.path)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
        except Exception:
            pass


def pre_agent(d, a):
    ti = dict(d.get("tool_input") or {})
    if d.get("agent_id"):
        reply("deny", "Subagents may not start their own subagents (one level only). Do this part yourself.")
    run_rank = RANK.get(family(a.model), 1)
    wanted = ti.get("model")
    if wanted in (None, ""):
        eff = "haiku"
    elif wanted == "inherit":
        eff = family(a.model) or "haiku"
    else:
        eff = family(wanted)
    if not eff:
        reply("deny", "Unknown subagent model: %s. Allowed: haiku, sonnet, opus." % wanted)
    if RANK[eff] > run_rank:
        reply("deny", "Denied: a subagent may not use a more expensive model than its run (run: %s, requested: %s). "
                      "Use haiku for searching/reading/summarising, sonnet for regular code changes and tests, "
                      "opus only for security, architecture and hard debugging." % (family(a.model) or a.model, eff))
    with State(a.state) as st:
        if len(st.d["running"]) >= a.max:
            reply("deny", "Denied: at most %d subagents at the same time — %d are running. Wait until one has "
                          "finished, then start this part, or do it yourself step by step." % (a.max, len(st.d["running"])))
        st.d["running"][d.get("tool_use_id") or "no-id-%d" % time.time_ns()] = {
            "model": eff, "description": str(ti.get("description") or "")[:120], "since": time.time()}
        st.save()
    ti["model"] = eff
    ti["run_in_background"] = False
    reply("allow", "subagent model: %s" % eff, ti)


def post_agent(d, a):
    with State(a.state) as st:
        st.d["running"].pop(d.get("tool_use_id") or "", None)
        st.save()
    sys.exit(0)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="")
    p.add_argument("--max", type=int, default=2)
    p.add_argument("--state", default="/tmp/aiw-subagent-guard")
    a = p.parse_args()
    try:
        d = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    ev, tool = d.get("hook_event_name"), d.get("tool_name")
    if tool not in ("Agent", "Task"):
        sys.exit(0)
    if ev == "PreToolUse":
        pre_agent(d, a)
    if ev in ("PostToolUse", "PostToolUseFailure"):
        post_agent(d, a)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
