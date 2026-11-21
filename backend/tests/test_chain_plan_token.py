"""Service token profile "chain", multi-project plan scope: the token may also file into listed plan projects with
the plan title prefix; an excluded project is never reachable; the collecting project keeps its own prefix.

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-chainplan-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-chainplan-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CHAIN_PREFIX = "[chain] "
PLAN_PREFIX = "Project plan · "
ENV = ("AIWERKSTATT_CHAIN_PROJECT", "AIWERKSTATT_CHAIN_TITLE_PREFIX", "AIWERKSTATT_CHAIN_PLAN_PROJECTS",
       "AIWERKSTATT_CHAIN_PLAN_PREFIX", "AIWERKSTATT_CHAIN_PLAN_EXCLUDED")


class ChainPlanToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, wk
        import db  # noqa: F401
        import engine
        import store
        import app as wk
        cls.token = store.api_token_create("plan-a", "chain")
        cls.other_tok = store.api_token_create("plan-b", "chain")
        cls.c = wk.app.test_client()
        # Projects once per class: each project takes a port from the shared range (8100-8199); five per test case
        # exhausted it in the combined `unittest discover` run.
        mk = lambda name: engine.create_project(name + " plan-token", "x", "d", "admin", provider="demo")["id"]  # noqa: E731
        cls.coll, cls.pa, cls.pb, cls.ex, cls.out = mk("Coll"), mk("PlanA"), mk("PlanB"), mk("Excl"), mk("Outside")

    def setUp(self):
        self._orig_deploy = wk.ORCH.deploy
        wk.ORCH.deploy = lambda pid, port, tid=None: True
        self._orig_limit = wk.CHAIN_LIMIT.limit
        wk.CHAIN_LIMIT.limit = 50
        wk.CHAIN_LIMIT.reset()
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = self.coll
        os.environ["AIWERKSTATT_CHAIN_TITLE_PREFIX"] = CHAIN_PREFIX
        os.environ["AIWERKSTATT_CHAIN_PLAN_PROJECTS"] = "%s, %s, %s" % (self.pa, self.pb, self.ex)
        os.environ["AIWERKSTATT_CHAIN_PLAN_PREFIX"] = PLAN_PREFIX
        os.environ["AIWERKSTATT_CHAIN_PLAN_EXCLUDED"] = self.ex.upper()

    def tearDown(self):
        for pid in (self.coll, self.pa, self.pb, self.ex, self.out):
            for r in db.query_all("SELECT t.id FROM tasks t JOIN threads h ON h.id=t.thread_id WHERE h.project_id=?", (pid,)):
                try:
                    os.remove(os.path.join(engine.TASK_QUEUE, "%d.json" % r["id"]))
                except OSError:
                    pass
                db.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=? "
                           "AND status IN ('queued','running','retrying','waiting')", (store.now(), r["id"]))
        wk.ORCH.deploy = self._orig_deploy
        wk.CHAIN_LIMIT.limit = self._orig_limit
        wk.CHAIN_LIMIT.reset()
        for k in ENV:
            os.environ.pop(k, None)

    def post(self, pid, title, token=None, **extra):
        body = {"title": title, "request": "Do it"}
        body.update(extra)
        return self.c.post("/api/projects/%s/threads" % pid,
                           headers={"Authorization": "Bearer " + (token or self.token)}, json=body)

    def test_plan_project_with_plan_prefix_is_allowed(self):
        for pid in (self.pa, self.pb):
            r = self.post(pid, PLAN_PREFIX + "x A1: step")
            self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        th = db.query_one("SELECT origin, project_id FROM threads ORDER BY id DESC LIMIT 1")
        self.assertEqual(th["origin"], "api:plan-a")        # still bound to the token

    def test_plan_project_needs_the_plan_prefix(self):
        self.assertEqual(self.post(self.pa, CHAIN_PREFIX + "step").status_code, 403)
        self.assertEqual(self.post(self.pa, "step").status_code, 403)
        self.assertEqual(self.post(self.pa, "x " + PLAN_PREFIX).status_code, 403)

    def test_collecting_project_keeps_the_chain_prefix(self):
        self.assertEqual(self.post(self.coll, CHAIN_PREFIX + "step").status_code, 201)
        self.assertEqual(self.post(self.coll, PLAN_PREFIX + "step").status_code, 403)

    def test_unlisted_project_is_refused(self):
        self.assertEqual(self.post(self.out, PLAN_PREFIX + "step").status_code, 403)
        self.assertEqual(self.post(self.out, CHAIN_PREFIX + "step").status_code, 403)

    def test_excluded_project_is_never_reachable(self):
        self.assertEqual(self.post(self.ex, PLAN_PREFIX + "step").status_code, 403)
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = self.ex            # not even as the collecting project
        self.assertEqual(self.post(self.ex, CHAIN_PREFIX + "step").status_code, 403)

    def test_unset_project_list_or_prefix_means_nowhere(self):
        os.environ["AIWERKSTATT_CHAIN_PLAN_PREFIX"] = ""
        self.assertEqual(self.post(self.pa, PLAN_PREFIX + "step").status_code, 403)
        os.environ.pop("AIWERKSTATT_CHAIN_PLAN_PREFIX")
        self.assertEqual(self.post(self.pa, PLAN_PREFIX + "step").status_code, 403)
        os.environ["AIWERKSTATT_CHAIN_PLAN_PREFIX"] = PLAN_PREFIX
        os.environ["AIWERKSTATT_CHAIN_PLAN_PROJECTS"] = ""
        self.assertEqual(self.post(self.pa, PLAN_PREFIX + "step").status_code, 403)

    def test_plan_scope_without_collecting_project(self):
        os.environ["AIWERKSTATT_CHAIN_PROJECT"] = ""
        self.assertEqual(self.post(self.pa, PLAN_PREFIX + "step").status_code, 201)
        self.assertEqual(self.post(self.coll, CHAIN_PREFIX + "step").status_code, 403)

    def test_models_efforts_and_attachments_still_limited(self):
        t = PLAN_PREFIX + "step"
        self.assertEqual(self.post(self.pa, t, model="demo").status_code, 201)
        self.assertEqual(self.post(self.pa, t, model="some-special-model").status_code, 403)
        self.assertEqual(self.post(self.pa, t, effort="ludicrous").status_code, 403)
        self.assertEqual(self.post(self.pa, t, files=[{"name": "a", "data": "b"}]).status_code, 403)

    def test_read_own_tasks_only(self):
        tid = self.post(self.pb, PLAN_PREFIX + "step").get_json()["id"]
        h = lambda tok: {"Authorization": "Bearer " + tok}  # noqa: E731
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=h(self.token)).status_code, 200)
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=h(self.other_tok)).status_code, 403)

    def test_other_profiles_are_not_widened(self):
        rep = store.api_token_create("plan-rep-" + self.id().rsplit(".", 1)[-1][:8], "reporter")
        os.environ["AIWERKSTATT_REPORTER_PROJECT"] = self.coll
        try:
            self.assertEqual(self.post(self.pa, PLAN_PREFIX + "step", token=rep).status_code, 403)
        finally:
            os.environ.pop("AIWERKSTATT_REPORTER_PROJECT", None)


if __name__ == "__main__":
    unittest.main()
