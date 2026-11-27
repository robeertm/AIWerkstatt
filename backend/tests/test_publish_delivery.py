"""GitHub delivery of a built project (publish.py), proven without a network.

Covers the outward-facing paths: token check, repo lookup/creation, the leak-scan gate that must
block a push, what is (not) copied into the pushed tree, the push itself (always through one git
call per step, never with a secret in argv), and the release request. `requests` and `git` are
replaced by fakes.

Run: cd backend && python3 -m unittest discover -s tests"""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="aiw-test-")
os.environ.setdefault("AIWERKSTATT_DATA", _TMP)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import publish  # noqa: E402
import store  # noqa: E402

TOKEN = "TEST-VALUE-" + "7b1e30"


class Resp:
    def __init__(self, code, data=None):
        self.status_code = code
        self._d = data

    def json(self):
        if self._d is None:
            raise ValueError("no json")
        return self._d


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="aiw-ws-")
        self.addCleanup(shutil.rmtree, self.ws, True)
        p = mock.patch.object(publish, "WORKSPACES", self.ws)
        p.start()
        self.addCleanup(p.stop)
        try:
            db.init()
        except Exception:
            pass
        for k in ("github:token", "github:login"):
            store.delete_secret(k)
        db.execute("DELETE FROM pubmeta")

    def project(self, pid="p1", files=None):
        root = os.path.join(self.ws, pid)
        os.makedirs(root, exist_ok=True)
        for name, text in (files or {"app.py": "print('hi')\n"}).items():
            full = os.path.join(root, name)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        return root

    def connect(self):
        store.set_secret("github:token", TOKEN)
        store.set_secret("github:login", "someone")


