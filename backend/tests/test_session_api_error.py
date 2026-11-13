"""Session driver: API-error retry, runlog truncation, per-run evidence (0.17.0).

Runs agent-runner/session.py against a fake `claude` that answers every input line with a scripted
result (error -> retry -> success, 3 x error -> gave up, 429 limit must NOT use the retry).
Run: cd backend && python3 -m unittest discover -s tests     (~15 s)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "..", "..", "agent-runner", "session.py")
RUNNER = os.path.join(HERE, "..", "..", "agent-runner")


def _load(name, path, package=False):
    """Load a runner module under a private name (the backend has its own `providers` package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[os.path.dirname(path)] if package else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
plan = os.environ["FAKE_PLAN"].split(",")
counter = os.environ["FAKE_COUNTER"]
sid = "11111111-2222-4333-8444-555555555555"
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
for line in sys.stdin:
    if not line.strip():
        continue
    with open(os.environ["FAKE_INPUT"], "a") as f:
        f.write(line)
    try:
        n = int(open(counter).read())
    except (OSError, ValueError):
        n = 0
    open(counter, "w").write(str(n + 1))
    kind = plan[n] if n < len(plan) else "ok"
    if kind == "edge403":
        o = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
             "api_error_status": 403, "result": "Failed to authenticate. API Error: 403 Attention Required"}
    elif kind == "overload":
        o = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 529,
             "result": "Overloaded"}
    elif kind == "limit":
        o = {"type": "result", "subtype": "error_during_execution", "is_error": True, "error": "rate_limit",
             "api_error_status": 429, "rate_limit_info": {"resetsAt": int(time.time()) + 3600}}
    else:
        know = os.environ["FAKE_KNOW"]
        for name, inp in (("Read", {"file_path": know + "/knowledge.md"}),
                          ("Bash", {"command": "cat >> " + know + "/knowledge.md <<'EOF'\nnote\nEOF"}),
                          ("Agent", {"prompt": "x", "description": "d"})):
            print(json.dumps({"type": "assistant", "session_id": sid, "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t" + name, "name": name, "input": inp}]}}), flush=True)
        o = {"type": "result", "subtype": "success", "is_error": False, "result": "Done."}
    o["session_id"] = sid
    print(json.dumps(o), flush=True)
