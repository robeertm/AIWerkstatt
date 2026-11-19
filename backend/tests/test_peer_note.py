"""Service token profile "peer": a note from one open task to another open task.

Run: cd backend && python3 -m unittest discover -s tests
The backend modules are imported lazily in setUpClass (see test_coordinator_token.py).
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-peer-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-peer-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class PeerNote(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, service_token, wk
        import db  # noqa: F401
        import engine
        import store
        import service_token
        import app as wk
        cls.token = store.api_token_create("peer-test", "peer")
        cls.ctoken = store.api_token_create("peer-coord", "coordinator")
        cls.rtoken = store.api_token_create("peer-restricted")
        cls.c = wk.app.test_client()

    def setUp(self):
        wk.PEER_LIMIT.reset()
        wk.WRITE_LIMIT.reset()
        p = engine.create_project("Peer %s" % self.id().rsplit(".", 1)[-1][:16], "x", "d",
                                  "admin", provider="demo")
        self.pid = p["id"]
        self.a = self.task()
        self.b = self.task()

    def tearDown(self):
        for r in db.query_all("SELECT t.id FROM tasks t JOIN threads h ON h.id=t.thread_id "
                              "WHERE h.project_id=?", (self.pid,)):
            try:
                os.remove(os.path.join(engine.TASK_QUEUE, "%d.json" % r["id"]))
            except OSError:
                pass
            db.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=? "
                       "AND status NOT IN ('done','cancelled')", (store.now(), r["id"]))
        wk.PEER_LIMIT.reset()
        wk.WRITE_LIMIT.reset()
        store.api_token_set_disabled("peer-test", False)

    def h(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def task(self):
        r = self.c.post("/api/projects/%s/threads" % self.pid, headers=self.h(self.ctoken),
                        json={"title": "Change", "request": "Do it"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["id"]

    def note(self, to, token=None, **body):
        return self.c.post("/api/threads/%d/peer-note" % to, headers=self.h(token), json=body)

    def test_note_is_delivered_with_sender_prefix(self):
        r = self.note(self.b, **{"from": self.a, "text": "interface changed"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        ev = db.query_all("SELECT text, author FROM events WHERE thread_id=? AND type='user'", (self.b,))
        self.assertEqual(ev[-1]["text"], "↳ from #%d: interface changed" % self.a)
        row = db.query_one("SELECT kind, origin FROM tasks WHERE thread_id=? ORDER BY id DESC", (self.b,))
        self.assertEqual((row["kind"], row["origin"]), ("followup", "api:peer-test"))
        with open(service_token.PEER_LOG, encoding="utf-8") as f:
            last = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual((last["from"], last["to"], last["chars"]), (self.a, self.b, 17))
        self.assertNotIn("text", last)

    def test_validation(self):
        for body in ({}, {"from": self.a}, {"text": "x"}, {"from": "abc", "text": "x"},
                     {"from": True, "text": "x"}, {"from": self.a, "text": "  "},
                     {"from": self.a, "text": "x" * 4001}, {"from": self.a, "text": 5},
                     {"from": self.a, "text": "x", "files": ["f"]}):
            self.assertEqual(self.note(self.b, **body).status_code, 400, body)
        self.assertEqual(self.note(self.a, **{"from": self.a, "text": "me"}).status_code, 400)
        self.assertEqual(self.note(999999, **{"from": self.a, "text": "x"}).status_code, 404)
        self.assertEqual(self.note(self.b, **{"from": 999999, "text": "x"}).status_code, 404)

    def test_both_sides_must_be_open(self):
        db.execute("UPDATE tasks SET status='cancelled' WHERE thread_id=?", (self.b,))
        self.assertEqual(self.note(self.b, **{"from": self.a, "text": "x"}).status_code, 409)
        self.assertEqual(self.note(self.a, **{"from": self.b, "text": "x"}).status_code, 409)

    def test_restricted_target_refused(self):
        r = self.c.post("/api/projects/%s/threads" % self.pid, headers=self.h(self.rtoken),
                        json={"title": "R", "request": "Do it"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        self.assertEqual(self.note(r.get_json()["id"], **{"from": self.a, "text": "x"}).status_code, 409)

    def test_profile_stays_narrow(self):
        for method, path, body in (
                ("post", "/api/projects/%s/threads" % self.pid, {"title": "t", "request": "r"}),
                ("post", "/api/threads/%d/comment" % self.b, {"text": "x"}),
                ("get", "/api/threads/%d" % self.b, {}),
                ("get", "/api/projects", {}),
                ("post", "/api/coordinator/threads/%d/stop" % self.b, {"reason": "valid reason"}),
                ("post", "/api/threads/%d/review" % self.b, {"decision": "approve"})):
            r = getattr(self.c, method)(path, headers=self.h(), json=body)
            self.assertEqual(r.status_code, 401, "%s %s" % (method, path))

    def test_other_profiles_refused(self):
        self.assertEqual(self.note(self.b, token=self.rtoken, **{"from": self.a, "text": "x"}).status_code, 401)
        self.assertEqual(self.note(self.b, token="nope", **{"from": self.a, "text": "x"}).status_code, 401)
        self.assertEqual(self.c.post("/api/threads/%d/peer-note" % self.b,
                                     json={"from": self.a, "text": "x"}).status_code, 401)

    def test_coordinator_token_has_no_peer_route(self):
        r = self.note(self.b, token=self.ctoken, **{"from": self.a, "text": "x"})
        self.assertEqual(r.status_code, 401)

    def test_kill_switch(self):
        store.api_token_set_disabled("peer-test", True)
        self.assertEqual(self.note(self.b, **{"from": self.a, "text": "x"}).status_code, 401)

    def test_rate_limit_429(self):
        wk.PEER_LIMIT.limit = 2
        try:
            codes = [self.note(self.b, **{"from": self.a, "text": "n%d" % i}).status_code for i in range(3)]
        finally:
            wk.PEER_LIMIT.limit = service_token.peer_rate()
        self.assertEqual(codes, [201, 201, 429])

    def test_cli_creates_peer_profile(self):
        self.assertIn("peer", store.TOKEN_PROFILES)
        names = {t["name"]: t["profile"] for t in store.api_token_list()}
        self.assertEqual(names["peer-test"], "peer")


if __name__ == "__main__":
    unittest.main()
