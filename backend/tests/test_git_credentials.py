"""GitHub credentials never appear on a command line.

`/proc/<pid>/cmdline` is readable by every local user, and container processes show up in the
host's process list. Publishing must therefore hand the token to git through the environment of
that one git process (http.extraheader via GIT_CONFIG_*), never in argv or in a remote URL.

Run: cd backend && python3 -m unittest discover -s tests"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="aiw-test-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import publish  # noqa: E402

VALUE = "TEST-VALUE-" + "4d2c9e"          # neutral placeholder, does not look like a real key
B64 = base64.b64encode(("x-access-token:" + VALUE).encode()).decode()
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _Done:
    returncode = 0
    stdout = ""
    stderr = ""


class GitHelper(unittest.TestCase):
    def test_token_only_in_environment(self):
        with mock.patch.object(publish.subprocess, "run", return_value=_Done()) as run:
            publish._git(["push", "-f", "origin", "main"], "/tmp", VALUE)
        argv = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertEqual(argv, ["git", "push", "-f", "origin", "main"])
        self.assertNotIn(VALUE, " ".join(argv))
        self.assertNotIn(B64, " ".join(argv))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " + B64)

    def test_no_credentials_without_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_CONFIG_COUNT", None)
            with mock.patch.object(publish.subprocess, "run", return_value=_Done()) as run:
                publish._git(["init", "-q"], "/tmp")
        self.assertNotIn("GIT_CONFIG_COUNT", run.call_args.kwargs["env"])

    def test_error_output_is_redacted(self):
        bad = _Done()
        bad.returncode = 1
        bad.stderr = "fatal: %s rejected (%s)" % (VALUE, "AUTHORIZATION: basic " + B64)
        with mock.patch.object(publish.subprocess, "run", return_value=bad):
            with self.assertRaises(ValueError) as cm:
                publish._git(["push"], "/tmp", VALUE)
        self.assertNotIn(VALUE, str(cm.exception))
        self.assertNotIn(B64, str(cm.exception))


class PublishFlow(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="aiw-ws-")
        with open(os.path.join(self.ws, "index.html"), "w") as f:
            f.write("<h1>hi</h1>\n")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_publish_never_puts_the_token_in_argv_or_url(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((list(argv), kw.get("env") or {}))
            return _Done()

        secrets = {"github:token": VALUE, "github:login": "someone"}
        with mock.patch.object(publish, "has_files", return_value=True), \
             mock.patch.object(publish, "scan_project", return_value={"blocking": 0}), \
             mock.patch.object(publish.store, "get_secret", side_effect=secrets.get), \
             mock.patch.object(publish, "_ensure_repo", return_value={"default_branch": "main"}), \
             mock.patch.object(publish, "workspace", return_value=self.ws), \
             mock.patch.object(publish, "db_upsert_pub"), \
             mock.patch.object(publish.subprocess, "run", side_effect=fake_run):
            publish.publish_project("demo", "Demo", "demo", private=True)

        self.assertTrue(calls)
        for argv, _env in calls:
            joined = " ".join(argv)
            self.assertNotIn(VALUE, joined)
            self.assertNotIn(B64, joined)
            self.assertNotRegex(joined, r"https?://[^/\s]+@")
        remote = [a for a, _ in calls if a[1:3] == ["remote", "add"]][0]
        self.assertEqual(remote[-1], "https://github.com/someone/demo.git")
        push = [(a, e) for a, e in calls if "push" in a][0]
        self.assertEqual(push[1].get("GIT_CONFIG_VALUE_0"), "AUTHORIZATION: basic " + B64)


class NoCredentialsOnCommandLines(unittest.TestCase):
    """Static guard over the shipped code: no credential URLs, no Authorization header in argv."""
    RULES = [
        ("credentials in URL", re.compile(r"https?://[^\s/\"'@]*:(?:%s|\$\{?\w+|\{\w)[^\s@]*@")),
        ("credentials in URL", re.compile(r"https?://x-access-token:")),
        ("Authorization header in argv", re.compile(r"Authorization:\s*(?:Bearer|token|Basic|basic)\s+\$")),
        ("Authorization header in argv", re.compile(r"[\"'](?:-H|--header)[\"']\s*,\s*f?[\"']Authorization")),
        ("extraheader in argv", re.compile(r"[\"']-c[\"']\s*,\s*f?[\"'][^\"']*extraheader=|-c\s+http\.\S*extraheader=")),
    ]
    SKIP = {".git", "node_modules", "__pycache__", "dist", ".venv", "venv", "tests"}

    def test_shipped_code_is_clean(self):
        hits = []
        for d, sub, files in os.walk(REPO_ROOT):
            sub[:] = [s for s in sub if s not in self.SKIP]
            for fn in files:
                if not fn.endswith((".py", ".sh", ".yml", ".yaml")):
                    continue
                p = os.path.join(d, fn)
                with open(p, encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if line.lstrip().startswith("#"):
                            continue
                        for name, rx in self.RULES:
                            if rx.search(line):
                                hits.append("%s:%d %s" % (os.path.relpath(p, REPO_ROOT), n, name))
        self.assertEqual(hits, [])

    def test_guard_catches_a_bait(self):
        bait = 'remote = "https://' + 'x-access-token:%s@github.com/%s.git" % (t, r)'
        self.assertTrue(any(rx.search(bait) for _, rx in self.RULES))
        bait2 = 'curl -H "' + 'Authorization: Bearer $TOKEN" https://api.github.com'
        self.assertTrue(any(rx.search(bait2) for _, rx in self.RULES))


if __name__ == "__main__":
    unittest.main()
