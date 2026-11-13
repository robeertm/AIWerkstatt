"""Fixed worker pool (strict FIFO) and the subagent guard (0.15.0).

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-pool-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-pool-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
GUARD = os.path.join(HERE, "..", "..", "agent-runner", "providers", "subagent_guard.py")

import orchestrator  # noqa: E402


class FakePool(orchestrator.Orchestrator):
    def __init__(self, cap):
        self._creds_fn = lambda p: {}
        self._client = None
        self._running = {}
        self._lock = __import__("threading").Lock()
        self.launched = []
        self.cap = cap
        os.makedirs(orchestrator.TASK_QUEUE, exist_ok=True)

    def _alive(self, slug):
        return self._running.get(slug)

    def _launch(self, d):
        self.launched.append(int(d["id"]))
        self._running[d["slug"]] = {"thread_id": int(d["thread_id"])}


def drop(tid, slug):
    with open(os.path.join(orchestrator.TASK_QUEUE, "%d.json" % tid), "w") as f:
        json.dump({"id": tid, "slug": slug, "thread_id": tid, "kind": "new", "text": "x"}, f)


class WorkerPool(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
        os.makedirs(orchestrator.TASK_QUEUE)
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old

    def test_five_run_two_wait_in_order(self):
        o = FakePool(5)
        for tid in (8, 9, 10, 11, 12, 13, 14):
            drop(tid, "p%d" % tid)
        o.poll_once()
        self.assertEqual(o.launched, [8, 9, 10, 11, 12])           # numeric order, 9 before 10
        self.assertEqual(sorted(os.listdir(orchestrator.TASK_QUEUE)), ["13.json", "14.json"])
        o._running.pop("p9")                                       # one slot frees up
        o.poll_once()
        self.assertEqual(o.launched[-1], 13)                       # strictly the next in line
        self.assertEqual(os.listdir(orchestrator.TASK_QUEUE), ["14.json"])

    def test_zero_means_unlimited(self):
        orchestrator.MAX_PARALLEL_RUNS = 0
        o = FakePool(0)
        for tid in range(1, 8):
            drop(tid, "q%d" % tid)
        o.poll_once()
        self.assertEqual(len(o.launched), 7)


def guard(event, state, extra=(), **kw):
    d = {"hook_event_name": event, "tool_name": "Agent", "tool_use_id": kw.pop("tid", "t1"),
         "tool_input": kw.pop("tool_input", {"description": "d", "prompt": "p"})}
    d.update(kw)
    r = subprocess.run([sys.executable, GUARD, "--model", "claude-sonnet-5-5", "--max", "2", "--state", state] + list(extra),
                       input=json.dumps(d), capture_output=True, text=True)
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None


class SubagentGuard(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="aiw-guard-")

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def test_default_haiku_and_synchronous(self):
        h = guard("PreToolUse", self.state)
        self.assertEqual(h["permissionDecision"], "allow")
        self.assertEqual(h["updatedInput"]["model"], "haiku")
        self.assertFalse(h["updatedInput"]["run_in_background"])

    def test_at_most_two_then_serial(self):
        self.assertEqual(guard("PreToolUse", self.state, tid="a")["permissionDecision"], "allow")
        self.assertEqual(guard("PreToolUse", self.state, tid="b")["permissionDecision"], "allow")
        self.assertEqual(guard("PreToolUse", self.state, tid="c")["permissionDecision"], "deny")
        guard("PostToolUse", self.state, tid="a")
        self.assertEqual(guard("PreToolUse", self.state, tid="c")["permissionDecision"], "allow")

    def test_no_more_expensive_model_and_depth_one(self):
        h = guard("PreToolUse", self.state, tool_input={"description": "d", "prompt": "p", "model": "opus"})
        self.assertEqual(h["permissionDecision"], "deny")
        h = guard("PreToolUse", self.state, agent_id="sub-1")
        self.assertEqual(h["permissionDecision"], "deny")


    def test_rules_block_is_prepended_once_and_other_fields_unchanged(self):
        ti = {"description": "d", "prompt": "Find the bug in parser.py", "subagent_type": "general-purpose"}
        extra = ["--knowledge", "/vault/demo", "--memory", "/mem"]
        h = guard("PreToolUse", self.state, extra=extra, tool_input=dict(ti))
        new = h["updatedInput"]
        block, _, rest = new["prompt"].partition("--- Task ---\n")
        self.assertTrue(new["prompt"].startswith("[AIWerkstatt subagent]"))
        self.assertEqual(rest, ti["prompt"])                       # the original prompt follows, untouched
        self.assertLessEqual(len(block.encode("utf-8")) + len("--- Task ---\n"), 1536)
        self.assertIn("/vault/demo", block)
        self.assertIn("/mem", block)
        self.assertEqual(new["description"], "d")
        self.assertEqual(new["subagent_type"], "general-purpose")
        self.assertEqual(new["model"], "haiku")
        # a prompt that already carries the block is not wrapped a second time
        h2 = guard("PreToolUse", self.state, extra=extra, tid="t2",
                   tool_input={"description": "d", "prompt": new["prompt"]})
        self.assertEqual(h2["updatedInput"]["prompt"].count("[AIWerkstatt subagent]"), 1)

    def test_rules_block_without_folders_and_with_huge_paths_stays_small(self):
        h = guard("PreToolUse", self.state)
        self.assertTrue(h["updatedInput"]["prompt"].startswith("[AIWerkstatt subagent]"))
        self.assertNotIn("Knowledge is read-only", h["updatedInput"]["prompt"])
        h = guard("PreToolUse", self.state, extra=["--knowledge", "/k" * 3000], tid="t3")
        block = h["updatedInput"]["prompt"].partition("--- Task ---\n")[0]
        self.assertLessEqual(len(block.encode("utf-8")), 1536)


if __name__ == "__main__":
    unittest.main()
