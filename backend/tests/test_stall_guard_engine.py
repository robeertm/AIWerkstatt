"""Stall guard, engine side (0.44.0): a `hung` event re-queues the task at the front with context (no second retry through
the self-healer); first hang = same session, second = fresh session, then the old path. Remaining time: "overdue since ..."
in the pool overview (rules in eta.overdue_s).

Run: cd backend && python3 -m unittest tests.test_stall_guard_engine
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-sge-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-sge-ws-")
_TMP_PROGRESS = tempfile.mkdtemp(prefix="aiw-sge-progress-")
for _d in (_TMP_DATA, _TMP_WS, _TMP_PROGRESS):
    atexit.register(shutil.rmtree, _d, True)
os.environ.setdefault("AIWERKSTATT_PROGRESS_DIR", _TMP_PROGRESS)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402
import engine  # noqa: E402
import eta  # noqa: E402
import orchestrator  # noqa: E402
import store  # noqa: E402
from test_pool_snapshot import Snap  # noqa: E402


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Requeue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ONE project for the class: in the shared `unittest discover` run every test module uses the same database, and a
        # project per test used up the port range (8100-8199) for later modules.
        # Earlier modules of the same run may already have filled 8100-8199 — this class takes its own range.
        alt = engine.config.PORT_RANGE
        engine.config.PORT_RANGE = (28100, 28199)
        try:
            p = engine.create_project("Stall %d" % time.time_ns(), "x", "d", "tester")
        finally:
            engine.config.PORT_RANGE = alt
        cls.project = p if isinstance(p, dict) else engine.get_project(p)

    def setUp(self):
        self.ev = tempfile.mkdtemp(prefix="aiw-sge-ev-")
        self.q = tempfile.mkdtemp(prefix="aiw-sge-q-")
        self.old = (engine.EVENTS_DIR, engine.TASK_QUEUE)
        engine.EVENTS_DIR, engine.TASK_QUEUE = self.ev, self.q
        r = engine.create_thread(self.project, "Build it", "Build a clock.", "tester")
        self.thread = r["thread_id"]
        self.task = db.query_one("SELECT id FROM tasks WHERE thread_id=?", (self.thread,))["id"]
        db.execute("UPDATE threads SET session_id='sess-1' WHERE id=?", (self.thread,))
        self.n = 0
        os.remove(os.path.join(self.q, "%d.json" % self.task))     # the first run claimed it

    def tearDown(self):
        engine.EVENTS_DIR, engine.TASK_QUEUE = self.old
        shutil.rmtree(self.ev, ignore_errors=True)
        shutil.rmtree(self.q, ignore_errors=True)

    def hung(self, payload=None):
        self.n += 1
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (self.task,))
        with open(os.path.join(self.ev, "e-%d.json" % self.n), "w") as f:
            json.dump({"thread_id": self.thread, "task_id": self.task, "type": "hung", "text": "hung",
                       "payload": payload or {"quiet_s": 2400, "last_action": "Bash", "session_id": "sess-1",
                                              "state": "Branch: work, last commit: abc done"}}, f)
        engine.ingest_once()

    def descriptor(self):
        with open(os.path.join(self.q, "%d.json" % self.task)) as f:
            return json.load(f)

    def row(self):
        return db.query_one("SELECT status, attempts, error FROM tasks WHERE id=?", (self.task,))

    def test_first_hang_continues_the_same_session_at_the_front(self):
        self.hung()
        d = self.descriptor()
        self.assertEqual((d["kind"], d["session_id"], d["id"]), ("followup", "sess-1", self.task))   # same id = FIFO front
        self.assertIn("[Workshop stall guard]", d["text"])
        self.assertIn("Branch: work, last commit: abc done", d["text"])
        self.assertIn("Build a clock.", d["text"])
        r = self.row()
        self.assertEqual((r["status"], r["attempts"] or 0, r["error"]), ("queued", 0, None))        # no retry counted
        ev = [e["text"] for e in db.query_all("SELECT text FROM events WHERE thread_id=? AND type='retrying'", (self.thread,))]
        self.assertTrue(any("re-queued at the front (hang 1 of 2)" in x for x in ev), ev)

    def test_front_of_the_queue_is_fifo_by_task_id(self):
        self.hung()
        newer = self.task + 5
        names = [self.task, newer]
        order = sorted(("%d.json" % i for i in names), key=orchestrator.Orchestrator._queue_order)
        self.assertEqual(order[0], "%d.json" % self.task)

    def test_second_hang_starts_a_fresh_session(self):
        self.hung()
        os.remove(os.path.join(self.q, "%d.json" % self.task))
        self.hung()
        d = self.descriptor()
        self.assertEqual((d["kind"], d["session_id"]), ("new", ""))
        self.assertTrue(d["text"].startswith(engine.PREAMBLE))
        self.assertIn("Build a clock.", d["text"])
        self.assertIn("[Workshop stall guard]", d["text"])
        self.assertEqual(self.row()["status"], "queued")

    def test_third_hang_hands_over_to_the_old_self_healing_path(self):
        for _ in range(2):
            self.hung()
            os.remove(os.path.join(self.q, "%d.json" % self.task))
        self.hung()
        self.assertFalse(os.path.exists(os.path.join(self.q, "%d.json" % self.task)))
        r = self.row()
        self.assertEqual((r["status"], r["error"]), ("failed", None))      # self-healer picks it up (no final error)

    def test_no_descriptor_for_a_finished_task(self):
        self.n += 1
        db.execute("UPDATE tasks SET status='done' WHERE id=?", (self.task,))
        with open(os.path.join(self.ev, "e-9.json"), "w") as f:
            json.dump({"thread_id": self.thread, "task_id": self.task, "type": "hung", "text": "hung"}, f)
        engine.ingest_once()
        self.assertFalse(os.path.exists(os.path.join(self.q, "%d.json" % self.task)))

    def test_self_healer_does_not_queue_it_a_second_time(self):
        self.hung()
        # descriptor present -> not claimed yet; other modules of a shared run may leave their own stale tasks behind
        mine = [x for x in engine.resurrect_stale_tasks(lambda pid: False) if self.task in tuple(x)[1:3]]
        self.assertEqual(mine, [])
        self.assertEqual(self.row()["attempts"] or 0, 0)


class OverdueInPool(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
        os.makedirs(orchestrator.TASK_QUEUE)
        self.ev = tempfile.mkdtemp(prefix="aiw-sge-ev2-")
        self.old = (engine.EVENTS_DIR, orchestrator.MAX_PARALLEL_RUNS)
        engine.EVENTS_DIR = self.ev
        orchestrator.MAX_PARALLEL_RUNS = 5
        db.execute("DELETE FROM tasks WHERE status='done'")

    def tearDown(self):
        engine.EVENTS_DIR, orchestrator.MAX_PARALLEL_RUNS = self.old
        shutil.rmtree(self.ev, ignore_errors=True)

    def run_with(self, started_ago, status=None):
        o = Snap()
        tid = o.add_run("alpha")
        db.execute("UPDATE tasks SET started_at=?, first_started_at=? WHERE thread_id=?",
                   (iso(time.time() - started_ago),) * 2 + (tid,))
        if status is not None:
            with open(os.path.join(self.ev, "status-%d.json" % tid), "w") as f:
                json.dump(status, f)
        return o.pool_snapshot()["runs"][0]

    def test_steps_stand_over_pace_is_overdue(self):
        now = time.time()
        r = self.run_with(3600, {"todo": {"done": 2, "total": 5, "current": "x", "stand_since": int(now - 6000)}})
        self.assertTrue(r["eta_text"].startswith("overdue since "), r)
        self.assertAlmostEqual(r["eta_overdue_since"], now - 600, delta=5)

    def test_steps_recent_stand_is_not_overdue(self):
        r = self.run_with(3600, {"todo": {"done": 2, "total": 5, "current": "x", "stand_since": int(time.time() - 600)}})
        self.assertIsNone(r["eta_overdue_since"])
        self.assertTrue(r["eta_text"].startswith("done in"), r)

    def test_experience_overrun_is_overdue(self):
        # Experience = median of the latest finished tasks in the WHOLE database — other tests leave done tasks behind, so
        # enough 20-minute rows that they decide the median on their own, at realistic (past) times.
        t0 = time.time() - 6 * 3600
        for i in range(25):
            db.execute("INSERT INTO tasks(thread_id,kind,status,started_at,finished_at,created_at) VALUES(987654,'new','done',?,?,?)",
                       (iso(t0 + 60 * i), iso(t0 + 60 * i + 1200), store.now()))
        r = self.run_with(3600)
        self.assertTrue(r["eta_text"].startswith("overdue since "), r)
        self.assertTrue(r["eta_overdue_since"])

    def test_stall_guard_view_in_the_pool(self):
        now = int(time.time())
        r = self.run_with(60, {"stall_guard": {"stage": 1, "quiet_s": 900, "last_activity": now - 900,
                                              "axes": {"output": 900}, "thresholds": [900, 1500, 2400], "deferred": False}})
        self.assertEqual(r["stall_guard"]["stage"], 1)
        self.assertIn("nudged", r["stall_guard"]["text"])


class OverdueRules(unittest.TestCase):
    def test_experience(self):
        t = {"source": "experience"}
        self.assertIsNone(eta.overdue_s(1700, t, experience_s=1200))             # < max(1800, 2100)
        self.assertEqual(eta.overdue_s(2400, t, experience_s=1200), 300)
        self.assertIsNone(eta.overdue_s(2400, t))                                # no experience value: nothing invented

    def test_steps(self):
        t = {"source": "steps"}
        self.assertEqual(eta.overdue_s(7200, t, step_since_s=1200, done=5, total=5), 300)   # all done, state 20 min old
        self.assertIsNone(eta.overdue_s(7200, t, step_since_s=600, done=5, total=5))
        self.assertIsNone(eta.overdue_s(3600, t, step_since_s=5000, done=2, total=5))      # pace 1800 -> limit 5400
        self.assertEqual(eta.overdue_s(3600, t, step_since_s=6000, done=2, total=5), 600)
        self.assertIsNone(eta.overdue_s(3600, t, step_since_s=2000, done=0, total=5))      # no pace: limit 45 min
        self.assertEqual(eta.overdue_s(3600, t, step_since_s=3700, done=0, total=5), 1000)

    def test_a_running_gate_is_never_overdue(self):
        self.assertIsNone(eta.overdue_s(99999, {"source": "run"}, step_since_s=99999, experience_s=60, done=1, total=9))
        self.assertIsNone(eta.overdue_s(99999, {"source": None}))

    def test_text(self):
        now = time.time()
        self.assertRegex(eta.overdue_text(now - 600, now), r"^overdue since \d\d:\d\d$")
        self.assertRegex(eta.overdue_text(now - 30 * 3600, now), r"^overdue since \d\d-\d\d \d\d:\d\d$")


class StallGuardDefault(unittest.TestCase):
    def test_on_by_default_zero_turns_it_off(self):
        import importlib
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        orch = importlib.import_module("orchestrator")
        self.assertEqual(orch.stall_guard_env({}), {"WK_STALL_GUARD": "1"})
        self.assertEqual(orch.stall_guard_env({"WK_STALL_GUARD": ""}), {"WK_STALL_GUARD": "1"})
        self.assertEqual(orch.stall_guard_env({"WK_STALL_GUARD": "0"}), {"WK_STALL_GUARD": "0"})
        self.assertEqual(orch.stall_guard_env({"WK_STALL_GUARD": "off", "WK_STALL_GUARD_STAGES": "60,120,180"}),
                         {"WK_STALL_GUARD": "0", "WK_STALL_GUARD_STAGES": "60,120,180"})


if __name__ == "__main__":
    unittest.main()
