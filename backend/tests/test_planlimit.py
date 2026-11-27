"""Plan-usage connection (planlimit.py): PKCE connect, token refresh, cached status — all against
fakes, no network. The usage endpoint is throttled, so caching and 'keep the last good value on a
hiccup' are behaviour that must not regress.

Run: cd backend && python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

os.environ.setdefault("AIWERKSTATT_DATA", tempfile.mkdtemp(prefix="aiw-test-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import planlimit  # noqa: E402


class FakeStore:
    def __init__(self):
        self.d = {}

    def get_secret(self, k, default=None):
        return self.d.get(k, default)

    def set_secret(self, k, v):
        self.d[k] = v

    def delete_secret(self, k):
        self.d.pop(k, None)


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


class Base(unittest.TestCase):
    def setUp(self):
        self.st = FakeStore()
        for p in (mock.patch.object(planlimit, "store", self.st), mock.patch.dict(planlimit._cache, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def token(self, expires_in=3600, user="u"):
        self.st.d[planlimit._TOK_KEY % user] = {"access": "A1", "refresh": "R1", "expires_at": 4_000_000_000 if expires_in else 0}


USAGE = {"five_hour": {"utilization": 12, "resets_at": "t1"}, "seven_day": {"utilization": 40, "resets_at": "t2"}}


class Connect(Base):
    def test_start_connect_url_and_pkce_pair(self):
        url = planlimit.start_connect("u")
        self.assertTrue(url.startswith(planlimit._AUTHORIZE + "?"))
        pk = self.st.d[planlimit._PKCE_KEY % "u"]
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("state=" + pk["state"], url)
        self.assertNotIn(pk["verifier"], url)              # only the hash goes out
        self.assertGreaterEqual(len(pk["verifier"]), 43)

    def test_finish_without_pending_or_empty_or_wrong_state(self):
        self.assertFalse(planlimit.finish_connect("u", "code#state")[0])
        planlimit.start_connect("u")
        self.assertFalse(planlimit.finish_connect("u", "")[0])
        ok, msg = planlimit.finish_connect("u", "code#wrong")
        self.assertFalse(ok)
        self.assertIn("State mismatch", msg)
        self.assertIn(planlimit._PKCE_KEY % "u", self.st.d)           # still pending

    def test_finish_stores_tokens_and_clears_pkce(self):
        planlimit.start_connect("u")
        state = self.st.d[planlimit._PKCE_KEY % "u"]["state"]
        with mock.patch.object(planlimit, "_post",
                               return_value={"access_token": "A", "refresh_token": "R", "expires_in": 100}) as post:
            ok, err = planlimit.finish_connect("u", "thecode#" + state)
        self.assertEqual((ok, err), (True, None))
        self.assertEqual(post.call_args.args[1]["code"], "thecode")
        self.assertTrue(planlimit.is_connected("u"))
        self.assertNotIn(planlimit._PKCE_KEY % "u", self.st.d)
        self.assertEqual(self.st.d[planlimit._TOK_KEY % "u"]["refresh"], "R")

    def test_finish_failures_do_not_store(self):
        planlimit.start_connect("u")
        with mock.patch.object(planlimit, "_post", side_effect=OSError("down")):
            self.assertFalse(planlimit.finish_connect("u", "c")[0])
        with mock.patch.object(planlimit, "_post", return_value={}):
            self.assertEqual(planlimit.finish_connect("u", "c"), (False, "No token returned."))
        self.assertFalse(planlimit.is_connected("u"))

    def test_disconnect_removes_token_and_cache(self):
        self.token()
        planlimit._cache["u"] = (1, {})
        planlimit.disconnect("u")
        self.assertFalse(planlimit.is_connected("u"))
        self.assertNotIn("u", planlimit._cache)
        self.assertFalse(planlimit.is_connected(None))


class Status(Base):
    def test_not_connected(self):
        self.assertEqual(planlimit.get_status(None), {"connected": False})
        self.assertEqual(planlimit.get_status("u"), {"connected": False})

    def test_fetches_then_serves_from_cache(self):
        self.token()
        with mock.patch.object(planlimit, "_fetch_usage", return_value=USAGE) as f:
            s = planlimit.get_status("u")
            planlimit.get_status("u")
        self.assertEqual(f.call_count, 1)
        self.assertEqual(s["session"], {"pct": 12, "resets_at": "t1"})
        self.assertEqual(s["weekly"]["pct"], 40)

    def test_cache_expires(self):
        self.token()
        with mock.patch.object(planlimit, "_fetch_usage", return_value=USAGE) as f:
            planlimit.get_status("u")
            t, st = planlimit._cache["u"]
            planlimit._cache["u"] = (t - planlimit._TTL - 1, st)
            planlimit.get_status("u")
        self.assertEqual(f.call_count, 2)

    def test_expired_token_is_refreshed_first(self):
        self.token(expires_in=0)
        with mock.patch.object(planlimit, "_post", return_value={"access_token": "A2", "expires_in": 3600}) as post, \
                mock.patch.object(planlimit, "_fetch_usage", return_value=USAGE) as f:
            planlimit.get_status("u")
        self.assertEqual(post.call_args.args[1]["grant_type"], "refresh_token")
        self.assertEqual(f.call_args.args[0], "A2")
        self.assertEqual(self.st.d[planlimit._TOK_KEY % "u"]["refresh"], "R1")      # kept when none returned

    def test_401_refreshes_and_retries_once(self):
        self.token()
        with mock.patch.object(planlimit, "_post", return_value={"access_token": "A2", "expires_in": 3600}), \
                mock.patch.object(planlimit, "_fetch_usage", side_effect=[http_error(401), USAGE]) as f:
            self.assertTrue(planlimit.get_status("u")["available"])
        self.assertEqual(f.call_count, 2)

    def test_failed_refresh_degrades_without_raising(self):
        self.token()
        with mock.patch.object(planlimit, "_post", side_effect=OSError("down")), \
                mock.patch.object(planlimit, "_fetch_usage", side_effect=http_error(403)):
            self.assertEqual(planlimit.get_status("u"), {"connected": True, "available": False})

    def test_server_error_keeps_last_good_value(self):
        self.token()
        with mock.patch.object(planlimit, "_fetch_usage", return_value=USAGE):
            good = planlimit.get_status("u")
        t, st = planlimit._cache["u"]
        planlimit._cache["u"] = (t - planlimit._TTL - 1, st)
        with mock.patch.object(planlimit, "_fetch_usage", side_effect=http_error(500)):
            self.assertEqual(planlimit.get_status("u"), good)
        planlimit._cache.clear()
        with mock.patch.object(planlimit, "_fetch_usage", side_effect=OSError("offline")):
            self.assertEqual(planlimit.get_status("u"), {"connected": True, "available": False})

    def test_users_are_separate(self):
        self.token(user="a")
        with mock.patch.object(planlimit, "_fetch_usage", return_value=USAGE):
            planlimit.get_status("a")
        self.assertEqual(planlimit.get_status("b"), {"connected": False})


if __name__ == "__main__":
    unittest.main()