'''


class SessionApiError(unittest.TestCase):
    def run_session(self, plan, delays="1,1,1", limit=60):
        t = tempfile.mkdtemp(prefix="aiw-sess-")
        self.addCleanup(shutil.rmtree, t, True)
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        for d in ("events", "inbox", "usage", "wt"):
            os.makedirs(os.path.join(t, d))
        first = os.path.join(t, "first.txt")
        with open(first, "w") as f:
            f.write("Do the task.")
        know = os.path.join(t, "vault", "demo")
        os.makedirs(know)
        env = dict(os.environ)
        env.update({
            "PATH": bindir + os.pathsep + env.get("PATH", ""), "WK_SLUG": "demo", "WK_THREAD_ID": "7",
            "WK_REPO": "local/demo", "WK_WT": os.path.join(t, "wt"), "WK_MODEL": "claude-haiku-5-5",
            "WK_FIRST_MSG_FILE": first, "WK_FIRST_TASK": "100", "WK_EVENTS": os.path.join(t, "events"),
            "WK_INBOX": os.path.join(t, "inbox"), "WK_RUNLOG": os.path.join(t, "run.log"),
            "WK_USAGE_DIR": os.path.join(t, "usage"), "WK_IDLE_TIMEOUT": "1", "WK_STALL_TIMEOUT": "60",
            "WK_STREAM_RETRY_MAX": "0", "AIW_API_RETRY_DELAYS": delays, "AIW_KNOWLEDGE_DIR": know,
            "FAKE_PLAN": ",".join(plan), "FAKE_COUNTER": os.path.join(t, "counter"),
            "FAKE_INPUT": os.path.join(t, "input.jsonl"), "FAKE_KNOW": know,
        })
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "AIW_MEMORY_DIR"):
            env.pop(k, None)
        seen = []
        status = os.path.join(t, "events", "status-7.json")
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    with open(status) as f:
                        seen.append(json.load(f))
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)

        th = threading.Thread(target=watch, daemon=True)
        th.start()
        try:
            r = subprocess.run([sys.executable, SESSION], env=env, capture_output=True, text=True, timeout=limit)
        finally:
            stop.set()
            th.join(timeout=2)
        events = []
        for n in sorted(os.listdir(os.path.join(t, "events"))):
            if n.endswith(".json") and not n.startswith(("status", "limit")):
                with open(os.path.join(t, "events", n)) as f:
                    events.append(json.load(f))
        result = json.loads(r.stdout.strip().splitlines()[-1])
        sent = []
        if os.path.exists(env["FAKE_INPUT"]):
            sent = [json.loads(x) for x in open(env["FAKE_INPUT"])]
        live = []
        lf = os.path.join(t, "events", "live-7.jsonl")
        if os.path.exists(lf):
            live = [json.loads(x) for x in open(lf)]
        return result, events, seen, sent, live

    def test_error_then_retry_then_success(self):
        res, evs, seen, sent, _live = self.run_session(["edge403", "ok"])
        pairs = [(e["type"], e["text"]) for e in evs]
        self.assertTrue(any(ty == "progress" and "API error (403" in tx and "retrying at" in tx and "(1/3)" in tx
                            for ty, tx in pairs), pairs)
        self.assertIn(("reply", "Done."), pairs)
        self.assertFalse(any(ty == "failed" for ty, _ in pairs), pairs)
        waiting = [s["api_error"] for s in seen if s.get("api_error")]
        self.assertTrue(waiting and waiting[0]["attempt"] == 1 and waiting[0]["max"] == 3
                        and waiting[0]["next_attempt"] > 0 and not waiting[0]["gave_up"]
                        and "403" in waiting[0]["reason"], seen[-3:])
        self.assertTrue(res["ok"])
        self.assertFalse(res["api_gave_up"])
        self.assertIsNone(res["api_error"])                       # a successful answer resets the state
        self.assertEqual(len(sent), 2)                            # order + one "continue" in the SAME process
        self.assertIn("Continue exactly where you left off", json.dumps(sent[1]))

    def test_three_errors_then_gave_up(self):
        res, evs, _seen, sent, _live = self.run_session(["overload"] * 6)
        pairs = [(e["type"], e["text"]) for e in evs]
        self.assertEqual(sum(1 for ty, tx in pairs if ty == "progress" and "retrying at" in tx), 3, pairs)
        self.assertTrue(any(ty == "failed" and "gave up after 3 retries" in tx for ty, tx in pairs), pairs)
        self.assertTrue(res["api_gave_up"])
        self.assertFalse(res["ok"])
        self.assertTrue(res["api_error"]["gave_up"])
        self.assertEqual(res["requeue"], [])
        self.assertEqual(len(sent), 4)                            # order + 3 retries, no fourth

    def test_real_limit_does_not_use_the_retry(self):
        res, evs, _seen, sent, _live = self.run_session(["limit"])
        pairs = [(e["type"], e["text"]) for e in evs]
        self.assertFalse(any("API error" in tx for _ty, tx in pairs), pairs)
        self.assertTrue(any(ty == "limited" for ty, _ in pairs), pairs)
        self.assertTrue(res["limited"])
        self.assertEqual(len(sent), 1)

    def test_empty_setting_keeps_the_old_behaviour(self):
        res, evs, _seen, sent, _live = self.run_session(["edge403"], delays="")
        pairs = [(e["type"], e["text"]) for e in evs]
        self.assertFalse(any("retrying at" in tx for _ty, tx in pairs), pairs)

    def test_evidence_line_and_payload(self):
        res, _evs, seen, _sent, live = self.run_session(["ok"])
        self.assertEqual(res["evidence"], {"knowledge_read": True, "knowledge_updated": True, "subagents": 1})
        lines = [r["text"] for r in live if r["text"].startswith("Evidence:")]
        self.assertEqual(lines, ["Evidence: knowledge read ✓ · knowledge updated ✓ · subagents 1"])
        self.assertEqual(seen[-1]["evidence"]["subagents"], 1)


class EvidenceCounting(unittest.TestCase):
    def setUp(self):
        Evidence = _load("aiw_runner_evidence", os.path.join(RUNNER, "evidence.py")).Evidence
        self.ev = Evidence(["/vault/demo"])

    def test_reads_count_but_writes_and_noise_do_not(self):
        for cmd in ("cat >> /vault/demo/knowledge.md <<'EOF'\nnote\nEOF",
                    "echo hi > /tmp/x <<'EOF'\nsee /vault/demo/knowledge.md\nEOF",
                    "curl -s https://example.invalid/?p=/vault/demo",
                    "git commit -m 'update /vault/demo/knowledge.md notes'"):
            self.ev.tool("Bash", {"command": cmd})
        self.assertFalse(self.ev.read)
        self.assertTrue(self.ev.updated)          # only the first command wrote there
        self.ev.tool("Bash", {"command": "grep -n Pitfalls /vault/demo/knowledge.md"})
        self.assertTrue(self.ev.read)

    def test_tool_calls_and_subagents(self):
        self.ev.tool("Glob", {"pattern": "/vault/demo/*.md"})
        self.ev.tool("Edit", {"file_path": "/vault/demo/knowledge.md"})
        self.ev.tool("Task", {})
        self.ev.tool("Read", {"file_path": "/workspace/app.py"})
        self.assertEqual(self.ev.as_dict(), {"knowledge_read": True, "knowledge_updated": True, "subagents": 1})


class ApiErrorClassification(unittest.TestCase):
    def parse(self, **o):
        get_adapter = _load("aiw_runner_providers", os.path.join(RUNNER, "providers", "__init__.py"),
                            package=True).get_adapter
        o = dict({"type": "result", "is_error": True}, **o)
        return [e for e in get_adapter("claude").parse_line(json.dumps(o)) if e["kind"] == "result"][0]

    def test_classification(self):
        self.assertTrue(self.parse(terminal_reason="api_error")["api_error"])
        for st in (401, 403, 408, 409, 529, 500, 503):
            self.assertTrue(self.parse(api_error_status=st)["api_error"], st)
        for text in ("Failed to authenticate", "Overloaded", "API Error: x", "Attention Required | edge"):
            self.assertTrue(self.parse(result=text)["api_error"], text)
        self.assertFalse(self.parse(api_error_status=429, terminal_reason="api_error")["api_error"])
        self.assertFalse(self.parse(error="rate_limit")["api_error"])
        self.assertFalse(self.parse(result="tests failed")["api_error"])
        self.assertFalse(self.parse(is_error=False, result="overloaded servers are a topic")["api_error"])


if __name__ == "__main__":
    unittest.main()