class TokenAndRepo(Base):
    def test_verify_token_ok_bad_and_offline(self):
        with mock.patch.object(publish.requests, "get", return_value=Resp(200, {"login": "someone"})) as g:
            info = publish.verify_token(TOKEN)
        self.assertEqual(info, {"login": "someone", "name": "someone"})
        self.assertEqual(g.call_args.kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        with mock.patch.object(publish.requests, "get", return_value=Resp(401, {})):
            self.assertIsNone(publish.verify_token(TOKEN))
        with mock.patch.object(publish.requests, "get", side_effect=publish.requests.ConnectionError()):
            self.assertIsNone(publish.verify_token(TOKEN))

    def test_connect_stores_only_after_a_good_check(self):
        with mock.patch.object(publish, "verify_token", return_value=None):
            with self.assertRaises(ValueError):
                publish.connect_github(TOKEN)
        self.assertFalse(publish.github_connected())
        with mock.patch.object(publish, "verify_token", return_value={"login": "someone"}):
            publish.connect_github(TOKEN)
        self.assertTrue(publish.github_connected())
        publish.disconnect_github()
        self.assertFalse(publish.github_connected())
        self.assertIsNone(publish.github_login())

    def test_existing_repo_is_reused_without_create(self):
        with mock.patch.object(publish.requests, "get", return_value=Resp(200, {"name": "r"})), \
                mock.patch.object(publish.requests, "post") as post:
            self.assertEqual(publish._ensure_repo(TOKEN, "someone", "r", True), {"name": "r"})
        post.assert_not_called()

    def test_missing_repo_is_created_with_requested_visibility(self):
        with mock.patch.object(publish.requests, "get", return_value=Resp(404, {})), \
                mock.patch.object(publish.requests, "post", return_value=Resp(201, {"name": "r"})) as post:
            publish._ensure_repo(TOKEN, "someone", "r", True)
        self.assertTrue(post.call_args.kwargs["json"]["private"])
        self.assertEqual(post.call_args.kwargs["json"]["name"], "r")

    def test_lookup_error_and_create_error_are_reported(self):
        with mock.patch.object(publish.requests, "get", return_value=Resp(500, {})):
            with self.assertRaisesRegex(ValueError, "500"):
                publish._ensure_repo(TOKEN, "someone", "r", False)
        with mock.patch.object(publish.requests, "get", return_value=Resp(404, {})), \
                mock.patch.object(publish.requests, "post", return_value=Resp(422, {"message": "name taken"})):
            with self.assertRaisesRegex(ValueError, "name taken"):
                publish._ensure_repo(TOKEN, "someone", "r", False)


class PublishFlow(Base):
    def run_publish(self, **kw):
        calls = []

        def fake_git(args, cwd, token=None):
            calls.append((list(args), cwd, token))
            if args[:1] == ["push"]:
                calls.append(("tree", sorted(os.listdir(cwd))))
            return ""
        with mock.patch.object(publish, "_git", side_effect=fake_git), \
                mock.patch.object(publish, "_ensure_repo",
                                  return_value={"default_branch": "trunk", "html_url": "https://github.com/someone/r"}):
            res = publish.publish_project("p1", "My App", "r", kw.get("private", False))
        return res, calls

    def test_needs_files_and_connection(self):
        with self.assertRaisesRegex(ValueError, "no files"):
            publish.publish_project("nope", "x", "r", False)
        self.project()
        with self.assertRaisesRegex(ValueError, "Connect a GitHub account"):
            publish.publish_project("p1", "x", "r", False)

    def test_blocking_leak_scan_stops_before_any_network_or_git(self):
        self.project(files={"app.py": "x = 1\n"})
        self.connect()
        with mock.patch.object(publish, "scan_project", return_value={"blocking": 2, "review": 0, "findings": []}), \
                mock.patch.object(publish, "_ensure_repo") as ensure, \
                mock.patch.object(publish, "_git") as git:
            with self.assertRaisesRegex(ValueError, "2 blocking"):
                publish.publish_project("p1", "x", "r", False)
        ensure.assert_not_called()
        git.assert_not_called()

    def test_push_goes_to_default_branch_with_token_only_on_push(self):
        self.project(files={"app.py": "x = 1\n", "node_modules/m.js": "1", ".git/config": "[x]", "sub/a.txt": "a"})
        self.connect()
        res, calls = self.run_publish(private=True)
        steps = [c for c in calls if c[0] != "tree"]
        self.assertEqual([s[0][0] if s[0][0] != "-c" else "commit" for s in steps],
                         ["init", "add", "commit", "remote", "push"])
        self.assertIn("user.email=someone@users.noreply.github.com", steps[2][0])
        self.assertEqual(steps[0][0], ["init", "-q", "-b", "trunk"])
        self.assertEqual([s[2] for s in steps], [None, None, None, None, TOKEN])
        remote = steps[3][0]
        self.assertEqual(remote[-1], "https://github.com/someone/r.git")      # plain URL, no token in it
        self.assertNotIn(TOKEN, " ".join(" ".join(s[0]) for s in steps))
        tree = [c for c in calls if c[0] == "tree"][0][1]
        self.assertIn("README.md", tree)                                      # generated when missing
        self.assertIn("app.py", tree)
        self.assertNotIn("node_modules", tree)
        self.assertNotIn(".git", tree)
        self.assertEqual(res["visibility"], "private")
        self.assertEqual(res["full_name"], "someone/r")
        self.assertEqual(publish.pub_status("p1")["repo"], "r")

    def test_existing_readme_is_kept_and_temp_tree_removed(self):
        self.project(files={"README.md": "mine\n", "a.py": "1\n"})
        self.connect()
        seen = {}

        def fake_git(args, cwd, token=None):
            seen["cwd"] = cwd
            if args[0] == "add":
                with open(os.path.join(cwd, "README.md")) as fh:
                    seen["readme"] = fh.read()
            return ""
        with mock.patch.object(publish, "_git", side_effect=fake_git), \
                mock.patch.object(publish, "_ensure_repo", return_value={}):
            res = publish.publish_project("p1", "x", "", False)       # empty repo name -> project id
        self.assertEqual(seen["readme"], "mine\n")
        self.assertFalse(os.path.exists(seen["cwd"]))
        self.assertEqual(res["full_name"], "someone/p1")
        self.assertEqual(res["visibility"], "public")

    def test_failed_push_cleans_up_and_records_nothing(self):
        self.project()
        self.connect()

        def boom(args, cwd, token=None):
            if args[0] == "push":
                raise ValueError("rejected")
            return ""
        with mock.patch.object(publish, "_git", side_effect=boom), \
                mock.patch.object(publish, "_ensure_repo", return_value={}):
            with self.assertRaisesRegex(ValueError, "rejected"):
                publish.publish_project("p1", "x", "r", False)
        self.assertEqual(publish.pub_status("p1"), {})
        self.assertEqual([d for d in os.listdir(tempfile.gettempdir()) if d.startswith("aiw-pub-")
                          and os.path.isdir(os.path.join(tempfile.gettempdir(), d, ".git"))], [])


class Release(Base):
    def published(self):
        self.project()
        self.connect()
        publish.db_upsert_pub("p1", "r", "https://github.com/someone/r", "public")

    def test_requires_publish_connection_and_tag(self):
        with self.assertRaisesRegex(ValueError, "Publish this project"):
            publish.release_project("p1", "1.0", "")
        self.published()
        with self.assertRaisesRegex(ValueError, "version tag"):
            publish.release_project("p1", "  ", "")
        store.delete_secret("github:token")
        with self.assertRaisesRegex(ValueError, "Connect a GitHub"):
            publish.release_project("p1", "1.0", "")

    def test_tag_prefix_rules_and_target_commit(self):
        self.published()
        sha = "a" * 40
        with mock.patch.object(publish.requests, "post",
                               return_value=Resp(201, {"html_url": "https://github.com/someone/r/releases/tag/v1.0"})) as post:
            res = publish.release_project("p1", "v1.0", "notes", sha)
        self.assertEqual(res["tag"], "v1.0")
        self.assertTrue(post.call_args.args[0].endswith("/repos/someone/r/releases"))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tag_name"], "v1.0")
        self.assertEqual(body["target_commitish"], sha)
        self.assertEqual(publish.pub_status("p1")["version"], "v1.0")
        with mock.patch.object(publish.requests, "post", return_value=Resp(201, {})) as post:
            self.assertEqual(publish.release_project("p1", "beta", "")["tag"], "vbeta")     # no digit, no v -> v added
            self.assertEqual(publish.release_project("p1", "2.0", "")["tag"], "2.0")         # digit first: kept as is

    def test_bad_commit_makes_no_request(self):
        self.published()
        with mock.patch.object(publish.requests, "post") as post:
            with self.assertRaises(ValueError):
                publish.release_project("p1", "v1.0", "", "main")
        post.assert_not_called()

    def test_github_refusal_is_reported_and_not_recorded(self):
        self.published()
        with mock.patch.object(publish.requests, "post", return_value=Resp(422, {"message": "tag exists"})):
            with self.assertRaisesRegex(ValueError, "tag exists"):
                publish.release_project("p1", "v1.0", "")
        self.assertIsNone(publish.pub_status("p1").get("version"))


class ZipExport(Base):
    def test_zip_skips_junk_folders(self):
        self.project(files={"a.py": "1", "node_modules/x.js": "1", ".git/HEAD": "ref", "d/b.txt": "b"})
        names = zipfile.ZipFile(publish.zip_project("p1")).namelist()
        self.assertEqual(sorted(names), ["p1/a.py", "p1/d/b.txt"])

    def test_has_files_ignores_only_junk(self):
        self.project(files={"node_modules/x.js": "1"})
        self.assertFalse(publish.has_files("p1"))
        self.project(files={"a.py": "1"})
        self.assertTrue(publish.has_files("p1"))


if __name__ == "__main__":
    unittest.main()
