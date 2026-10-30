"""Restricted tasks: a task created with a service token never goes live by itself.

Run: cd backend && python3 -m unittest discover -s tests
The backend modules are imported lazily in setUpClass, so whichever test module loads
first decides the shared data directory; this module only adds a throwaway workspace root.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-review-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-review-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class ReviewGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, review, wk
        import db  # noqa: F401
        import engine
        import store
        import review
        import app as wk
        cls.ws_root = review.WORKSPACES
        assert os.path.realpath(cls.ws_root).startswith(os.path.realpath(tempfile.gettempdir())) \
            or cls.ws_root == _TMP_WS, "tests must not touch a real workspace root"
        cls.token = store.api_token_create("Assistant-X")
        if not store.get_user("reviewmember"):
            store.add_user("reviewmember", "4321", "member", "x")
        cls.c = wk.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP_WS, ignore_errors=True)
        shutil.rmtree(_TMP_DATA, ignore_errors=True)   # unused when another module set the data dir

    def setUp(self):
        self.deploys = []
        self._orig_deploy = wk.ORCH.deploy
        wk.ORCH.deploy = lambda pid, port, tid=None: self.deploys.append((pid, tid)) or True
        p = engine.create_project("Gate %s" % self.id().rsplit(".", 1)[-1][:14], "x", "d",
                                  "admin", provider="demo")
        self.pid = p["id"]
        self.ws = os.path.join(self.ws_root, self.pid)
        os.makedirs(os.path.join(self.ws, "node_modules", "dep"))
        os.makedirs(os.path.join(self.ws, "src"))
        self.write("index.html", "v1")
        self.write("src/app.js", "one")
        self.write("node_modules/dep/index.js", "dep")
        self.write(".env.example", "X=")

    def tearDown(self):
        wk.ORCH.deploy = self._orig_deploy

    # ---- helpers ----
    def write(self, rel, text):
        with open(os.path.join(self.ws, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.ws, rel), encoding="utf-8") as f:
            return f.read()

    def token_h(self):
        return {"Authorization": "Bearer " + self.token}

    def sign_in(self, name="admin"):
        c = wk.app.test_client()
        with c.session_transaction() as s:
            s["user"] = name
        return c

    def token_task(self, **extra):
        r = self.c.post("/api/projects/%s/threads" % self.pid, headers=self.token_h(),
                        json={"title": "Change", "request": "Do it", **extra})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["id"]

    def reply(self, tid):
        db.execute("UPDATE tasks SET status='done' WHERE thread_id=?", (tid,))
        return wk._on_event(self.pid, tid, "reply")

    def wait_deploys(self, n=1):
        for _ in range(100):
            if len(self.deploys) >= n:
                return
            time.sleep(0.02)

    # ---- tests ----
    def test_token_task_gets_origin_snapshot_and_hold(self):
        tid = self.token_task()
        th = db.query_one("SELECT origin FROM threads WHERE id=?", (tid,))
        self.assertEqual(th["origin"], "api:assistant-x")
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertTrue(engine.project_held(self.pid))
        snap = review.snapshot_path(self.pid)
        with open(os.path.join(snap, "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1")
        self.assertTrue(os.path.isfile(os.path.join(snap, "src", "app.js")))
        self.assertTrue(os.path.isfile(os.path.join(snap, ".env.example")))
        self.assertFalse(os.path.exists(os.path.join(snap, "node_modules")))

    def test_body_fields_cannot_set_or_clear_origin(self):
        tid = self.token_task(origin="", review={"state": "approved"}, restricted=False)
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertEqual(engine.get_review(tid)["origin"], "api:assistant-x")
        r = self.sign_in().post("/api/projects/%s/threads" % self.pid,
                                json={"title": "t", "request": "r", "origin": "api:fake",
                                      "review": {"state": "pending"}})
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(engine.get_review(r.get_json()["id"]))

    def test_signed_in_task_unaffected(self):
        r = self.sign_in().post("/api/projects/%s/threads" % self.pid,
                                json={"title": "t", "request": "r"})
        tid = r.get_json()["id"]
        self.assertIsNone(engine.get_review(tid))
        self.assertFalse(engine.project_held(self.pid))
        self.assertFalse(review.has_snapshot(self.pid))
        self.assertTrue(self.reply(tid))
        self.assertEqual(self.deploys, [(self.pid, tid)])
        self.assertEqual(engine._thread_status(tid), "done")

    def test_reply_on_held_project_never_deploys(self):
        rtid = self.token_task()
        ntid = engine.create_thread(engine.get_project(self.pid), "normal", "r", "admin")["thread_id"]
        self.assertFalse(self.reply(ntid))          # ordinary task, but the project is held
        self.assertFalse(self.reply(rtid))
        self.assertEqual(self.deploys, [])
        self.assertEqual(engine.get_review(rtid)["state"], "pending")
        self.assertTrue(engine.project_held(self.pid))
        self.assertEqual(engine._thread_status(rtid), "review")
        self.assertEqual(engine._thread_status(ntid), "done")
        d = self.sign_in().get("/api/threads/%d" % rtid).get_json()
        self.assertEqual(d["status"], "review")
        self.assertEqual(d["review"]["state"], "pending")
        self.assertEqual(d["review"]["origin"], "api:assistant-x")
        listed = {t["id"]: t["status"] for t in engine.list_threads(self.pid)}
        self.assertEqual(listed[rtid], "review")

    def test_stop_also_waits_for_review(self):
        tid = self.token_task()
        engine.enqueue_stop(engine.get_project(self.pid), tid)
        self.assertEqual(engine.get_review(tid)["state"], "pending")
        self.assertEqual(engine._thread_status(tid), "review")

    def test_followup_goes_back_to_running(self):
        tid = self.token_task()
        self.reply(tid)
        r = self.sign_in().post("/api/threads/%d/comment" % tid, json={"text": "more"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertTrue(engine.project_held(self.pid))
        self.assertNotEqual(engine._thread_status(tid), "review")

    def test_token_cannot_decide(self):
        tid = self.token_task()
        self.reply(tid)
        r = self.c.post("/api/threads/%d/review" % tid, headers=self.token_h(),
                        json={"decision": "approve"})
        self.assertEqual(r.status_code, 401)
        for profile, endpoints in wk.API_ENDPOINTS.items():
            self.assertNotIn("thread_review", endpoints, profile)
        r = self.sign_in("reviewmember").post("/api/threads/%d/review" % tid,
                                              json={"decision": "approve"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(engine.get_review(tid)["state"], "pending")

    def test_decision_only_when_pending(self):
        tid = self.token_task()
        c = self.sign_in()
        self.assertEqual(c.post("/api/threads/%d/review" % tid,
                                json={"decision": "approve"}).status_code, 409)
        self.reply(tid)
        self.assertEqual(c.post("/api/threads/%d/review" % tid,
                                json={"decision": "maybe"}).status_code, 400)

    def test_approve_deploys_and_clears_hold(self):
        tid = self.token_task()
        self.write("index.html", "v2")
        self.reply(tid)
        r = self.sign_in().post("/api/threads/%d/review" % tid, json={"decision": "approve"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["deploying"])
        self.wait_deploys()
        self.assertEqual(self.deploys, [(self.pid, tid)])
        rv = engine.get_review(tid)
        self.assertEqual(rv["state"], "approved")
        self.assertEqual(rv["decided_by"], "admin")
        self.assertTrue(rv["decided_at"])
        self.assertFalse(engine.project_held(self.pid))
        self.assertFalse(review.has_snapshot(self.pid))
        self.assertEqual(self.read("index.html"), "v2")
        self.assertEqual(engine._thread_status(tid), "done")

    def test_reject_restores_snapshot(self):
        tid = self.token_task()
        self.write("index.html", "evil")
        self.write("new.js", "added")
        shutil.rmtree(os.path.join(self.ws, "src"))
        self.reply(tid)
        r = self.sign_in().post("/api/threads/%d/review" % tid, json={"decision": "reject"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self.read("index.html"), "v1")
        self.assertEqual(self.read("src/app.js"), "one")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "new.js")))
        self.assertEqual(self.read("node_modules/dep/index.js"), "dep")
        self.assertEqual(engine.get_review(tid)["state"], "rejected")
        self.assertFalse(engine.project_held(self.pid))
        self.assertFalse(review.has_snapshot(self.pid))
        time.sleep(0.1)
        self.assertEqual(self.deploys, [])

    def test_publish_and_release_refused_while_held(self):
        tid = self.token_task()
        c = self.sign_in()
        for path in ("publish", "release"):
            r = c.post("/api/projects/%s/%s" % (self.pid, path), json={"version": "1.0.0"})
            self.assertEqual(r.status_code, 409, path)
            self.assertIn("awaiting review", r.get_json()["error"])
        self.reply(tid)
        self.assertEqual(c.post("/api/projects/%s/release" % self.pid,
                                json={"version": "1.0.0"}).status_code, 409)
        for path in ("publish", "release"):
            r = self.c.post("/api/projects/%s/%s" % (self.pid, path), headers=self.token_h(), json={})
            self.assertEqual(r.status_code, 401, path)

    def test_snapshot_failure_refuses_the_task(self):
        orig = review.ensure_snapshot

        def boom(pid):
            raise OSError("disk full")
        review.ensure_snapshot = boom
        try:
            r = self.c.post("/api/projects/%s/threads" % self.pid, headers=self.token_h(),
                            json={"title": "t", "request": "r"})
        finally:
            review.ensure_snapshot = orig
        self.assertEqual(r.status_code, 500)
        self.assertIsNone(db.query_one("SELECT id FROM threads WHERE project_id=?", (self.pid,)))
        self.assertFalse(engine.project_held(self.pid))

    def test_token_project_with_idea_is_restricted(self):
        r = self.c.post("/api/projects", headers=self.token_h(),
                        json={"name": "Token Idea App", "idea": "build it"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        pid = r.get_json()["project"]["id"]
        th = db.query_one("SELECT id FROM threads WHERE project_id=?", (pid,))
        self.assertEqual(engine.get_review(th["id"])["origin"], "api:assistant-x")
        self.assertTrue(engine.project_held(pid))
        self.assertTrue(review.has_snapshot(pid))


if __name__ == "__main__":
    unittest.main()
