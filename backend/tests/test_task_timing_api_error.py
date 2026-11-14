"""Task vs. run clocks and the API-error state in the live view and the pool (0.18.0).

Run: cd backend && python3 -m unittest tests.test_task_timing_api_error
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-tt-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-tt-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import engine  # noqa: E402
import orchestrator  # noqa: E402
import store  # noqa: E402

T0 = 1_800_000_000


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class FakeContainer:
    status = "running"

    def reload(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.ev = tempfile.mkdtemp(prefix="aiw-tt-ev-")
        self.old = engine.EVENTS_DIR
        engine.EVENTS_DIR = self.ev
        self.tid = db.execute("INSERT INTO threads (project_id, title, created_at) VALUES (?,?,?)",
                              ("p-%d" % time.time_ns(), "T", store.now())).lastrowid

    def tearDown(self):
        engine.EVENTS_DIR = self.old
        shutil.rmtree(self.ev, ignore_errors=True)

    def task(self, started, first=None, status="running"):
        return db.execute("INSERT INTO tasks (thread_id, kind, status, started_at, first_started_at, created_at) "
                          "VALUES (?,?,?,?,?,?)", (self.tid, "new", status, iso(started),
                                                   iso(first) if first else None, store.now())).lastrowid

    def status(self, **kw):
        with open(os.path.join(self.ev, "status-%d.json" % self.tid), "w") as f:
            json.dump(dict({"alive": True, "ctx_pct": 1, "pending": 1}, **kw), f)


class TaskTiming(Base):
    def test_retry_does_not_move_task_since(self):
        self.task(started=T0 + 3000, first=T0)            # third attempt began 50 min after the task did
        self.status(run_started=T0 + 3000)
        d = engine.task_timing(self.tid)
        self.assertEqual(d["task_since"], T0)
        self.assertEqual(d["run_since"], T0 + 3000)
        self.assertEqual(d["started"], T0 + 3000)         # legacy field = latest attempt

    def test_open_command_is_never_older_than_the_task(self):
        self.task(started=T0 + 600, first=T0 + 600)
        self.status(run_started=T0 + 600)
        d = engine.task_timing(self.tid, command_since=T0 + 100)        # e.g. clock/restart skew
        self.assertEqual(d["task_since"], T0 + 100)
        self.assertLessEqual(d["task_since"], d["run_since"])

    def test_run_older_than_first_attempt_wins(self):
        self.task(started=T0 + 60, first=T0 + 60)
        self.status(run_started=T0 + 10)
        self.assertEqual(engine.task_timing(self.tid)["task_since"], T0 + 10)

    def test_old_rows_without_first_attempt_fall_back_to_started_at(self):
        self.task(started=T0)                                  # row from before 0.18.0
        d = engine.task_timing(self.tid)
        self.assertEqual((d["task_since"], d["run_since"]), (T0, T0))

    def test_no_running_task_gives_no_task_since(self):
        self.task(started=T0, first=T0, status="done")
        self.status(run_started=T0)
        d = engine.task_timing(self.tid)
        self.assertNotIn("task_since", d)
        self.assertEqual(d["run_since"], T0)

    def test_migration_adds_the_column_and_ack_keeps_the_first_start(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, thread_id INT, kind TEXT, status TEXT)")
        c.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY)")
        added = db.migrate(c)
        self.assertIn("tasks.first_started_at", added)
        self.assertEqual(db.migrate(c), [])                    # idempotent


class FirstStart(Base):
    def ack(self, task_id):
        with open(os.path.join(self.ev, "e-%d.json" % task_id), "w") as f:
            json.dump({"thread_id": self.tid, "task_id": task_id, "type": "ack", "text": "go"}, f)
        engine.ingest_once()

    def test_first_ack_sets_it_and_a_later_attempt_keeps_it(self):
        tid = db.execute("INSERT INTO tasks (thread_id, kind, status, created_at) VALUES (?,?,?,?)",
                         (self.tid, "new", "queued", store.now())).lastrowid
        self.ack(tid)
        r = db.query_one("SELECT started_at, first_started_at FROM tasks WHERE id=?", (tid,))
        self.assertTrue(r["first_started_at"])
        self.assertEqual(r["first_started_at"], r["started_at"])
        # self-healing re-queues the task with a later attempt start; the first start must survive
        db.execute("UPDATE tasks SET status='queued', first_started_at=?, started_at=? WHERE id=?",
                   ("2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", tid))
        self.ack(tid)
        r = db.query_one("SELECT started_at, first_started_at FROM tasks WHERE id=?", (tid,))
        self.assertEqual(r["first_started_at"], "2026-01-01T00:00:00Z")
        self.assertNotEqual(r["started_at"], "2026-01-01T01:00:00Z")


class ApiErrorLive(Base):
    def write_feed(self, at):
        with open(os.path.join(self.ev, "live-%d.jsonl" % self.tid), "w") as f:
            f.write(json.dumps({"act": "say", "text": "x", "at": at}) + "\n")

    def test_retry_wait_is_its_own_state_and_no_stall_warning(self):
        now = int(time.time())
        self.write_feed(now - 1500)                                   # 25 min quiet would be yellow
        self.status(last_output=now - 1500, api_error={"attempt": 2, "max": 3, "next_attempt": now + 120,
                                                       "gave_up": False, "reason": "403 forbidden"})
        d = engine.live_vitals(self.tid)
        self.assertEqual(d["state"], "api_error")
        self.assertEqual(d["api_error"], {"attempt": 2, "max": 3, "next_attempt": now + 120,
                                          "gave_up": False, "reason": "403 forbidden"})

    def test_gave_up_falls_back_to_the_normal_states(self):
        now = int(time.time())
        self.write_feed(now - 1500)
        self.status(last_output=now - 1500, api_error={"attempt": 3, "max": 3, "next_attempt": 0,
                                                       "gave_up": True, "reason": "overloaded"})
        d = engine.live_vitals(self.tid)
        self.assertEqual(d["state"], "stalled_yellow")
        self.assertTrue(d["api_error"]["gave_up"])

    def test_no_error_no_field_and_junk_is_ignored(self):
        now = int(time.time())
        self.write_feed(now - 5)
        self.status(last_output=now - 5, api_error=None)
        self.assertNotIn("api_error", engine.live_vitals(self.tid))
        self.status(last_output=now - 5, api_error="boom")
        self.assertNotIn("api_error", engine.live_vitals(self.tid))
        self.status(last_output=now - 5, api_error={"attempt": "x", "max": None, "reason": "a" * 500})
        d = engine.live_vitals(self.tid)["api_error"]
        self.assertEqual((d["attempt"], d["max"], len(d["reason"])), (0, 0, 120))

    def test_read_live_carries_task_and_run_clock(self):
        now = int(time.time())
        self.task(started=now - 300, first=now - 3000)
        self.write_feed(now - 5)
        self.status(last_output=now - 5, run_started=now - 300)
        d = engine.read_live(self.tid)
        self.assertAlmostEqual(d["task_since"], now - 3000, delta=2)
        self.assertAlmostEqual(d["run_since"], now - 300, delta=2)


class PoolApiError(Base):
    def test_pool_run_shows_task_start_and_api_error(self):
        o = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
        o._creds_fn = lambda p: {}
        o._client = None
        o._lock = threading.Lock()
        os.makedirs(orchestrator.TASK_QUEUE, exist_ok=True)
        tid = self.task(started=T0 + 900, first=T0)
        o._running = {"alpha": {"container": FakeContainer(), "thread_id": self.tid, "task_id": tid, "model": "m"}}
        self.status(api_error={"attempt": 1, "max": 3, "next_attempt": T0 + 1020, "gave_up": False, "reason": "overloaded"})
        runs = o.pool_snapshot()["runs"]
        self.assertEqual(runs[0]["started_at"], iso(T0))                # task start, not the latest attempt
        self.assertEqual(runs[0]["api_error"]["next_attempt"], T0 + 1020)
        self.status()                                                    # healthy again
        self.assertNotIn("api_error", o.pool_snapshot()["runs"][0])


if __name__ == "__main__":
    unittest.main()
