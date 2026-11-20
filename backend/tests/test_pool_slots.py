"""Active worker slots (0.34.0): the runtime switch "only N of M slots active".

Run: cd backend && python3 -m unittest tests.test_pool_slots
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-slots-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-slots-ws-")
_TMP_PROGRESS = tempfile.mkdtemp(prefix="aiw-slots-progress-")
for _d in (_TMP_DATA, _TMP_WS, _TMP_PROGRESS):
    atexit.register(shutil.rmtree, _d, True)
os.environ.setdefault("AIWERKSTATT_PROGRESS_DIR", _TMP_PROGRESS)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
import orchestrator  # noqa: E402
import pool_slots  # noqa: E402
import store  # noqa: E402
from test_pool_snapshot import Snap, drop  # noqa: E402


def reset():
    for n in ("pool-slots.json", "pool-slots.jsonl"):
        try:
            os.remove(os.path.join(config.DATA, n))
        except OSError:
            pass


class Setting(unittest.TestCase):
    def setUp(self):
        reset()

    def test_missing_means_max(self):
        self.assertIsNone(pool_slots.stored())
        self.assertEqual(pool_slots.limit(5), 5)

    def test_broken_file_means_max(self):
        with open(os.path.join(config.DATA, "pool-slots.json"), "w") as f:
            f.write("{nope")
        self.assertEqual(pool_slots.limit(5), 5)

    def test_unlimited_pool_ignores_setting(self):
        self.assertIsNone(pool_slots.limit(0))
        with self.assertRaises(ValueError):
            pool_slots.set_active(1, 0, "x")

    def test_validation(self):
        for bad in (-1, 6, True, "2", 1.5, None):
            with self.assertRaises(ValueError):
                pool_slots.set_active(bad, 5, "x")
        self.assertIsNone(pool_slots.stored())

    def test_persists_and_logs(self):
        r = pool_slots.set_active(2, 5, "alice")
        self.assertEqual((r["before"], r["after"]), (5, 2))
        self.assertEqual(pool_slots.limit(5), 2)          # a fresh read = what a restart sees
        pool_slots.set_active(0, 5, "bob")
        lines = [json.loads(x) for x in open(os.path.join(config.DATA, "pool-slots.jsonl"))]
        self.assertEqual([(x["by"], x["before"], x["after"]) for x in lines], [("alice", 5, 2), ("bob", 2, 0)])
        self.assertTrue(all(x["at"] for x in lines))

    def test_lower_max_wins(self):
        pool_slots.set_active(5, 5, "x")
        self.assertEqual(pool_slots.limit(3), 3)


class PoolBehaviour(unittest.TestCase):
    def setUp(self):
        reset()
        shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
        os.makedirs(orchestrator.TASK_QUEUE)
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old
        reset()

    def test_default_behaviour_unchanged(self):
        o = Snap()
        for n in "abcd":
            o.add_run(n)
        self.assertFalse(o._pool_full())
        o.add_run("e")
        self.assertTrue(o._pool_full())
        s = o.pool_snapshot()
        self.assertEqual((s["active"], s["waiting_reason"]), (5, None))

    def test_zero_starts_nothing(self):
        pool_slots.set_active(0, 5, "x")
        o = Snap()
        self.assertTrue(o._pool_full())
        launched = []
        o._launch = lambda d: launched.append(d)
        self.assertEqual(o._handle({"id": 1, "slug": "p", "thread_id": 1, "kind": "new"}), "requeued_pool")
        self.assertEqual(launched, [])
        self.assertTrue(os.path.exists(os.path.join(orchestrator.TASK_QUEUE, "1.json")))

    def test_lowering_keeps_runs_and_new_ones_wait(self):
        o = Snap()
        for n in "abc":
            o.add_run(n)
        pool_slots.set_active(2, 5, "x")
        self.assertEqual(len(o._running), 3)                 # nothing aborted
        self.assertEqual(o.pool_snapshot()["busy"], 3)
        self.assertTrue(o._pool_full())                      # 3 live >= 2 active: a new order waits
        del o._running["a"]
        self.assertTrue(o._pool_full())                      # 2 live >= 2 active
        del o._running["b"]
        self.assertFalse(o._pool_full())                     # 1 live < 2 active

    def test_waiting_reason_and_eta(self):
        o = Snap()
        o.add_run("a")
        drop(10, "w1", 9001)
        pool_slots.set_active(2, 5, "x")
        s = o.pool_snapshot()
        self.assertEqual(s["waiting_reason"], "waiting because only 2 of 5 slots are active")
        self.assertIn("starts", s["waiting_list"][0]["start_text"])
        pool_slots.set_active(0, 5, "x")
        s = o.pool_snapshot()
        self.assertEqual(s["waiting_reason"], "waiting because all slots are switched off")
        self.assertEqual(s["waiting_list"][0]["start_text"], "starts once a slot is switched on")
        self.assertIsNone(s["waiting_list"][0]["start_in_s"])

    def test_eta_counts_only_active_slots(self):
        o = Snap()
        o.add_run("a")
        o.add_run("b")
        drop(11, "w1", 9001)
        pool_slots.set_active(1, 5, "x")                     # 2 live, 1 active: waits for BOTH to be gone
        s = o.pool_snapshot()
        w = s["waiting_list"][0]
        self.assertIsNotNone(w["start_in_s"])
        self.assertGreater(w["start_in_s"], 0)               # not "free now"
        pool_slots.set_active(5, 5, "x")
        w = o.pool_snapshot()["waiting_list"][0]
        self.assertEqual(w["start_text"], "starts shortly")  # free slots again


class Endpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global wk
        import app as wk
        for n, role in (("slotboss", "admin"), ("slotmember", "member")):
            if not store.get_user(n):
                store.add_user(n, "4321", role, "x")

    def setUp(self):
        reset()
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old
        reset()

    def client(self, name=None):
        c = wk.app.test_client()
        if name:
            with c.session_transaction() as s:
                s["user"] = name
        return c

    def test_admin_reads_and_sets(self):
        c = self.client("slotboss")
        self.assertEqual(c.get("/api/pool/slots").get_json()["active"], 5)
        r = c.post("/api/pool/slots", json={"active": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["active"], r.get_json()["max"]), (1, 5))
        self.assertEqual(c.get("/api/pool").get_json()["active"], 1)
        line = json.loads(open(os.path.join(config.DATA, "pool-slots.jsonl")).read().splitlines()[-1])
        self.assertEqual((line["by"], line["before"], line["after"]), ("slotboss", 5, 1))

    def test_bad_input_is_400(self):
        c = self.client("slotboss")
        for body in ({"active": 9}, {"active": -1}, {"active": "2"}, {}):
            self.assertEqual(c.post("/api/pool/slots", json=body).status_code, 400)
        self.assertIsNone(pool_slots.stored())

    def test_only_admin(self):
        self.assertEqual(self.client("slotmember").post("/api/pool/slots", json={"active": 0}).status_code, 403)
        self.assertEqual(self.client("slotmember").get("/api/pool/slots").status_code, 403)
        self.assertEqual(self.client().post("/api/pool/slots", json={"active": 0}).status_code, 401)
        self.assertIsNone(pool_slots.stored())


if __name__ == "__main__":
    unittest.main()
