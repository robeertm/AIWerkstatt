"""Service token profiles: the "coordinator" profile next to the default "restricted" one.

Run: cd backend && python3 -m unittest discover -s tests
The backend modules are imported lazily in setUpClass, so whichever test module loads
first decides the shared data directory (see test_review_gate.py).
"""
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-coord-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-coord-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class CoordinatorToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, review, service_token, wk
        import db  # noqa: F401
        import engine
        import store
        import review
        import service_token
        import app as wk
        cls.token = store.api_token_create("coord-test", "coordinator")
        cls.rtoken = store.api_token_create("restricted-test")
        cls.c = wk.app.test_client()

    def setUp(self):
        self.deploys = []
        self._orig_deploy = wk.ORCH.deploy
        wk.ORCH.deploy = lambda pid, port, tid=None: self.deploys.append((pid, tid)) or True
        wk.WRITE_LIMIT.reset()
        p = engine.create_project("Coord %s" % self.id().rsplit(".", 1)[-1][:16], "x", "d",
                                  "admin", provider="demo")
        self.pid = p["id"]
        wk.API_BLOCKLIST.add(self.pid)          # every test project is blocklisted
        self.addCleanup(wk.API_BLOCKLIST.discard, self.pid)

    def tearDown(self):
        wk.ORCH.deploy = self._orig_deploy
        wk.WRITE_LIMIT.reset()
        store.api_token_set_disabled("coord-test", False)

    # ---- helpers ----
    def h(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def task(self, token=None, **extra):
        return self.c.post("/api/projects/%s/threads" % self.pid, headers=self.h(token),
                           json={"title": "Change", "request": "Do it", **extra})

    def audit_lines(self):
        try:
            with open(service_token.AUDIT_LOG, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    # ---- profiles in storage ----
    def test_profiles_stored_as_hash_only(self):
        rows = {r["key"]: r["value"] for r in
                db.query_all("SELECT key, value FROM secrets WHERE key LIKE 'api_token:%'")}
        self.assertTrue(all(self.token not in v and self.rtoken not in v for v in rows.values()))
        # a restricted token keeps the original format: the bare digest
        self.assertEqual(rows["api_token:restricted-test"], hashlib.sha256(self.rtoken.encode()).hexdigest())
        self.assertEqual(store.api_token_lookup(self.token)["profile"], "coordinator")
        self.assertEqual(store.api_token_lookup(self.rtoken)["profile"], "restricted")
        listed = {t["name"]: t for t in store.api_token_list()}
        self.assertEqual(listed["coord-test"]["profile"], "coordinator")
        self.assertNotIn("sha256", json.dumps(listed))

    def test_register_external_hash(self):
        plain = "an-externally-generated-token-0123456789"
        store.api_token_register("ext-coord", hashlib.sha256(plain.encode()).hexdigest(), "coordinator")
        try:
            self.assertEqual(store.api_token_lookup(plain)["name"], "ext-coord")
            self.assertEqual(self.c.get("/api/projects", headers=self.h(plain)).status_code, 200)
            with self.assertRaises(ValueError):
                store.api_token_register("ext-coord", "not-a-hash", "coordinator")
            with self.assertRaises(ValueError):
                store.api_token_register("ext coord", "0" * 64, "coordinator")
            with self.assertRaises(ValueError):
                store.api_token_register("ext-coord", "0" * 64, "superuser")
        finally:
            store.api_token_delete("ext-coord")
        self.assertIsNone(store.api_token_lookup(plain))

    def test_cli_register_reads_stdin_and_never_prints_it(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        plain = "cli-token-from-stdin-abcdefghijklmnop"
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(plain + "\n")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = service_token.main(["register", "cli-coord", "--profile", "coordinator"])
        finally:
            sys.stdin = old_stdin
        try:
            self.assertEqual(rc, 0, err.getvalue())
            self.assertNotIn(plain, out.getvalue() + err.getvalue())
            self.assertEqual(store.api_token_lookup(plain)["profile"], "coordinator")
            out = io.StringIO()
            with redirect_stdout(out):
                service_token.main(["list"])
            self.assertIn("cli-coord", out.getvalue())
            self.assertNotIn(hashlib.sha256(plain.encode()).hexdigest(), out.getvalue())
        finally:
            store.api_token_delete("cli-coord")

    # ---- coordinator rights ----
    def test_blocklisted_project_is_visible(self):
        r = self.c.get("/api/projects", headers=self.h())
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.pid, {p["id"] for p in r.get_json()["projects"]})
        self.assertEqual(self.c.get("/api/projects/%s" % self.pid, headers=self.h()).status_code, 200)
        r = self.task()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        tid = r.get_json()["id"]
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 200)
        # ... while the same project stays closed to a restricted token
        self.assertEqual(self.c.get("/api/projects/%s" % self.pid, headers=self.h(self.rtoken)).status_code, 403)
        self.assertEqual(self.task(self.rtoken).status_code, 403)

    def test_task_has_origin_and_no_review(self):
        tid = self.task(origin="api:someone-else", restricted=True).get_json()["id"]
        th = db.query_one("SELECT origin, origin_profile, review FROM threads WHERE id=?", (tid,))
        self.assertEqual(th["origin"], "api:coord-test")
        self.assertEqual(th["origin_profile"], "coordinator")
        self.assertIsNone(th["review"])
        task = db.query_one("SELECT origin FROM tasks WHERE thread_id=?", (tid,))
        self.assertEqual(task["origin"], "api:coord-test")
        self.assertIsNone(engine.get_review(tid))
        self.assertFalse(engine.project_held(self.pid))
        self.assertFalse(review.has_snapshot(self.pid))
        d = self.c.get("/api/threads/%d" % tid, headers=self.h()).get_json()
        self.assertEqual(d["origin"], "api:coord-test")
        self.assertIsNone(d["review"])
        listed = {t["id"]: t for t in engine.list_threads(self.pid)}
        self.assertEqual(listed[tid]["origin"], "api:coord-test")

    def test_reply_deploys_normally(self):
        tid = self.task().get_json()["id"]
        db.execute("UPDATE tasks SET status='done' WHERE thread_id=?", (tid,))
        self.assertTrue(wk._on_event(self.pid, tid, "reply"))
        self.assertEqual(self.deploys, [(self.pid, tid)])
        self.assertEqual(engine._thread_status(tid), "done")

    def test_followup_allowed_and_recorded(self):
        tid = engine.create_thread(engine.get_project(self.pid), "human", "r", "admin")["thread_id"]
        r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(), json={"text": "and also"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        rows = db.query_all("SELECT kind, origin FROM tasks WHERE thread_id=? ORDER BY id", (tid,))
        self.assertEqual([(x["kind"], x["origin"]) for x in rows],
                         [("new", None), ("followup", "api:coord-test")])
        self.assertIsNone(engine.get_review(tid))
        # a restricted token still cannot send follow-ups
        r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(self.rtoken), json={"text": "x"})
        self.assertEqual(r.status_code, 401)

    def test_followup_on_restricted_task_stays_restricted(self):
        wk.API_BLOCKLIST.discard(self.pid)
        r = self.task(self.rtoken)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        tid = r.get_json()["id"]
        engine.set_review(tid, "pending")
        r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(), json={"text": "more"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertTrue(engine.project_held(self.pid))
        review.drop_snapshot(self.pid)

    def test_model_choice_honoured(self):
        import automode
        import providers
        ids = [m.id for m in providers.get_spec("demo").models if m.id != automode.AUTO_MODEL]
        if ids:
            tid = self.task(model=ids[0]).get_json()["id"]
            self.assertEqual(db.query_one("SELECT req_model FROM tasks WHERE thread_id=?",
                                          (tid,))["req_model"], ids[0])
        tid = self.task(model="nope").get_json()["id"]   # invalid → silently ignored
        self.assertIsNone(db.query_one("SELECT req_model FROM tasks WHERE thread_id=?", (tid,))["req_model"])
        # a restricted token (member rights) cannot choose a model
        wk.API_BLOCKLIST.discard(self.pid)
        if ids:
            tid = self.task(self.rtoken, model=ids[0]).get_json()["id"]
            self.assertIsNone(db.query_one("SELECT req_model FROM tasks WHERE thread_id=?",
                                           (tid,))["req_model"])
            review.drop_snapshot(self.pid)

    def test_forbidden_routes_are_401(self):
        tid = self.task().get_json()["id"]
        for method, path, body in (
                ("post", "/api/projects/%s/release" % self.pid, {"version": "1.0.0"}),
                ("post", "/api/projects/%s/publish" % self.pid, {}),
                ("post", "/api/projects/%s/publish/scan" % self.pid, {}),
                ("post", "/api/threads/%d/review" % tid, {"decision": "approve"}),
                ("post", "/api/threads/%d/stop" % tid, {}),
                ("post", "/api/projects", {"name": "Nope"}),
                ("delete", "/api/projects/%s" % self.pid, {}),
                ("post", "/api/projects/%s/settings" % self.pid, {"model": "x"}),
                ("get", "/api/users", {}),
                ("post", "/api/users", {"name": "evil", "pin": "1234"}),
                ("delete", "/api/users/admin", {}),
                ("post", "/api/providers/claude/credentials", {"value": "x"}),
                ("post", "/api/github", {"token": "x"}),
                ("post", "/api/update/install", {}),
                ("post", "/api/me/pin", {"pin": "9999"})):
            r = getattr(self.c, method)(path, headers=self.h(), json=body)
            self.assertEqual(r.status_code, 401, "%s %s" % (method, path))
        self.assertTrue(engine.get_project(self.pid))
        self.assertIsNone(store.get_user("evil"))

    # ---- safety rails ----
    def test_write_rate_limit_429(self):
        wk.WRITE_LIMIT.limit = 3
        try:
            for _ in range(3):
                self.assertEqual(self.task().status_code, 201)
            r = self.task()
            self.assertEqual(r.status_code, 429)
            self.assertTrue(int(r.headers["Retry-After"]) > 0)
            tid = db.query_one("SELECT id FROM threads WHERE project_id=?", (self.pid,))["id"]
            r = self.c.post("/api/threads/%d/comment" % tid, headers=self.h(), json={"text": "x"})
            self.assertEqual(r.status_code, 429)
            self.assertEqual(db.query_one("SELECT COUNT(*) c FROM threads WHERE project_id=?",
                                          (self.pid,))["c"], 3)
            # reads are never limited
            self.assertEqual(self.c.get("/api/projects/%s" % self.pid, headers=self.h()).status_code, 200)
        finally:
            wk.WRITE_LIMIT.limit = service_token.WriteLimiter().limit

    def test_default_write_limit_is_30_per_hour(self):
        old = os.environ.pop("AIWERKSTATT_API_WRITE_LIMIT", None)
        try:
            lim = service_token.WriteLimiter()
            self.assertEqual(lim.limit, 30)
            self.assertEqual(lim.WINDOW, 3600)
            self.assertTrue(all(lim.allow("x", now=1000)[0] for _ in range(30)))
            self.assertFalse(lim.allow("x", now=1000)[0])
            self.assertTrue(lim.allow("x", now=1000 + 3601)[0])
        finally:
            if old is not None:
                os.environ["AIWERKSTATT_API_WRITE_LIMIT"] = old

    def test_kill_switch_401_without_restart(self):
        self.assertEqual(self.c.get("/api/projects", headers=self.h()).status_code, 200)
        os.makedirs(store.DISABLED_DIR, exist_ok=True)
        flag = os.path.join(store.DISABLED_DIR, "coord-test")
        with open(flag, "w"):
            pass
        self.assertEqual(self.c.get("/api/projects", headers=self.h()).status_code, 401)
        self.assertEqual(self.task().status_code, 401)
        self.assertIsNone(store.api_token_name(self.token))
        os.remove(flag)
        self.assertEqual(self.c.get("/api/projects", headers=self.h()).status_code, 200)
        store.api_token_set_disabled("coord-test", True)
        self.assertEqual(self.c.get("/api/projects", headers=self.h()).status_code, 401)

    def test_audit_logs_every_use_without_the_token(self):
        before = len(self.audit_lines())
        tid = self.task().get_json()["id"]
        self.c.post("/api/projects/%s/release" % self.pid, headers=self.h(), json={})
        self.c.get("/api/threads/%d" % tid, headers=self.h())
        self.c.get("/api/projects", headers=self.h("not-a-token"))   # unknown: not audited
        new = self.audit_lines()[before:]
        self.assertEqual([(e["method"], e["route"], e["status"]) for e in new],
                         [("POST", "/api/projects/<pid>/threads", 201),
                          ("POST", "/api/projects/<pid>/release", 401),
                          ("GET", "/api/threads/<int:tid>", 200)])
        for e in new:
            self.assertEqual(e["token"], "coord-test")
            self.assertEqual(e["profile"], "coordinator")
            self.assertEqual(e["project"], self.pid)
            self.assertTrue(e["at"])
        with open(service_token.AUDIT_LOG, encoding="utf-8") as f:
            raw = f.read()
        for secret in (self.token, self.rtoken, hashlib.sha256(self.token.encode()).hexdigest()):
            self.assertNotIn(secret, raw)
        self.assertNotIn("Bearer", raw)

    # ---- restricted profile unchanged ----
    def test_restricted_token_unchanged(self):
        wk.API_BLOCKLIST.discard(self.pid)
        r = self.task(self.rtoken)
        self.assertEqual(r.status_code, 201)
        tid = r.get_json()["id"]
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertEqual(engine.get_review(tid)["origin"], "api:restricted-test")
        self.assertTrue(engine.project_held(self.pid))
        wk.WRITE_LIMIT.limit = 1
        try:   # the write limit is a coordinator rail; restricted tokens behave as before
            self.assertEqual(self.task(self.rtoken).status_code, 201)
        finally:
            wk.WRITE_LIMIT.limit = service_token.WriteLimiter().limit
        review.drop_snapshot(self.pid)

    def test_legacy_thread_without_profile_stays_restricted(self):
        tid = engine.create_thread(engine.get_project(self.pid), "old", "r", "Assistant",
                                   origin="api:assistant")["thread_id"]
        db.execute("UPDATE threads SET origin_profile=NULL WHERE id=?", (tid,))
        self.assertEqual(engine.get_review(tid)["state"], "running")
        self.assertTrue(engine.project_held(self.pid))


if __name__ == "__main__":
    unittest.main()
