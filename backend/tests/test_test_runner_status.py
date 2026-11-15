"""The optional `test_runner` field of /api/pool (admins only), fed by the collector file of scripts/test-runner.

Run: cd backend && python3 -m unittest tests.test_test_runner_status
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-trs-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-trs-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

ENV = "AIWERKSTATT_TEST_RUNNER_STATUS"


class TestRunnerStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global store, wk
        import store
        import app as wk
        cls.token = store.api_token_create("trs-test")      # a restricted service token: not an admin
        cls.c = wk.app.test_client()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aiw-trs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "status.json")
        self._old = os.environ.pop(ENV, None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop(ENV, None)
        if self._old is not None:
            os.environ[ENV] = self._old

    def write(self, **over):
        d = {"load": [3.4, 2.0, 1.0], "cores": 8, "psi_cpu": None, "psi_io": None, "ram_free_gb": 5.5,
             "cpu_percent": 37.0, "test_runs": 1, "runs": [{"id": "secret-run-id", "repo": "x"}], "max_runs": 2,
             "t": int(time.time()), "ok": True, "error": None, "stand": int(time.time())}
        d.update(over)
        with open(self.file, "w") as f:
            json.dump(d, f)

    def test_unset_means_no_field(self):
        self.assertIsNone(wk.test_runner_status())

    def test_missing_or_broken_file_means_no_field(self):
        os.environ[ENV] = self.file
        self.assertIsNone(wk.test_runner_status())
        with open(self.file, "w") as f:
            f.write("not json")
        self.assertIsNone(wk.test_runner_status())
        with open(self.file, "w") as f:
            f.write("[1]")
        self.assertIsNone(wk.test_runner_status())

    def test_fresh(self):
        os.environ[ENV] = self.file
        self.write()
        d = wk.test_runner_status()
        self.assertTrue(d["fresh"])
        self.assertEqual(d["cores"], 8)
        self.assertEqual(d["load"][0], 3.4)
        self.assertEqual(d["test_runs"], 1)
        self.assertEqual(d["max_runs"], 2)
        self.assertEqual(d["cpu_percent"], 37.0)
        self.assertIsInstance(d["stand"], int)
        self.assertNotIn("runs", d)                      # no run details, no ids
        self.assertNotIn("secret-run-id", json.dumps(d))

    def test_stale_and_failed(self):
        os.environ[ENV] = self.file
        self.write(stand=int(time.time()) - 121)
        self.assertFalse(wk.test_runner_status()["fresh"])
        self.write(stand=int(time.time()) - 60)
        self.assertTrue(wk.test_runner_status()["fresh"])
        self.write(ok=False)                             # collector could not reach the machine: last values, not fresh
        self.assertFalse(wk.test_runner_status()["fresh"])
        self.write(stand=None)
        d = wk.test_runner_status()
        self.assertFalse(d["fresh"])
        self.assertIsNone(d["stand"])
        self.write(stand=int(time.time()) + 3600)        # a clock in the future is not fresh
        self.assertFalse(wk.test_runner_status()["fresh"])

    def test_route_is_admin_only_and_additive(self):
        os.environ[ENV] = self.file
        self.write()
        self.assertEqual(self.c.get("/api/pool").status_code, 401)
        r = self.c.get("/api/pool", headers={"Authorization": "Bearer " + self.token})
        self.assertIn(r.status_code, (401, 403))
        self.assertNotIn(b"test_runner", r.data)
        admin = store.list_users()[0]["name"]
        with self.c.session_transaction() as s:
            s["user"] = admin
        r = self.c.get("/api/pool")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["test_runner"]["fresh"])
        for k in ("max", "busy", "waiting", "runs"):
            self.assertIn(k, body)
        os.environ.pop(ENV)
        self.assertNotIn("test_runner", self.c.get("/api/pool").get_json())


if __name__ == "__main__":
    unittest.main()
