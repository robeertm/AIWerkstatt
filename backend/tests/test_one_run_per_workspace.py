"""One working tree per project: never two runs in the same workspace, follow-ups never lost.

Run: cd backend && python3 -m unittest tests.test_one_run_per_workspace
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-onerun-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-onerun-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import orchestrator  # noqa: E402


class FakeOrch(orchestrator.Orchestrator):
    def __init__(self):
        self._creds_fn = lambda p: {}
        self._client = None
        self._running = {}
        self._lock = __import__("threading").Lock()
        self.launched = []
        self.inbox = []
        os.makedirs(orchestrator.TASK_QUEUE, exist_ok=True)

    def _alive(self, slug):
        return self._running.get(slug)

    def _launch(self, d):
        self.launched.append((int(d["id"]), d.get("kind", "new")))
        self._running[d["slug"]] = {"thread_id": int(d["thread_id"])}

    def _drop_inbox(self, thread_id, payload):
        self.inbox.append((thread_id, payload["id"]))


def drop(tid, slug, thread_id, kind="new"):
    with open(os.path.join(orchestrator.TASK_QUEUE, "%d.json" % tid), "w") as f:
        json.dump({"id": tid, "slug": slug, "thread_id": thread_id, "kind": kind, "text": "x"}, f)


def queued():
    return sorted(os.listdir(orchestrator.TASK_QUEUE))


class OneRunPerWorkspace(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(orchestrator.TASK_QUEUE, ignore_errors=True)
        os.makedirs(orchestrator.TASK_QUEUE)
        self.old = orchestrator.MAX_PARALLEL_RUNS
        orchestrator.MAX_PARALLEL_RUNS = 5

    def tearDown(self):
        orchestrator.MAX_PARALLEL_RUNS = self.old

    def test_other_threads_wait_while_project_busy_then_start_in_order(self):
        o = FakeOrch()
        drop(10, "app", thread_id=1)                 # thread A starts
        o.poll_once()
        self.assertEqual(o.launched, [(10, "new")])
        drop(11, "app", thread_id=2)                 # new order, thread B — same project
        drop(12, "app", thread_id=3, kind="followup")  # follow-up of an older thread C — same project
        drop(13, "app", thread_id=1, kind="followup")  # follow-up of the RUNNING thread A
        o.poll_once()
        self.assertEqual(o.launched, [(10, "new")])            # nothing else runs in the busy workspace
        self.assertEqual(o.inbox, [(1, 13)])                   # A's follow-up goes into its live session
        self.assertEqual(queued(), ["11.json", "12.json"])     # B and C wait — nothing is lost
        o._running.pop("app")                                  # A finishes
        o.poll_once()
        self.assertEqual(o.launched[-1], (11, "new"))          # strictly in order
        self.assertEqual(queued(), ["12.json"])
        o._running.pop("app")
        o.poll_once()
        self.assertEqual(o.launched[-1], (12, "followup"))
        self.assertEqual(queued(), [])

    def test_different_projects_run_in_parallel(self):
        o = FakeOrch()
        drop(20, "one", thread_id=5)
        drop(21, "two", thread_id=6)
        drop(22, "two", thread_id=7, kind="followup")
        o.poll_once()
        self.assertEqual(o.launched, [(20, "new"), (21, "new")])
        self.assertEqual(queued(), ["22.json"])


if __name__ == "__main__":
    unittest.main()
