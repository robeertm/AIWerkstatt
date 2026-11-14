"""Optional service access via bearer token: narrow, with a blocklist.

Run: cd backend && python3 -m unittest discover -s tests
(the test points AIWERKSTATT_DATA at a throwaway directory itself)."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="aiw-test-")
os.environ["AIWERKSTATT_DATA"] = _TMP
os.environ["AIWERKSTATT_API_BLOCKLIST"] = "secret-app"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import engine  # noqa: E402
import store  # noqa: E402
import app as wk  # noqa: E402


class ServiceToken(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = store.api_token_create("assistant")
        cls.open_p = engine.create_project("Open App", "x", "d", "admin", provider="demo")
        cls.blocked = engine.create_project("Secret App", "x", "d", "admin", provider="demo")
        assert cls.blocked["id"] == "secret-app"
        cls.c = wk.app.test_client()

    @classmethod
    def tearDownClass(cls):
        try:
            db.conn().close()
        except sqlite3.Error:
            pass
        db._conn = None     # the connection is process-wide: later test modules must open a fresh one
        shutil.rmtree(_TMP, ignore_errors=True)

    def h(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def test_token_stored_only_as_hash(self):
        rows = db.query_all("SELECT value FROM secrets WHERE key LIKE 'api_token:%'")
        self.assertTrue(rows)
        self.assertTrue(all(self.token not in r["value"] for r in rows))
        self.assertEqual(store.api_token_name(self.token), "assistant")
        self.assertIsNone(store.api_token_name("wrong"))

    def test_no_or_wrong_token_is_401(self):
        self.assertEqual(self.c.get("/api/projects").status_code, 401)
        self.assertEqual(self.c.get("/api/projects", headers=self.h("wrong")).status_code, 401)

    def test_list_hides_blocklisted(self):
        r = self.c.get("/api/projects", headers=self.h())
        self.assertEqual(r.status_code, 200)
        ids = {p["id"] for p in r.get_json()["projects"]}
        self.assertIn("open-app", ids)
        self.assertNotIn("secret-app", ids)

    def test_blocklisted_project_is_403(self):
        self.assertEqual(self.c.get("/api/projects/secret-app", headers=self.h()).status_code, 403)
        r = self.c.post("/api/projects/secret-app/threads", headers=self.h(),
                        json={"title": "t", "request": "r"})
        self.assertEqual(r.status_code, 403)

    def test_create_and_read_task(self):
        r = self.c.post("/api/projects/open-app/threads", headers=self.h(),
                        json={"title": "Title", "request": "Do it"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        tid = r.get_json()["id"]
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 200)
        self.assertEqual(self.c.get("/api/projects/open-app", headers=self.h()).status_code, 200)

    def test_task_of_blocklisted_project_is_403(self):
        tid = engine.create_thread(self.blocked, "t", "r", "admin")["thread_id"]
        self.assertEqual(self.c.get("/api/threads/%d" % tid, headers=self.h()).status_code, 403)

    def test_create_project(self):
        r = self.c.post("/api/projects", headers=self.h(), json={"name": "From Token"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))

    def test_other_routes_stay_closed(self):
        for method, path in (("post", "/api/projects/open-app/release"),
                             ("post", "/api/projects/open-app/publish"),
                             ("delete", "/api/projects/open-app"),
                             ("get", "/api/users"),
                             ("post", "/api/projects/open-app/settings"),
                             ("post", "/api/threads/1/comment"),
                             ("post", "/api/threads/1/stop")):
            r = getattr(self.c, method)(path, headers=self.h(), json={})
            self.assertEqual(r.status_code, 401, path)


if __name__ == "__main__":
    unittest.main()
