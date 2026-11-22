"""Session driver with the stall guard (0.44.0): agent-runner/session.py against a fake `claude`.

Hanging tool -> stage 1 (nudge) -> stage 2 (tool ended, the agent carries on = healed); still hanging -> stage 3 (run ended,
"hung" event, NO "failed" event, no retry request); a long gate run with progress, a growing log and a protected release/gate
step never lead to a stage-3 end; the plain output watchdog never ends a run the guard sees activity on.

Run: cd backend && python3 -m unittest tests.test_session_stall_guard     (~60 s)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "..", "..", "agent-runner", "session.py")

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, threading, time
plan = os.environ["FAKE_PLAN"].split(",")
sid = "22222222-3333-4444-8555-666666666666"
lock = threading.Lock()


def out(o):
    o["session_id"] = sid
    with lock:
        print(json.dumps(o, ensure_ascii=False), flush=True)


def result(text):
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "parent_tool_use_id": None})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text})


def tool(cmd, then):
    out({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "wait"}}]}})
    p = subprocess.Popen(cmd)

    def wait():
        p.wait()
        if then == "silent":
            return
        out({"type": "user", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "rc=%s" % p.returncode}]}})
        result("Done.")
    threading.Thread(target=wait, daemon=True).start()


out({"type": "system", "subtype": "init"})
n = 0
for line in sys.stdin:
    if not line.strip():
        continue
    with open(os.environ["FAKE_INPUT"], "a") as f:
        f.write(line)
    kind = plan[n] if n < len(plan) else "nothing"
    n += 1
    if kind == "hang_answer":
        tool(["sleep", "40"], "answer")
    elif kind == "hang_silent":
        tool(["sleep", "40"], "silent")
    elif kind == "gate":
        tool([sys.executable, os.environ["FAKE_GATE"]], "answer")
    elif kind == "growing_log":
        tool(["bash", "-c", "for i in $(seq 1 40); do echo line >> %s; sleep 0.25; done" % os.environ["FAKE_LOG"]], "answer")
    elif kind == "protected":
        tool([sys.executable, os.environ["FAKE_PROTECTED"]], "silent")
    elif kind == "ok":
        result("Done.")
'''
# a gate run: heartbeat file of this run (thread 7), refreshed every 0.25 s for 9 s; the path comes through the environment,
# never through the command line, so only the gate axis can see it
FAKE_GATE = ("import json,os,time\np=os.environ['AIW_STALL_GUARD_PROGRESS_DIR']+'/gate.json'\nfor i in range(36):\n"
             "    json.dump({'format':'aiwerkstatt-progress/1','run':'gate','thread':7,'stamp':time.time(),'end':None},"
             "open(p,'w'));time.sleep(0.25)\n")
FAKE_PROTECTED = "import os,time\ntime.sleep(10)\nopen(os.environ['FAKE_MARK'],'w').write('done')\n"


class SessionStallGuard(unittest.TestCase):
    def run_session(self, plan, extra=None, limit=120, prep=None):
        t = tempfile.mkdtemp(prefix="aiw-sg-")
        self.addCleanup(shutil.rmtree, t, True)
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        for d in ("events", "inbox", "usage", "wt", "vault/demo", "progress"):
            os.makedirs(os.path.join(t, d))
        first = os.path.join(t, "first.txt")
        with open(first, "w") as f:
            f.write("Do the task.")
        gate = os.path.join(t, "gate.py")
        open(gate, "w").write(FAKE_GATE)
        os.makedirs(os.path.join(t, "scripts"))
        prot = os.path.join(t, "scripts", "release_check.py")      # protected by its script name
        open(prot, "w").write(FAKE_PROTECTED)
        env = dict(os.environ)
        env.update({
            "PATH": bindir + os.pathsep + env.get("PATH", ""), "WK_SLUG": "demo", "WK_THREAD_ID": "7",
            "WK_REPO": "local/demo", "WK_WT": os.path.join(t, "wt"), "WK_MODEL": "claude-haiku-5-5",
            "WK_FIRST_MSG_FILE": first, "WK_FIRST_TASK": "100", "WK_EVENTS": os.path.join(t, "events"),
            "WK_INBOX": os.path.join(t, "inbox"), "WK_RUNLOG": os.path.join(t, "run.log"),
            "WK_USAGE_DIR": os.path.join(t, "usage"), "WK_IDLE_TIMEOUT": "1", "WK_STALL_TIMEOUT": "3",
            "WK_STREAM_RETRY_MAX": "0", "AIW_API_RETRY_DELAYS": "", "AIW_KNOWLEDGE_DIR": os.path.join(t, "vault/demo"),
            "WK_STALL_GUARD": "1", "WK_STALL_GUARD_STAGES": "2,4,6", "WK_STALL_GUARD_TICK_S": "0.5",
            "AIW_STALL_GUARD_PROGRESS_DIR": os.path.join(t, "progress"), "WK_STALL_GUARD_TMP": os.path.join(t, "tmp"),
            "FAKE_PLAN": ",".join(plan), "FAKE_INPUT": os.path.join(t, "input.jsonl"), "FAKE_GATE": gate,
            "FAKE_LOG": os.path.join(t, "grow.log"), "FAKE_PROTECTED": prot, "FAKE_MARK": os.path.join(t, "mark"),
        })
        env.update(extra or {})
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "AIW_MEMORY_DIR"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, SESSION], env=env, capture_output=True, text=True, timeout=limit)
        events = []
        for n in sorted(os.listdir(os.path.join(t, "events"))):
            if n.endswith(".json") and not n.startswith(("status", "limit")):
                with open(os.path.join(t, "events", n)) as f:
                    events.append(json.load(f))
        result = json.loads(r.stdout.strip().splitlines()[-1])
        sent = [json.loads(x) for x in open(env["FAKE_INPUT"])] if os.path.exists(env["FAKE_INPUT"]) else []
        return result, events, sent, t

    @staticmethod
    def texts(events, etype):
        return [e["text"] for e in events if e["type"] == etype]

    def test_off_by_default(self):
        res, events, sent, _t = self.run_session(["ok"], {"WK_STALL_GUARD": ""})
        self.assertEqual(self.texts(events, "reply"), ["Done."])
        self.assertIsNone(res["stall_guard"])

    def test_hanging_tool_stage1_and_stage2_healed(self):
        res, events, sent, _t = self.run_session(["hang_answer", "nothing", "nothing"])
        prog = self.texts(events, "progress")
        self.assertTrue(any("stage 1/3" in x for x in prog), prog)
        self.assertTrue(any("stage 2/3" in x and "sleep" in x for x in prog), prog)
        self.assertTrue(any("healed after stage" in x for x in prog), prog)
        self.assertEqual(self.texts(events, "reply"), ["Done."])
        self.assertEqual(self.texts(events, "hung"), [])
        self.assertFalse(self.texts(events, "failed"))
        self.assertEqual(len(sent), 3)                              # order + nudge + continue
        self.assertIn("Workshop stall guard (stage 1/3)", json.dumps(sent[1]))
        self.assertIn("Workshop stall guard (stage 2/3)", json.dumps(sent[2]))
        self.assertEqual(res["stall_guard"]["reached"], 2)
        self.assertIsNone(res["stall_guard"]["hung"])
        self.assertEqual(res["requeue"], [])

    def test_still_hanging_stage3_no_failed_no_retry(self):
        res, events, sent, _t = self.run_session(["hang_silent", "nothing", "nothing"])
        hung = [e for e in events if e["type"] == "hung"]
        self.assertEqual(len(hung), 1, events)
        self.assertEqual(hung[0]["task_id"], 100)
        self.assertEqual(hung[0]["payload"]["task_id"], 100)
        self.assertIn("last_action", hung[0]["payload"])
        self.assertFalse(self.texts(events, "failed"))              # no double retry through the failed path
        self.assertFalse(self.texts(events, "reply"))
        self.assertEqual(res["requeue"], [])
        self.assertTrue(res["ok"])
        self.assertEqual(res["stall_guard"]["reached"], 3)
        self.assertEqual(res["stall_guard"]["hung"]["task_id"], 100)

    def test_long_gate_with_progress_is_no_stall(self):
        # silent for 9 s (> stage 3 at 6 s, > old watchdog at 3 s): only the gate heartbeat is alive
        res, events, sent, _t = self.run_session(["gate"])
        self.assertEqual(self.texts(events, "reply"), ["Done."], events)
        self.assertFalse([x for x in self.texts(events, "progress") if "Stall guard" in x])
        self.assertEqual(self.texts(events, "hung"), [])
        self.assertEqual(res["stall_guard"]["reached"], 0)

    def test_growing_log_is_no_stall(self):
        res, events, sent, _t = self.run_session(["growing_log"])
        self.assertEqual(self.texts(events, "reply"), ["Done."], events)
        self.assertFalse([x for x in self.texts(events, "progress") if "Stall guard" in x])
        self.assertEqual(res["stall_guard"]["reached"], 0)

    def test_protected_step_is_never_ended(self):
        # release_check.py runs 10 s without any activity: stage 2 leaves it alone, stage 3 waits for it
        res, events, sent, t = self.run_session(["protected", "nothing", "nothing"])
        prog = self.texts(events, "progress")
        self.assertTrue(any("left untouched" in x for x in prog), prog)
        self.assertTrue(any("waits, nothing is aborted" in x for x in prog), prog)
        self.assertTrue(os.path.exists(os.path.join(t, "mark")), "the protected script was ended early")
        self.assertTrue(res["stall_guard"]["deferred"])

    def test_without_the_guard_the_old_watchdog_still_ends_a_silent_run(self):
        res, events, sent, _t = self.run_session(["hang_silent"], {"WK_STALL_GUARD": ""})
        self.assertTrue(self.texts(events, "failed"), events)       # old path: failed + requeue
        self.assertEqual(res["requeue"], [100])


if __name__ == "__main__":
    unittest.main()
