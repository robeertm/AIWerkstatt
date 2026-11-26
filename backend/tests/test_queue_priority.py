"""Priority in the task queue (0.48.0): urgent, next, up/down, position; running orders are never touched.

Run: cd backend && python3 -m unittest tests.test_queue_priority
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-prio-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-prio-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import orchestrator  # noqa: E402
import queue_priority as qp  # noqa: E402
import store  # noqa: E402

DATA = os.path.dirname(orchestrator.PRIORITY_FILE)     # whichever test module set the data folder first


def drop(tid, slug="app", thread=None, kind="new"):
    os.makedirs(orchestrator.TASK_QUEUE, exist_ok=True)
    with open(os.path.join(orchestrator.TASK_QUEUE, "%d.json" % tid), "w") as f:
        json.dump({"id": tid, "slug": slug, "thread_id": thread if thread is not None else tid * 100,
                   "kind": kind, "text": "x"}, f)


def reset():
    shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
    os.makedirs(orchestrator.TASK_QUEUE)
    for n in (qp.FILE_NAME, qp.LOG_NAME, qp.FILE_NAME + ".lock"):
        try:
            os.remove(os.path.join(DATA, n))
        except OSError:
            pass


def _log_lines():
    with open(os.path.join(DATA, qp.LOG_NAME)) as f:
        return f.read().splitlines()


def start_order():
    return [d["id"] for d in qp.order(qp.read_queue(orchestrator.TASK_QUEUE), qp.load(orchestrator.PRIORITY_FILE))]


def mv(thread, action, **kw):
    return qp.change(orchestrator.PRIORITY_FILE, orchestrator.TASK_QUEUE, thread * 100, action, "tester", **kw)


class Rule(unittest.TestCase):
    def setUp(self):
        reset()

    def test_without_priority_strictly_fifo_by_task_id(self):
        for t in (12, 9, 10, 11):
            drop(t)
        self.assertEqual(start_order(), [9, 10, 11, 12])           # 9 before 10, numerically

    def test_urgent_before_normal_fifo_inside_the_class(self):
        for t in (1, 2, 3, 4):
            drop(t)
        mv(4, "urgent")
        mv(2, "urgent")
        self.assertEqual(start_order(), [2, 4, 1, 3])
        mv(2, "normal")                                            # back to its FIFO place
        self.assertEqual(start_order(), [4, 1, 2, 3])

    def test_manual_rank_only_inside_the_class(self):
        for t in (1, 2, 3, 4, 5):
            drop(t)
        mv(5, "urgent")
        mv(4, "front")                                             # normal class: 4 first, never before the urgent 5
        self.assertEqual(start_order(), [5, 4, 1, 2, 3])
        mv(4, "down")
        self.assertEqual(start_order(), [5, 1, 4, 2, 3])
        mv(4, "up")
        mv(4, "up")                                                # already first of the class: nothing happens
        r = mv(4, "up")
        self.assertTrue(r["unchanged"])
        self.assertEqual(start_order(), [5, 4, 1, 2, 3])
        mv(4, "end")
        self.assertEqual(start_order(), [5, 1, 2, 3, 4])
        r = mv(4, "down")
        self.assertTrue(r["unchanged"])
        mv(3, "position", position=1)
        self.assertEqual(start_order(), [5, 3, 1, 2, 4])
        mv(3, "position", position=99)                             # beyond the end: last of the class
        self.assertEqual(start_order(), [5, 1, 2, 4, 3])

    def test_moving_in_the_edge_of_the_urgent_class(self):
        for t in (1, 2, 3):
            drop(t)
        mv(1, "urgent")
        mv(2, "urgent")
        mv(2, "front")
        self.assertEqual(start_order(), [2, 1, 3])
        self.assertTrue(mv(3, "up")["unchanged"])                  # first of normal: cannot climb into urgent

    def test_followups_of_a_thread_move_as_one_block(self):
        drop(1)
        drop(2, thread=200)
        drop(3, thread=200, kind="followup")
        drop(4)
        self.assertEqual(start_order(), [1, 2, 3, 4])
        mv(4, "front")
        self.assertEqual(start_order(), [4, 1, 2, 3])
        mv(2, "front")
        self.assertEqual(start_order(), [2, 3, 4, 1])
        v = qp.orders_view(qp.read_queue(orchestrator.TASK_QUEUE), qp.load(orchestrator.PRIORITY_FILE))
        self.assertEqual([(r["thread_id"], r["task_ids"], r["followups"]) for r in v],
                         [(200, [2, 3], 1), (400, [4], 0), (100, [1], 0)])

    def test_control_messages_never_part_of_the_order(self):
        drop(1)
        drop(2, kind="stop")
        self.assertEqual(start_order(), [1])
        self.assertEqual(qp.sort_key({"id": 2, "kind": "stop"}, qp.empty())[0], 0)

    def test_running_or_unknown_order_is_refused(self):
        drop(1)
        with self.assertRaises(LookupError):
            mv(7, "urgent")                                        # not in the queue (running / finished / unknown)
        with self.assertRaises(ValueError):
            mv(1, "teleport")
        for bad in (0, -1, "2", True, None):
            with self.assertRaises(ValueError):
                mv(1, "position", position=bad)
        self.assertFalse(os.path.exists(orchestrator.PRIORITY_FILE))

    def test_entries_of_orders_that_no_longer_wait_are_dropped(self):
        for t in (1, 2):
            drop(t)
        mv(2, "urgent")
        os.remove(os.path.join(orchestrator.TASK_QUEUE, "2.json"))     # started meanwhile
        mv(1, "urgent")
        self.assertEqual(sorted(qp.load(orchestrator.PRIORITY_FILE)["orders"]), ["1"])

    def test_broken_file_means_plain_fifo_and_every_change_is_logged(self):
        for t in (1, 2):
            drop(t)
        with open(orchestrator.PRIORITY_FILE, "w") as f:
            f.write("{nope")
        self.assertEqual(start_order(), [1, 2])
        mv(2, "front")
        mv(2, "up")
        lines = [json.loads(x) for x in _log_lines()]
        self.assertEqual([(x["by"], x["action"], x["thread_id"], x["unchanged"]) for x in lines],
                         [("tester", "front", 200, False), ("tester", "up", 200, True)])

    def test_view_flags_for_the_buttons(self):
        for t in (1, 2, 3):
            drop(t)
        mv(3, "urgent")
        v = qp.orders_view(qp.read_queue(orchestrator.TASK_QUEUE), qp.load(orchestrator.PRIORITY_FILE))
        self.assertEqual([(r["position"], r["urgent"], r["can_up"], r["can_down"]) for r in v],
                         [(1, True, False, False), (2, False, False, True), (3, False, True, False)])


class FakeOrch(orchestrator.Orchestrator):
    def __init__(self):
        self._creds_fn = lambda p: {}
        self._client = None
        self._running = {}
        self._lock = threading.Lock()
        self.launched = []

    def _alive(self, slug):
        return self._running.get(slug)

    def _launch(self, d):
        self.launched.append(int(d["id"]))
        self._running[d["slug"]] = {"thread_id": int(d["thread_id"])}

    def _drop_inbox(self, thread_id, payload):
        pass


class Start(unittest.TestCase):
    """The orchestrator starts in the priority order and never touches what already runs."""

    def setUp(self):
        reset()
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old

    def test_next_free_slot_goes_to_the_urgent_order_running_one_untouched(self):
        o = FakeOrch()
        drop(1, slug="a")
        o.poll_once()                                              # a runs
        for t, slug in ((2, "a"), (3, "b"), (4, "c")):
            drop(t, slug=slug)
        mv(4, "urgent")
        o.poll_once()                                              # b and c start at once (separate workspaces)
        self.assertEqual(o.launched, [1, 4, 3])                    # urgent 4 before 3; 1 never restarted
        self.assertEqual(sorted(os.listdir(orchestrator.TASK_QUEUE)), ["2.json"])
        self.assertEqual(list(o._running), ["a", "c", "b"])

    def test_full_pool_keeps_the_priority_order_for_the_rest(self):
        orchestrator.MAX_PARALLEL_RUNS = 1
        o = FakeOrch()
        for t, slug in ((1, "a"), (2, "b"), (3, "c")):
            drop(t, slug=slug)
        mv(3, "front")
        o.poll_once()
        self.assertEqual(o.launched, [3])
        self.assertEqual(sorted(os.listdir(orchestrator.TASK_QUEUE)), ["1.json", "2.json"])
        self.assertEqual(start_order(), [1, 2])

    def test_unreadable_descriptor_is_dropped_not_fatal(self):
        o = FakeOrch()
        drop(1, slug="a")
        with open(os.path.join(orchestrator.TASK_QUEUE, "2.json"), "w") as f:
            f.write("[1, 2]")                                      # valid JSON, not an object
        o.poll_once()
        self.assertEqual(o.launched, [1])
        self.assertEqual(os.listdir(orchestrator.TASK_QUEUE), [])


class Endpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global wk
        import app as wk
        for n, role in (("prioboss", "admin"), ("priomember", "member")):
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

    def post(self, c, thread, body):
        return c.post("/api/queue/%d/priority" % (thread * 100), json=body)

    def test_admin_changes_and_pool_shows_the_order(self):
        for t in (1, 2, 3):
            drop(t)
        c = self.client("prioboss")
        r = self.post(c, 3, {"action": "urgent"})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertEqual((j["before"], j["after"], j["urgent"], j["unchanged"]), (3, 1, True, False))
        pool = c.get("/api/pool").get_json()
        self.assertEqual([r["thread_id"] for r in pool["queue"]], [300, 100, 200])
        self.assertEqual([r["urgent"] for r in pool["queue"]], [True, False, False])
        self.assertEqual([w["thread_id"] for w in pool["waiting_list"]], [300, 100, 200])
        self.assertEqual(pool["waiting"], 3)
        line = json.loads(_log_lines()[-1])
        self.assertEqual((line["by"], line["action"]), ("prioboss", "urgent"))

    def test_errors_and_rights(self):
        drop(1)
        c = self.client("prioboss")
        self.assertEqual(self.post(c, 9, {"action": "up"}).status_code, 409)       # does not wait
        self.assertEqual(self.post(c, 1, {"action": "nope"}).status_code, 400)
        self.assertEqual(self.post(c, 1, {"action": "position", "position": 0}).status_code, 400)
        self.assertEqual(self.post(self.client("priomember"), 1, {"action": "urgent"}).status_code, 403)
        self.assertEqual(self.post(self.client(), 1, {"action": "urgent"}).status_code, 401)
        self.assertFalse(os.path.exists(orchestrator.PRIORITY_FILE))


if __name__ == "__main__":
    unittest.main()
