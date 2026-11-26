"""AIWERKSTATT_UPDATE_CHECK=off: the automatic update check makes no request; the button (force=1) still can.

Run: cd backend && python3 -m unittest tests.test_update_check_switch
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-upd-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-upd-ws-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class UpdateCheckSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global store, wk, config
        import config
        import store
        import app as wk
        cls.c = wk.app.test_client()
        with cls.c.session_transaction() as s:
            s["user"] = store.list_users()[0]["name"]

    def setUp(self):
        wk._update_cache.update(at=0, data=None)

    def test_off_makes_no_request(self):
        with mock.patch.object(config, "UPDATE_CHECK", False), \
                mock.patch.object(wk.requests, "get", side_effect=AssertionError("no network")) as g:
            d = self.c.get("/api/update").get_json()
        self.assertFalse(g.called)
        self.assertFalse(d["update_available"])
        self.assertTrue(d["check_disabled"])

    def test_button_still_asks(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"tag_name": "v999.0.0", "html_url": "https://example.invalid/r"}
        with mock.patch.object(config, "UPDATE_CHECK", False), \
                mock.patch.object(wk.requests, "get", return_value=resp) as g:
            d = self.c.get("/api/update?force=1").get_json()
        self.assertTrue(g.called)
        self.assertTrue(d["update_available"])

    def test_default_is_on(self):
        self.assertTrue(config.UPDATE_CHECK)


if __name__ == "__main__":
    unittest.main()
