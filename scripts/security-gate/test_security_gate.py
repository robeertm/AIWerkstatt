"""Tests for security_gate.py: no network, fake tools, throw-away git repos.

Run: python3 -m unittest discover -s scripts/security-gate
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import security_gate as sg  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@users.noreply.github.com", GIT_COMMITTER_NAME="T",
           GIT_COMMITTER_EMAIL="t@users.noreply.github.com", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
FAKE_KEY = "sk-ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"      # built at runtime: no literal in the repo


def git(d, *a):
    return subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True, env=ENV, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="security-gate-test-")
        self.addCleanup(shutil.rmtree, self.t, True)
        self.repo = os.path.join(self.t, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        sg.CACHE = os.path.join(self.t, "cache")
        os.environ["AIWERKSTATT_GATE_TODAY"] = "2026-11-21"
        self.addCleanup(os.environ.pop, "AIWERKSTATT_GATE_TODAY", None)
        sg.PIP_AUDIT = sg.NPM = sg.CARGO_AUDIT = "/nonexistent/tool"

    def commit(self, name, text, msg="c"):
        p = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-q", "-m", msg)

    def fake_pip_audit(self, payload):
        path = os.path.join(self.t, "pip-audit")
        with open(path, "w") as f:
            f.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit 1\n" % json.dumps(payload))
        os.chmod(path, 0o755)
        sg.PIP_AUDIT = path


class Levels(unittest.TestCase):
    def test_cvss(self):
        self.assertEqual(sg.cvss3_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(sg.level_from_score(9.8), "critical")
        self.assertEqual(sg.level_from_score(None), "unknown")
        self.assertIsNone(sg.cvss3_score("nonsense"))

    def test_osv(self):
        self.assertEqual(sg.level_from_osv({"database_specific": {"severity": "HIGH"}}), "high")
        self.assertEqual(sg.level_from_osv({"database_specific": {"severity": "MODERATE"}}), "medium")
        self.assertEqual(sg.level_from_osv({}), "unknown")


class Secrets(Base):
    def test_clean_history_is_green(self):
        self.commit("a.txt", "hello\n")
        r = sg.check(self.repo, only="secrets")
        self.assertEqual(r["status"], "green")
        self.assertEqual(r["secrets"]["commits"], 1)

    def test_secret_deleted_later_is_still_found(self):
        self.commit("conf.txt", "key = %s\n" % FAKE_KEY)
        self.commit("conf.txt", "key = removed\n")
        r = sg.check(self.repo, only="secrets")
        self.assertEqual(r["status"], "red")
        f = r["secrets"]["findings"][0]
        self.assertNotIn(FAKE_KEY, json.dumps(r))                  # masked only
        self.assertEqual(len(f["finding"]), 16)

    def test_secret_file_created(self):
        self.commit("deploy/id_rsa", "x\n")
        self.assertEqual(sg.check(self.repo, only="secrets")["status"], "red")

    def test_advisory_mode_never_red(self):
        self.commit("conf.txt", FAKE_KEY + "\n")
        r = sg.check(self.repo, mode="advisory", only="secrets")
        self.assertEqual(r["status"], "advisory")
        self.assertTrue(r["log_line"].startswith("SECURITY: advisory"))

    def test_exception_covers_finding(self):
        self.commit("conf.txt", FAKE_KEY + "\n")
        fp = sg.check(self.repo, only="secrets")["secrets"]["findings"][0]["finding"]
        ex = os.path.join(self.t, "ex.json")
        json.dump({"exceptions": [{"kind": "secret", "finding": fp, "reason": "test fixture, not a real key",
                                   "until": "2027-05-21"}]}, open(ex, "w"))
        r = sg.check(self.repo, exception_paths=[ex], only="secrets")
        self.assertEqual(r["status"], "green")
        self.assertEqual(r["exceptions"]["used"], 1)

    def test_expired_exception_does_not_cover(self):
        self.commit("conf.txt", FAKE_KEY + "\n")
        fp = sg.check(self.repo, only="secrets")["secrets"]["findings"][0]["finding"]
        ex = os.path.join(self.t, "ex.json")
        json.dump({"exceptions": [{"kind": "secret", "finding": fp, "reason": "test fixture, not a real key",
                                   "until": "2026-11-01"}]}, open(ex, "w"))
        self.assertEqual(sg.check(self.repo, exception_paths=[ex], only="secrets")["status"], "red")


class Exceptions(Base):
    def load(self, items):
        ex = os.path.join(self.t, "ex.json")
        json.dump({"exceptions": items}, open(ex, "w"))
        return sg.load_exceptions([ex])

    def test_validation(self):
        ok = {"kind": "dependency", "id": "GHSA-x", "reason": "no reachable code path", "until": "2027-01-01"}
        valid, errors = self.load([ok])
        self.assertEqual((len(valid), errors), (1, []))
        for bad in ({**ok, "reason": "short"}, {**ok, "until": "2030-01-01"}, {**ok, "until": "soon"},
                    {**ok, "id": ""}, {**ok, "kind": "other"}):
            valid, errors = self.load([bad])
            self.assertEqual((len(valid), len(errors)), (0, 1), bad)

    def test_invalid_list_is_red(self):
        self.commit("a.txt", "x\n")
        ex = os.path.join(self.t, "ex.json")
        open(ex, "w").write("not json")
        r = sg.check(self.repo, exception_paths=[ex], only="secrets")
        self.assertEqual(r["status"], "red")

    def test_missing_file_is_fine(self):
        self.assertEqual(sg.load_exceptions([os.path.join(self.t, "none.json")]), ([], []))


class Dependencies(Base):
    VULN = {"dependencies": [{"name": "requests", "version": "2.0.0", "vulns": [
        {"id": "GHSA-aaaa-bbbb-cccc", "aliases": ["CVE-2099-1"], "fix_versions": ["2.1.0"], "description": "boom"}]}]}

    def setUp(self):
        super().setUp()
        self.commit("requirements.txt", "requests==2.0.0\nflask>=2\n")
        self.level = "high"
        sg.level_lookup = lambda ids: (self.level, "")
        self.addCleanup(lambda: setattr(sg, "level_lookup", _orig_lookup))

    def test_high_is_red(self):
        self.fake_pip_audit(self.VULN)
        r = sg.check(self.repo, only="dependencies")
        self.assertEqual(r["status"], "red")
        self.assertEqual(r["dependencies"]["counts"]["high"], 1)
        self.assertTrue(any("not pinned" in n for n in r["notes"]))

    def test_medium_is_only_advisory(self):
        self.level = "medium"
        self.fake_pip_audit(self.VULN)
        self.assertEqual(sg.check(self.repo, only="dependencies")["status"], "green")

    def test_exception_covers_alias(self):
        self.fake_pip_audit(self.VULN)
        ex = os.path.join(self.t, "ex.json")
        json.dump({"exceptions": [{"kind": "dependency", "id": "CVE-2099-1", "package": "requests",
                                   "reason": "not reachable from our code", "until": "2027-01-01"}]}, open(ex, "w"))
        self.assertEqual(sg.check(self.repo, exception_paths=[ex], only="dependencies")["status"], "green")

    def test_no_tool_and_no_cache_fails_closed(self):
        r = sg.check(self.repo, only="dependencies")
        self.assertEqual(r["status"], "red")
        self.assertTrue(any("pip-audit not installed" in e for e in r["errors"]))

    def test_cache_answers_when_tool_is_gone(self):
        self.fake_pip_audit(self.VULN)
        sg.check(self.repo, only="dependencies")
        sg.PIP_AUDIT = "/nonexistent/tool"
        r = sg.check(self.repo, only="dependencies")
        self.assertEqual(r["dependencies"]["ecosystems"]["pypi"]["status"], "cached")
        self.assertEqual(r["status"], "red")                        # the cached high finding still counts

    def test_dev_requirements_are_skipped(self):
        self.commit("requirements-dev.txt", "pytest==1.0.0\n")
        pins, _, used = sg.python_sources(self.repo, sg.tracked_files(self.repo))
        self.assertNotIn("pytest==1.0.0", pins)
        self.assertEqual(used, ["requirements.txt"])


_orig_lookup = sg.level_lookup


class Npm(unittest.TestCase):
    def test_parse(self):
        data = {"vulnerabilities": {"foo": {"severity": "high", "range": "<2", "via": [
            {"source": 1, "name": "foo", "title": "t", "url": "https://github.com/advisories/GHSA-abcd-efgh-ijkl",
             "severity": "high", "range": "<2"}, "bar"]}}}
        f = sg.npm_parse(data, "package-lock.json", {"foo": ["1.0.0"]})
        self.assertEqual([(x["package"], x["id"], x["level"], x["version"]) for x in f],
                         [("foo", "GHSA-abcd-efgh-ijkl", "high", "1.0.0")])


if __name__ == "__main__":
    unittest.main()
