"""Agents-at-work overview: ``Orchestrator.pool_snapshot()`` and ``GET /api/pool`` (0.15.1).

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-snap-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-snap-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
_TMP_PROGRESS = tempfile.mkdtemp(prefix="aiw-snap-progress-")
atexit.register(shutil.rmtree, _TMP_PROGRESS, True)
os.environ.setdefault("AIWERKSTATT_PROGRESS_DIR", _TMP_PROGRESS)     # never read a real progress folder
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import eta  # noqa: E402
import orchestrator  # noqa: E402
import store  # noqa: E402


class FakeContainer:
    def __init__(self, status="running"):
        self.status = status
        self.id = "c-%d" % id(self)

    def reload(self):
        pass


class Snap(orchestrator.Orchestrator):
    def __init__(self):
        self._creds_fn = lambda p: {}
        self._client = None
        self._running = {}
        self._lock = threading.Lock()
        os.makedirs(orchestrator.TASK_QUEUE, exist_ok=True)

    def add_run(self, project_id, status="running", model="m-1"):
        tid = db.execute("INSERT INTO threads (project_id, title, created_at) VALUES (?,?,?)",
                         (project_id, "T %s %s" % (project_id, "x" * 120), store.now())).lastrowid
        cur = db.execute("INSERT INTO tasks (thread_id, kind, status, started_at, created_at) "
                         "VALUES (?,?,?,?,?)", (tid, "new", "running", "2026-11-12T10:00:00", store.now()))
        self._running[project_id] = {"container": FakeContainer(status), "thread_id": tid,
                                     "task_id": cur.lastrowid, "model": model}
        return tid


def drop(task_id, slug, thread_id, kind="new"):
    with open(os.path.join(orchestrator.TASK_QUEUE, "%d.json" % task_id), "w") as f:
        json.dump({"id": task_id, "slug": slug, "thread_id": thread_id, "kind": kind, "text": "x"}, f)


class PoolSnapshot(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
        os.makedirs(orchestrator.TASK_QUEUE)
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old

    def test_empty(self):
        s = Snap().pool_snapshot()
        self.assertEqual({k: s[k] for k in ("max", "busy", "waiting", "runs")}, {"max": 5, "busy": 0, "waiting": 0, "runs": []})
        self.assertEqual((s["waiting_list"], s["eta_runs"]), ([], []))

    def test_counts_busy_and_waiting(self):
        o = Snap()
        t1 = o.add_run("alpha", model="model-a")
        o.add_run("beta")
        o.add_run("gone", status="exited")            # dead container: not busy
        drop(101, "gamma", 9001)                       # waits for a slot
        drop(102, "delta", 9002, kind="followup")      # follow-up needing a fresh run: waits
        drop(103, "alpha", t1, kind="followup")        # follow-up for a live run: goes straight in
        drop(104, "alpha", t1, kind="stop")            # control message: not work
        with open(os.path.join(orchestrator.TASK_QUEUE, "105.json.tmp"), "w") as f:
            f.write("{")                               # half-written descriptor is ignored
        s = o.pool_snapshot()
        self.assertEqual((s["max"], s["busy"], s["waiting"]), (5, 2, 2))
        self.assertEqual([r["project"] for r in s["runs"]], ["alpha", "beta"])
        a = s["runs"][0]
        self.assertEqual(a["model"], "model-a")
        self.assertEqual(a["started_at"], "2026-11-12T10:00:00")
        self.assertLessEqual(len(a["title"]), 80)
        self.assertTrue(a["title"].startswith("T alpha"))
        self.assertNotIn("gone", [r["project"] for r in s["runs"]])

    def test_eta_fields_are_additive(self):
        folder = tempfile.mkdtemp(prefix="aiw-snap-eta-")
        self.addCleanup(shutil.rmtree, folder, True)
        old = os.environ.get("AIWERKSTATT_PROGRESS_DIR")
        os.environ["AIWERKSTATT_PROGRESS_DIR"] = folder
        self.addCleanup(lambda: os.environ.pop("AIWERKSTATT_PROGRESS_DIR") if old is None
                        else os.environ.__setitem__("AIWERKSTATT_PROGRESS_DIR", old))
        o = Snap()
        t1 = o.add_run("alpha")
        o.add_run("beta")
        drop(201, "gamma", 9001)
        # a run linked to alpha's thread: 2nd phase running, history of equal runs
        run = eta.Run("app", "gate", ["queue", "test"], title="t", folder=folder, heartbeat=False, thread=t1).start()
        run.phase("queue")
        run.phase("test", total=10)
        run.tests(done=2)
        for i in range(3):
            with open(os.path.join(folder, "history-x.jsonl"), "a") as fh:
                fh.write(json.dumps({"profile": "app", "kind": "gate", "run": "h%d" % i, "end": 1 + i, "result": "ok",
                                     "phases": {"queue": 5, "test": 600}, "skipped": [], "tests": {"test": 10}, "load": {}}) + "\n")
        s = o.pool_snapshot()
        self.assertEqual((s["busy"], s["waiting"]), (2, 1))
        a, b = s["runs"]
        for k in ("project", "title", "started_at", "model"):
            self.assertIn(k, a)
        self.assertEqual(a["phase"], "test run")
        self.assertIn("2/10", a["eta_line"])
        self.assertEqual(a["run"]["progress"]["done"], 2)
        self.assertTrue(a["eta_text"].startswith("done in"))
        self.assertIsInstance(a["eta_s"], int)
        self.assertEqual(len(a["eta_spanne"]), 2)
        self.assertIsNone(b["run"])
        self.assertIn("unknown", b["eta_text"])
        self.assertEqual(len(s["waiting_list"]), 1)
        self.assertEqual(s["waiting_list"][0]["project"], "gamma")
        self.assertIn("start_in_s", s["waiting_list"][0])
        self.assertTrue(s["waiting_list"][0]["start_text"])
        self.assertEqual(len(s["eta_runs"]), 1)

    def test_broken_progress_never_breaks_the_view(self):
        o = Snap()
        o.add_run("alpha")
        old = eta.load_runs
        eta.load_runs = lambda *a, **k: 1 / 0
        try:
            s = o.pool_snapshot()
        finally:
            eta.load_runs = old
        self.assertEqual((s["busy"], s["waiting"], len(s["runs"])), (1, 0, 1))
        self.assertNotIn("eta_text", s["runs"][0])

    def test_unlimited_mode(self):
        orchestrator.MAX_PARALLEL_RUNS = 0
        o = Snap()
        for n in ("a", "b", "c", "d", "e", "f", "g"):
            o.add_run(n)
        s = o.pool_snapshot()
        self.assertEqual(s["max"], 0)
        self.assertEqual(s["busy"], 7)


class PoolEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global wk
        import app as wk
        if not store.get_user("poolboss"):
            store.add_user("poolboss", "4321", "admin", "x")
        if not store.get_user("poolmember"):
            store.add_user("poolmember", "4321", "member", "x")

    def client(self, name=None):
        c = wk.app.test_client()
        if name:
            with c.session_transaction() as s:
                s["user"] = name
        return c

    def test_admin_gets_snapshot(self):
        r = self.client("poolboss").get("/api/pool")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue({"max", "busy", "waiting", "runs"} <= set(d))          # existing fields stay
        self.assertTrue(set(d) <= {"max", "busy", "waiting", "runs", "waiting_list", "eta_runs", "test_runner"})
        self.assertIsInstance(d["runs"], list)

    def test_member_is_refused(self):
        self.assertEqual(self.client("poolmember").get("/api/pool").status_code, 403)

    def test_anonymous_is_refused(self):
        self.assertEqual(self.client().get("/api/pool").status_code, 401)


if __name__ == "__main__":
    unittest.main()
