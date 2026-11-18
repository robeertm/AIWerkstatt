"""Session driver: the plan (todo list) survives a resume.

Run: cd backend && python3 -m unittest tests.test_session_plan_resume
The last todo list is saved next to the status file on every change, taken over at start (display always, task list
only when the SAME session resumes), discarded by a real reply and ignored when older than 7 days.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "..", "..", "agent-runner", "session.py")
SID = "11111111-2222-4333-8444-555555555555"


def load(events, resume_sid=""):
    env = {"WK_SLUG": "x", "WK_THREAD_ID": "7", "WK_FIRST_MSG_FILE": "/dev/null", "WK_FIRST_TASK": "1",
           "WK_EVENTS": events, "WK_INBOX": events, "WK_RUNLOG": os.path.join(events, "run.jsonl"),
           "WK_RESUME_SID": resume_sid}
    old = dict(os.environ)
    os.environ.update(env)
    # the backend has its own `providers` package: hide it while the runner imports ITS providers
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "providers" or k.startswith("providers.")}
    try:
        spec = importlib.util.spec_from_file_location("session_plan_%d" % time.time_ns(), SESSION)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        runner = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "providers" or k.startswith("providers.")}
        sys.modules.update(saved)
        load.runner = runner
        os.environ.clear()
        os.environ.update(old)
    m.log = lambda *a, **k: None
    return m


def tasks(done=2, total=3):
    out = {}
    for i in range(1, total + 1):
        out[str(i)] = {"subject": "step %d" % i, "active": "doing %d" % i,
                       "status": "completed" if i <= done else ("in_progress" if i == done + 1 else "pending")}
    return out


class PlanResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiw-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def first_run(self):
        m = load(self.tmp)
        m.LIVE["sid"] = SID
        m.LIVE["tasks"] = tasks()
        m.LIVE["todo"] = m.plan_summary(m.LIVE["tasks"])
        m.plan_save()
        return m

    def test_summary(self):
        m = load(self.tmp)
        self.assertEqual(m.plan_summary(tasks()), {"done": 2, "total": 3, "current": "doing 3"})

    def test_same_session_carries_task_list(self):
        self.first_run()
        m2 = load(self.tmp, SID)
        got = m2.plan_take_over(SID)
        self.assertEqual(got["3"]["status"], "in_progress")
        self.assertEqual((m2.LIVE["todo"]["done"], m2.LIVE["todo"]["total"]), (2, 3))
        self.assertTrue(m2.LIVE["todo"]["carried_over"])
        m2.write_status(5, True, 1)
        with open(os.path.join(self.tmp, "status-7.json")) as f:
            st = json.load(f)
        self.assertTrue(st["todo"]["carried_over"])

    def test_other_session_is_display_only(self):
        self.first_run()
        m2 = load(self.tmp, "another-session")
        self.assertIsNone(m2.plan_take_over("another-session"))
        self.assertEqual(m2.LIVE["todo"]["total"], 3)
        self.assertIsNone(m2.LIVE["tasks"])

    def test_real_reply_discards(self):
        m = self.first_run()
        m.plan_discard()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plan-7.json")))
        self.assertIsNone(load(self.tmp, SID).plan_take_over(SID))

    def test_too_old_empty_or_broken_is_ignored(self):
        path = os.path.join(self.tmp, "plan-7.json")
        for body in ({"sid": SID, "todo": {"done": 1, "total": 2}, "saved": int(time.time()) - 8 * 86400},
                     {"sid": SID, "todo": {"done": 0, "total": 0}, "saved": int(time.time())}, "broken"):
            with open(path, "w") as f:
                f.write(body if isinstance(body, str) else json.dumps(body))
            m = load(self.tmp, SID)
            self.assertIsNone(m.plan_take_over(SID))
            self.assertFalse(m.LIVE.get("todo"))

    def test_adapter_seeds_and_emits_full_plan(self):
        a = load(self.tmp).adapter
        a.seed_plan(tasks())
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "TaskUpdate",
                           "input": {"taskId": "3", "status": "completed"}}]}})
        ev = [e for e in a.parse_line(line) if e.get("act") == "plan"][0]
        self.assertEqual(ev["plan"]["3"]["status"], "completed")
        self.assertEqual(len(ev["plan"]), 3)
        a.seed_plan({})


if __name__ == "__main__":
    unittest.main()
