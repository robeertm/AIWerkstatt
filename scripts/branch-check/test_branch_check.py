"""Tests for scripts/branch-check/branch_check.py with real git in throw-away repositories (no network).

Run: python3 -m unittest discover -s scripts/branch-check
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import branch_check as bc  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@users.noreply.github.com", GIT_COMMITTER_NAME="T",
           GIT_COMMITTER_EMAIL="t@users.noreply.github.com", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")


def g(d, *a):
    return subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True, env=ENV, check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="branch-check-test-")
        self.addCleanup(shutil.rmtree, self.t, True)
        origin = os.path.join(self.t, "origin.git")
        self.c = os.path.join(self.t, "clone")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True, env=ENV)
        subprocess.run(["git", "clone", "-q", origin, self.c], check=True, env=ENV, capture_output=True)
        g(self.c, "checkout", "-q", "-B", "main")
        self.commit("a.txt", "start")
        g(self.c, "push", "-q", "origin", "main")
        self.commit("b.txt", "release")
        g(self.c, "tag", "v1.0.0")
        g(self.c, "push", "-q", "origin", "main", "--tags")
        self.cl = bc.Clone(self.c)

    def commit(self, f, text):
        with open(os.path.join(self.c, f), "a") as fh:
            fh.write(text + "\n")
        g(self.c, "add", f)
        g(self.c, "commit", "-q", "-m", text)
        return g(self.c, "rev-parse", "HEAD")

    def branch(self, name, f="x.txt", text="work"):
        g(self.c, "checkout", "-q", "-b", name, "main")
        sha = self.commit(f, text)
        g(self.c, "checkout", "-q", "main")
        return sha


class Check(Base):
    def test_local_only_is_not_pushed(self):
        sha = self.branch("task/1")
        r = bc.check(self.cl, ["task/1"], [sha[:7]])
        self.assertEqual(r["state"], "not_pushed")
        self.assertIn("only locally", r["reason"])
        self.assertEqual(r["commit"], sha[:9])

    def test_remote_branch_only_is_not_pushed(self):
        self.branch("task/2")
        g(self.c, "push", "-q", "origin", "task/2")
        r = bc.check(self.cl, ["task/2"])
        self.assertEqual(r["state"], "not_pushed")
        self.assertIn("remote branch", r["reason"])

    def test_train_hint_on_remote_is_unclear_then_not_pushed_after_the_grace(self):
        self.branch("task/7")
        g(self.c, "push", "-q", "origin", "task/7")
        r = bc.check(self.cl, ["task/7"], train="1.2.0", age_hours=5)
        self.assertEqual(r["state"], "unclear")
        self.assertIn("planned for train 1.2.0", r["reason"])
        self.assertEqual(bc.check(self.cl, ["task/7"], train="1.2.0")["state"], "unclear")        # age unknown = young
        late = bc.check(self.cl, ["task/7"], train="1.2.0", age_hours=60)
        self.assertEqual(late["state"], "not_pushed")
        self.assertIn("planned for train 1.2.0", late["reason"])
        self.assertEqual(bc.check(self.cl, ["task/7"])["state"], "not_pushed")                    # no hint: as before

    def test_train_hint_never_saves_a_local_only_branch(self):
        self.branch("task/8")
        r = bc.check(self.cl, ["task/8"], train="1.2.0", age_hours=1)
        self.assertEqual(r["state"], "not_pushed")
        self.assertIn("only locally", r["reason"])

    def test_running_task_is_not_checked(self):
        self.branch("task/9")
        r = bc.check(self.cl, ["task/9"], running=True)
        self.assertEqual(r["state"], "unclear")
        self.assertIn("working again", r["reason"])

    def test_train_hint_is_found_in_text(self):
        self.assertEqual(bc.train_hint("Branch task/1 is ready for release train 1.50.0."), "1.50.0")
        self.assertEqual(bc.train_hint("goes into train v2.1"), "2.1")
        self.assertEqual(bc.train_hint("The train leaves later, see v1.0 notes"), "")
        self.assertEqual(bc.train_hint(None), "")

    def test_cli_picks_the_hint_from_the_text(self):
        self.branch("task/10")
        g(self.c, "push", "-q", "origin", "task/10")
        f = os.path.join(self.t, "answer.md")
        with open(f, "w") as fh:
            fh.write("Work is on task/10, planned for train 1.4.0.\n")
        self.assertEqual(bc.main([self.c, "--text", f, "--json"]), 2)                            # unclear = exit 2
        self.assertEqual(bc.main([self.c, "--text", f, "--age-hours", "100"]), 3)                # grace over = not_pushed
        self.assertEqual(bc.main([self.c, "--branch", "task/10", "--running"]), 2)

    def test_merged_is_on_main_then_in_tag(self):
        self.branch("task/3")
        g(self.c, "merge", "-q", "--ff-only", "task/3")
        g(self.c, "push", "-q", "origin", "main")
        self.assertEqual(bc.check(bc.Clone(self.c), ["task/3"])["state"], "on_main")
        g(self.c, "tag", "v1.1.0")
        self.assertEqual(bc.check(bc.Clone(self.c), ["task/3"])["state"], "in_tag")

    def test_train_branch_counts(self):
        self.branch("task/4")
        g(self.c, "push", "-q", "origin", "task/4")
        g(self.c, "checkout", "-q", "-b", "release/1.1.0", "main")
        g(self.c, "merge", "-q", "--no-ff", "-m", "train", "task/4")
        g(self.c, "push", "-q", "origin", "release/1.1.0")
        g(self.c, "checkout", "-q", "main")
        self.assertEqual(bc.check(bc.Clone(self.c), ["task/4"])["state"], "on_main")

    def test_rebased_same_patch_counts(self):
        sha = self.branch("task/5")
        self.commit("other.txt", "main moves on")
        g(self.c, "cherry-pick", sha)
        g(self.c, "push", "-q", "origin", "main")
        r = bc.check(bc.Clone(self.c), [], [sha[:8]])
        self.assertEqual(r["state"], "on_main")
        self.assertIn("rebased", r["reason"])

    def test_without_branch_newest_commit_decides(self):
        wip = self.branch("scratch", "w.txt", "wip")
        self.commit("f1.txt", "f1")
        g(self.c, "push", "-q", "origin", "main")
        time.sleep(1.1)
        final = self.commit("f2.txt", "f2")
        g(self.c, "push", "-q", "origin", "main")
        self.assertEqual(bc.check(bc.Clone(self.c), [], [wip[:7], final[:7]])["state"], "on_main")
        self.assertEqual(bc.check(bc.Clone(self.c), [], [wip[:7]])["state"], "not_pushed")

    def test_unclear(self):
        self.assertEqual(bc.check(self.cl, ["task/none"], ["deadbee"])["state"], "unclear")


class Extract(unittest.TestCase):
    def test_extract(self):
        shas, brs = bc.extract("Done on `task/7` at 3f65d01 (run 20261120, defaced, 12345678) and work/x-y.")
        self.assertEqual(shas, ["3f65d01"])
        self.assertEqual(brs, ["task/7", "work/x-y"])
        self.assertEqual(bc.extract("on feature/z", ("feature",))[1], ["feature/z"])


class Cli(Base):
    def test_exit_codes_and_json(self):
        sha = self.branch("task/8")
        with tempfile_text("Result on task/8, commit %s" % sha[:7]) as f:
            self.assertEqual(bc.main([self.c, "--text", f, "--json"]), 3)
        g(self.c, "merge", "-q", "--ff-only", "task/8")
        g(self.c, "push", "-q", "origin", "main")
        self.assertEqual(bc.main([self.c, "--branch", "task/8"]), 0)
        self.assertEqual(bc.main([self.c, "--branch", "task/none"]), 2)
        self.assertEqual(bc.main([os.path.join(self.t, "nope")]), 2)


class tempfile_text:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        fd, self.p = tempfile.mkstemp(prefix="bc-text-")
        with os.fdopen(fd, "w") as f:
            f.write(self.text)
        return self.p

    def __exit__(self, *a):
        os.unlink(self.p)


if __name__ == "__main__":
    unittest.main()
