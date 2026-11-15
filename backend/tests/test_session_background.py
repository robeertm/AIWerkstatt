"""Session driver: a turn that ends with open background work is not a finished task (0.23.0).

Runs agent-runner/session.py against a fake `claude` that emits the real stream events
(`background_tasks_changed`, results) per scripted step.
Run: cd backend && python3 -m unittest tests.test_session_background     (~30 s)
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

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time
plan = os.environ["FAKE_PLAN"].split(",")
counter = os.environ["FAKE_COUNTER"]
sid = "11111111-2222-4333-8444-555555555555"
TASK = {"task_id": "b7t4ieoiq", "task_type": "local_bash", "description": "long probe run", "status": "running"}


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


def tasks(lst):
    out({"type": "system", "subtype": "background_tasks_changed", "tasks": lst})


def result(text):
    out({"type": "result", "subtype": "success", "is_error": False, "result": text})


out({"type": "system", "subtype": "init"})
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
    if kind == "bg_wake":            # background task open, the CLI wakes the agent itself after a second
        tasks([TASK])
        result("I will wait for the monitor notification.")
        time.sleep(1.5)
        tasks([])
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "back"}]}})
        result("Finished after the notification.")
    elif kind == "bg_silent":        # task ends, but the CLI never wakes the agent
        tasks([TASK])
        result("Waiting for the background run.")
        time.sleep(0.3)
        tasks([])
    elif kind == "bg_open":          # task stays open for a long time
        tasks([TASK])
        result("Started the long run.")
    elif kind == "close":            # the long run is done and the agent finishes
        tasks([])
        result("Done.")
    elif kind == "text":             # no state event, the reply only announces a wait
        result("Ich warte auf die Benachrichtigung des Monitors, melde mich, sobald er fertig ist.")
    elif kind == "long_text":        # long report that happens to contain the phrase: finished
        result(("Report line. " * 120) + " I'll wait for nothing more.")
    else:
        result("Done.")
'''


class SessionBackground(unittest.TestCase):
    def run_session(self, plan, env_extra=None, limit=90):
        t = tempfile.mkdtemp(prefix="aiw-bg-")
        self.addCleanup(shutil.rmtree, t, True)
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        for d in ("events", "inbox", "usage", "wt", "vault/demo"):
            os.makedirs(os.path.join(t, d))
        first = os.path.join(t, "first.txt")
        with open(first, "w") as f:
            f.write("Do the task.")
        env = dict(os.environ)
        env.update({
            "PATH": bindir + os.pathsep + env.get("PATH", ""), "WK_SLUG": "demo", "WK_THREAD_ID": "7",
            "WK_REPO": "local/demo", "WK_WT": os.path.join(t, "wt"), "WK_MODEL": "claude-haiku-5-5",
            "WK_FIRST_MSG_FILE": first, "WK_FIRST_TASK": "100", "WK_EVENTS": os.path.join(t, "events"),
            "WK_INBOX": os.path.join(t, "inbox"), "WK_RUNLOG": os.path.join(t, "run.log"),
            "WK_USAGE_DIR": os.path.join(t, "usage"), "WK_IDLE_TIMEOUT": "1", "WK_STALL_TIMEOUT": "3",
            "WK_STREAM_RETRY_MAX": "0", "AIW_API_RETRY_DELAYS": "", "AIW_KNOWLEDGE_DIR": os.path.join(t, "vault/demo"),
            "AIW_BG_EMPTY_S": "1", "AIW_BG_NUDGE_S": "900", "AIW_BG_MAX_CONTINUATIONS": "3",
            "FAKE_PLAN": ",".join(plan), "FAKE_COUNTER": os.path.join(t, "counter"),
            "FAKE_INPUT": os.path.join(t, "input.jsonl"),
        })
        env.update(env_extra or {})
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
        pairs = [(e["type"], e["text"]) for e in events]
        return result, pairs, seen, sent

    def replies(self, pairs):
        return [tx for ty, tx in pairs if ty == "reply"]

    def test_normal_reply_is_unchanged(self):
        res, pairs, seen, sent = self.run_session(["ok"])
        self.assertEqual(self.replies(pairs), ["Done."])
        self.assertFalse(any("Waiting for a background run" in tx for _ty, tx in pairs), pairs)
        self.assertEqual(len(sent), 1)
        self.assertEqual(res["continuations"], 0)
        self.assertIsNone(res["stuck"])
        self.assertFalse(any("background_wait" in s for s in seen))

    def test_open_task_no_reply_until_the_cli_wakes_itself(self):
        res, pairs, seen, sent = self.run_session(["bg_wake"])
        self.assertEqual(self.replies(pairs), ["Finished after the notification."], pairs)
        prog = [tx for ty, tx in pairs if ty == "progress" and "Waiting for a background run" in tx]
        self.assertEqual(len(prog), 1, pairs)
        self.assertIn("long probe run", prog[0])
        self.assertIn("I will wait for the monitor notification.", prog[0])
        self.assertEqual(len(sent), 1)                            # no continuation needed
        waits = [s for s in seen if s.get("background_wait")]
        self.assertTrue(waits and waits[0]["background_wait"]["reason"] == "background"
                        and waits[0]["background_wait"]["max"] == 3
                        and waits[0]["background_wait"]["description"] == "long probe run"
                        and waits[0]["background_open"] == 1, seen[-3:])
        self.assertNotIn("background_wait", seen[-1])
        self.assertEqual(res["continuations"], 0)
        self.assertEqual(res["requeue"], [])

    def test_cli_does_not_wake_one_continuation_then_reply(self):
        res, pairs, seen, sent = self.run_session(["bg_silent", "ok"])
        self.assertEqual(self.replies(pairs), ["Done."], pairs)
        self.assertTrue(any(ty == "progress" and "Automatic continuation 1/3" in tx for ty, tx in pairs), pairs)
        self.assertEqual(len(sent), 2)
        self.assertIn("Automatic continuation by the workshop", json.dumps(sent[1]))
        self.assertEqual(res["continuations"], 1)
        self.assertIsNone(res["stuck"])

    def test_long_run_gets_a_continuation_after_the_nudge_time(self):
        res, pairs, seen, sent = self.run_session(
            ["bg_open", "close"], {"AIW_BG_NUDGE_S": "2", "AIW_BG_EMPTY_S": "600"})
        self.assertEqual(self.replies(pairs), ["Done."], pairs)
        self.assertTrue(any(ty == "progress" and "running for" in tx for ty, tx in pairs), pairs)
        self.assertEqual(len(sent), 2)
        self.assertEqual(res["continuations"], 1)

    def test_text_pattern_fallback_without_state_event(self):
        res, pairs, seen, sent = self.run_session(["text", "ok"])
        self.assertEqual(self.replies(pairs), ["Done."], pairs)
        self.assertTrue(any(ty == "progress" and "only announces a wait" in tx for ty, tx in pairs), pairs)
        self.assertEqual(len(sent), 2)
        self.assertEqual(res["continuations"], 1)

    def test_long_report_with_the_phrase_is_a_reply(self):
        res, pairs, seen, sent = self.run_session(["long_text"])
        self.assertEqual(len(self.replies(pairs)), 1)
        self.assertEqual(len(sent), 1)

    def test_three_continuations_then_stuck_without_requeue(self):
        res, pairs, seen, sent = self.run_session(["bg_silent"] * 6)
        stuck = [tx for ty, tx in pairs if ty == "reply" and tx.startswith("⏳ Stuck on a background run")]
        self.assertEqual(len(stuck), 1, pairs)
        self.assertEqual(sum(1 for ty, tx in pairs if ty == "progress" and "Automatic continuation" in tx), 3, pairs)
        self.assertEqual(len(sent), 4)                            # order + 3 continuations, no fifth
        self.assertEqual(res["continuations"], 3)
        self.assertEqual(res["stuck"]["task_id"], 100)
        self.assertEqual(res["stuck"]["continuations"], 3)
        self.assertEqual(res["requeue"], [])
        self.assertFalse(any(ty == "failed" for ty, _ in pairs), pairs)


class BackgroundParsing(unittest.TestCase):
    def parse(self, **o):
        import importlib.util
        path = os.path.join(HERE, "..", "..", "agent-runner", "providers", "__init__.py")
        spec = importlib.util.spec_from_file_location("aiw_bg_providers", path,
                                                      submodule_search_locations=[os.path.dirname(path)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["aiw_bg_providers"] = mod
        spec.loader.exec_module(mod)
        return [e for e in mod.get_adapter("claude").parse_line(json.dumps(o)) if e["kind"] == "background"]

    def test_events(self):
        full = self.parse(type="system", subtype="background_tasks_changed",
                          tasks=[{"task_id": "a", "task_type": "local_bash", "description": "x"}])
        self.assertEqual(full[0]["tasks"][0]["id"], "a")
        self.assertEqual(self.parse(type="system", subtype="background_tasks_changed", tasks=[])[0]["tasks"], [])
        self.assertEqual(self.parse(type="system", subtype="task_started", task_id="a", is_backgrounded=True)[0]
                         ["started"]["id"], "a")
        self.assertEqual(self.parse(type="system", subtype="task_started", task_id="a"), [])
        self.assertEqual(self.parse(type="system", subtype="task_updated", task_id="a",
                                    patch={"status": "killed"})[0]["ended"], "a")
        self.assertEqual(self.parse(type="system", subtype="task_notification", task_id="a")[0]["ended"], "a")


if __name__ == "__main__":
    unittest.main()
