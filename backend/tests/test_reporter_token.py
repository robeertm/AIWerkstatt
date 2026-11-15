"""Service token profile "reporter": files tasks into one intake project, sees only its own.

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-rep-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-rep-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class ReporterToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, wk
        import db  # noqa: F401
        import engine
        import store
        import app as wk
        cls.token = store.api_token_create("reporter-a", "reporter")
        cls.token_b = store.api_token_create("reporter-b", "reporter")
        cls.c = wk.app.test_client()

    def setUp(self):
        self._orig_deploy = wk.ORCH.deploy
        wk.ORCH.deploy = lambda pid, port, tid=None: True
        self._orig_limit = wk.REPORTER_LIMIT.limit
        wk.REPORTER_LIMIT.limit = 8
        wk.REPORTER_LIMIT.reset()
        name = "Intake %s" % self.id().rsplit(".", 1)[-1][:16]
        self.pid = engine.create_project(name, "x", "d", "admin", provider="demo")["id"]
        self.other = engine.create_project("Other " + name, "x", "d", "admin", provider="demo")["id"]
        os.environ["AIWERKSTATT_REPORTER_PROJECT"] = self.pid

    def tearDown(self):
        # leave no open task behind: other test modules share this data directory
        for pid in (self.pid, self.other):
            for r in db.query_all("SELECT t.id FROM tasks t JOIN threads h ON h.id=t.thread_id "
                                  "WHERE h.project_id=?", (pid,)):
                try:
                    os.remove(os.path.join(engine.TASK_QUEUE, "%d.json" % r["id"]))
                except OSError:
                    pass
                db.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=? "
                           "AND status IN ('queued','running','retrying','waiting')", (store.now(), r["id"]))
        wk.ORCH.deploy = self._orig_deploy
        wk.REPORTER_LIMIT.limit = self._orig_limit
        wk.REPORTER_LIMIT.reset()
        os.environ.pop("AIWERKSTATT_REPORTER_PROJECT", None)
        store.api_token_set_disabled("reporter-a", False)

    def h(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def task(self, pid=None, token=None):
        return self.c.post("/api/projects/%s/threads" % (pid or self.pid), headers=self.h(token),
                           json={"title": "Report", "request": "Something is off"})

    def test_profile_stored(self):
        self.assertEqual(store.api_token_lookup(self.token)["profile"], "reporter")

    def test_create_in_intake_project_is_restricted(self):
        r = self.task()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        th = db.query_one("SELECT origin, origin_profile, review FROM threads WHERE id=?",
                          (r.get_json()["id"],))
        self.assertEqual(th["origin"], "api:reporter-a")
        self.assertEqual(th["origin_profile"], "restricted")
        self.assertIsNotNone(th["review"])           # review gate stays active
        self.assertIsNotNone(engine.get_review(r.get_json()["id"]))

    def test_other_project_and_unset_variable_refused(self):
        self.assertEqual(self.task(self.other).status_code, 403)
        os.environ.pop("AIWERKSTATT_REPORTER_PROJECT")
        self.assertEqual(self.task().status_code, 403)
        os.environ["AIWERKSTATT_REPORTER_PROJECT"] = ""
        self.assertEqual(self.task().status_code, 403)

    def test_read_and_follow_up_own_task_only(self):
        tid = self.task().get_json()["id"]
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 200)
        r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(), json={"text": "More"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertIsNotNone(engine.get_review(tid))  # follow-up stays restricted
        # another reporter, and a task not created by any token
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h(self.token_b)).status_code, 403)
        self.assertEqual(self.c.post("/api/threads/%d/comment" % tid, headers=self.h(self.token_b),
                                     json={"text": "x"}).status_code, 403)
        own = engine.create_thread(engine.get_project(self.pid), "Own", "body", "admin")["thread_id"]
        self.assertEqual(self.c.get("/api/threads/%d" % own, headers=self.h()).status_code, 403)
        self.assertEqual(self.c.post("/api/threads/%d/comment" % own, headers=self.h(),
                                     json={"text": "x"}).status_code, 403)
        self.assertEqual(self.c.get("/api/threads/999999", headers=self.h()).status_code, 403)

    def test_other_routes_are_401(self):
        t = self.task().get_json()["id"]
        for method, path in [("get", "/api/projects"), ("get", "/api/projects/%s" % self.pid),
                             ("post", "/api/projects"), ("post", "/api/threads/%d/review" % t),
                             ("post", "/api/threads/%d/stop" % t), ("get", "/api/threads/%d/live" % t),
                             ("get", "/api/projects/%s/files" % self.pid),
                             ("post", "/api/projects/%s/publish" % self.pid),
                             ("post", "/api/projects/%s/release" % self.pid),
                             ("get", "/api/users"), ("get", "/api/pool")]:
            r = getattr(self.c, method)(path, headers=self.h(), json={})
            self.assertEqual(r.status_code, 401, (method, path, r.status_code))

    def test_rate_limit(self):
        wk.REPORTER_LIMIT.limit = 2
        self.assertEqual(self.task().status_code, 201)
        self.assertEqual(self.task().status_code, 201)
        r = self.task()
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.task(token=self.token_b).status_code, 201)   # per token

    def test_rate_default_and_junk(self):
        import service_token
        old = os.environ.pop("AIWERKSTATT_REPORTER_RATE", None)
        try:
            self.assertEqual(service_token.reporter_rate(), 8)
            for v, want in (("3", 3), ("0", 8), ("-1", 8), ("x", 8)):
                os.environ["AIWERKSTATT_REPORTER_RATE"] = v
                self.assertEqual(service_token.reporter_rate(), want)
        finally:
            os.environ.pop("AIWERKSTATT_REPORTER_RATE", None)
            if old is not None:
                os.environ["AIWERKSTATT_REPORTER_RATE"] = old

    def test_kill_switch(self):
        tid = self.task().get_json()["id"]
        store.api_token_set_disabled("reporter-a", True)
        self.assertEqual(self.task().status_code, 401)
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 401)
        self.assertEqual(self.c.post("/api/threads/%d/comment" % tid, headers=self.h(),
                                     json={"text": "x"}).status_code, 401)

    def test_no_review_bypass_by_body(self):
        r = self.c.post("/api/projects/%s/threads" % self.pid, headers=self.h(),
                        json={"title": "T", "request": "R", "origin": "api:other",
                              "restricted": False, "profile": "coordinator"})
        th = db.query_one("SELECT origin, origin_profile FROM threads WHERE id=?", (r.get_json()["id"],))
        self.assertEqual((th["origin"], th["origin_profile"]), ("api:reporter-a", "restricted"))


if __name__ == "__main__":
    unittest.main()
