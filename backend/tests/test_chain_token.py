"""Service token profile "chain": the automatic task chain files tasks into one collecting
project, with a fixed title prefix and standard models only, and sees only its own tasks.

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-chain-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-chain-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PREFIX = "[chain] "


class ChainToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, wk
        import db  # noqa: F401
        import engine
        import store
        import app as wk
        cls.token = store.api_token_create("chain-a", "chain")
        cls.token_b = store.api_token_create("chain-b", "chain")
        cls.rep = store.api_token_create("rep-x", "reporter")
        cls.c = wk.app.test_client()

    def setUp(self):
        self._orig_deploy = wk.ORCH.deploy
        wk.ORCH.deploy = lambda pid, port, tid=None: True
        self._orig_limit = wk.CHAIN_LIMIT.limit
        wk.CHAIN_LIMIT.limit = 6
        wk.CHAIN_LIMIT.reset()
        name = "Chain %s" % self.id().rsplit(".", 1)[-1][:16]
        self.pid = engine.create_project(name, "x", "d", "admin", provider="demo")["id"]
        self.other = engine.create_project("Other " + name, "x", "d", "admin", provider="demo")["id"]
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = self.pid
        os.environ["AIWERKSTATT_CHAIN_TITLE_PREFIX"] = PREFIX

    def tearDown(self):
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
        wk.CHAIN_LIMIT.limit = self._orig_limit
        wk.CHAIN_LIMIT.reset()
        for k in ("AIWERKSTATT_CHAIN_PROJECT", "AIWERKSTATT_CHAIN_TITLE_PREFIX",
                  "AIWERKSTATT_CHAIN_REVIEW"):
            os.environ.pop(k, None)
        store.api_token_set_disabled("chain-a", False)

    def h(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def task(self, pid=None, token=None, **extra):
        body = {"title": PREFIX + "step 1", "request": "Do the step"}
        body.update(extra)
        return self.c.post("/api/projects/%s/threads" % (pid or self.pid), headers=self.h(token),
                           json=body)

    def test_profile_stored(self):
        self.assertEqual(store.api_token_lookup(self.token)["profile"], "chain")

    def test_create_is_restricted_by_default(self):
        r = self.task()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        th = db.query_one("SELECT origin, origin_profile, review FROM threads WHERE id=?",
                          (r.get_json()["id"],))
        self.assertEqual(th["origin"], "api:chain-a")
        self.assertEqual(th["origin_profile"], "restricted")
        self.assertIsNotNone(th["review"])

    def test_review_off_runs_like_coordinator(self):
        os.environ["AIWERKSTATT_CHAIN_REVIEW"] = "off"
        r = self.task()
        self.assertEqual(r.status_code, 201)
        th = db.query_one("SELECT origin_profile, review FROM threads WHERE id=?", (r.get_json()["id"],))
        self.assertEqual(th["origin_profile"], "coordinator")
        self.assertIsNone(th["review"])

    def test_other_project_and_unset_variable_refused(self):
        self.assertEqual(self.task(self.other).status_code, 403)
        os.environ.pop("AIWERKSTATT_CHAIN_PROJECT")
        self.assertEqual(self.task().status_code, 403)
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = ""
        self.assertEqual(self.task().status_code, 403)

    def test_title_prefix_enforced(self):
        self.assertEqual(self.task(title="no prefix").status_code, 403)
        self.assertEqual(self.task(title="x " + PREFIX).status_code, 403)
        os.environ.pop("AIWERKSTATT_CHAIN_TITLE_PREFIX")
        self.assertEqual(self.task().status_code, 403)
        os.environ["AIWERKSTATT_CHAIN_TITLE_PREFIX"] = ""
        self.assertEqual(self.task().status_code, 403)

    def test_models_and_efforts(self):
        self.assertEqual(self.task(model="demo").status_code, 201)
        self.assertEqual(self.task(model="auto").status_code, 201)
        self.assertEqual(self.task(model="some-special-model").status_code, 403)
        self.assertEqual(self.task(model=5).status_code, 403)
        self.assertEqual(self.task(effort="ludicrous").status_code, 403)

    def test_model_standard_list_for_claude(self):
        p = {"provider": "claude"}
        ok = lambda m: wk._chain_request_ok(p, {"model": m}, PREFIX + "t")  # noqa: E731
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = "p"
        for m in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "auto", ""):
            self.assertTrue(ok(m), m)
        for m in ("claude-fable-5-1", "claude-opus-4-8", "claude-sonnet-5", "nope"):
            self.assertFalse(ok(m), m)
        self.assertTrue(wk._chain_request_ok(p, {"effort": "max"}, PREFIX + "t"))
        self.assertFalse(wk._chain_request_ok(p, {"effort": "extreme"}, PREFIX + "t"))

    def test_no_attachments(self):
        for k in ("files", "attachments", "attachment"):
            self.assertEqual(self.task(**{k: [{"name": "a", "data": "b"}]}).status_code, 403, k)

    def test_read_and_follow_up_own_task_only(self):
        tid = self.task().get_json()["id"]
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 200)
        r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(), json={"text": "More"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertIsNotNone(engine.get_review(tid))
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h(self.token_b)).status_code, 403)
        self.assertEqual(self.c.post("/api/threads/%d/comment" % tid, headers=self.h(self.token_b),
                                     json={"text": "x"}).status_code, 403)
        # a task of a reporter token and a task of a person are not the chain's
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h(self.rep)).status_code, 403)
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
        wk.CHAIN_LIMIT.limit = 2
        self.assertEqual(self.task().status_code, 201)
        self.assertEqual(self.task().status_code, 201)
        r = self.task()
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.task(token=self.token_b).status_code, 201)   # per token

    def test_rate_default_and_junk(self):
        import service_token
        old = os.environ.pop("AIWERKSTATT_CHAIN_RATE", None)
        try:
            self.assertEqual(service_token.chain_rate(), 6)
            for v, want in (("3", 3), ("0", 6), ("-1", 6), ("x", 6)):
                os.environ["AIWERKSTATT_CHAIN_RATE"] = v
                self.assertEqual(service_token.chain_rate(), want)
        finally:
            os.environ.pop("AIWERKSTATT_CHAIN_RATE", None)
            if old is not None:
                os.environ["AIWERKSTATT_CHAIN_RATE"] = old

    def test_kill_switch(self):
        tid = self.task().get_json()["id"]
        store.api_token_set_disabled("chain-a", True)
        self.assertEqual(self.task().status_code, 401)
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 401)
        self.assertEqual(self.c.post("/api/threads/%d/comment" % tid, headers=self.h(),
                                     json={"text": "x"}).status_code, 401)

    def test_no_review_bypass_by_body(self):
        r = self.task(origin="api:other", restricted=False, profile="coordinator")
        th = db.query_one("SELECT origin, origin_profile FROM threads WHERE id=?", (r.get_json()["id"],))
        self.assertEqual((th["origin"], th["origin_profile"]), ("api:chain-a", "restricted"))


if __name__ == "__main__":
    unittest.main()
