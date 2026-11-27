"""Updater proof: pre-check, backup, rollback of a broken package, data kept, check switch.

Runs scripts/self-update.sh against a throw-away git origin and a fake `docker`
(no network, no Docker socket).

Run: cd backend && python3 -m unittest tests.test_self_update_script
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "scripts", "self-update.sh")
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$FAKE_LOG"
case "$*" in
  *build*) if [ -f "$UPDATE_PROJECT/BROKEN" ]; then echo "build failed" >&2; exit 1; fi ;;
esac
exit 0
"""


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def pyproject(version):
    return '[project]\nname = "x"\nversion = "%s"\n' % version


class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="aiw-selfupd-")
        self.addCleanup(shutil.rmtree, self.t, True)
        self.origin = os.path.join(self.t, "origin.git")
        self.work = os.path.join(self.t, "work")      # the publisher
        self.proj = os.path.join(self.t, "project")   # the running install
        git(self.t, "init", "-q", "--bare", "-b", "main", self.origin)
        git(self.t, "clone", "-q", self.origin, self.work)
        git(self.work, "checkout", "-q", "-b", "main")
        self.publish("1.0.0")
        git(self.work, "push", "-q", "origin", "main")
        git(self.t, "clone", "-q", self.origin, self.proj)
        # data / config that must survive any outcome (ignored by git, like the real ones)
        with open(os.path.join(self.proj, ".git", "info", "exclude"), "a") as f:
            f.write(".env\ndata/\n")
        os.makedirs(os.path.join(self.proj, "data"))
        self.data_file = os.path.join(self.proj, "data", "aiwerkstatt.db")
        self.env_file = os.path.join(self.proj, ".env")
        open(self.data_file, "w").write("user data")
        open(self.env_file, "w").write("AIWERKSTATT_PORT=8095\n")
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.bin)
        d = os.path.join(self.bin, "docker")
        open(d, "w").write(FAKE_DOCKER)
        os.chmod(d, 0o755)
        self.log = os.path.join(self.t, "docker.log")
        open(self.log, "w").close()

    def publish(self, version, broken=False):
        open(os.path.join(self.work, "pyproject.toml"), "w").write(pyproject(version))
        bp = os.path.join(self.work, "BROKEN")
        if broken:
            open(bp, "w").write("x")
        elif os.path.exists(bp):
            os.remove(bp)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "v" + version)
        git(self.work, "push", "-q", "origin", "main")

    def run_update(self, health="ok"):
        # health "ok": answers with whatever version the checkout shows now
        cmd = {"ok": "echo '{\"version\":\"'$(sed -n 's/^version *= *\"\\(.*\\)\"/\\1/p' %s/pyproject.toml)'\"}'" % self.proj,
               "down": "false"}[health]
        env = dict(os.environ, UPDATE_PROJECT=self.proj, DOCKER_BIN=os.path.join(self.bin, "docker"),
                   FAKE_LOG=self.log, HEALTH_CMD=cmd, HEALTH_TRIES="2", HEALTH_WAIT="0",
                   GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
                   HOME=self.t, PATH=os.environ["PATH"])
        return subprocess.run(["sh", SCRIPT], env=env, capture_output=True, text=True, timeout=60)

    def head(self):
        return git(self.proj, "rev-parse", "HEAD")

    def version(self):
        return open(os.path.join(self.proj, "pyproject.toml")).read()

    def dockerlog(self):
        return open(self.log).read()

    def assert_data_kept(self):
        self.assertEqual(open(self.data_file).read(), "user data")
        self.assertEqual(open(self.env_file).read(), "AIWERKSTATT_PORT=8095\n")

    # --- pre-check: nothing is touched ---
    def test_up_to_date_changes_nothing(self):
        before = self.head()
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.dockerlog(), "")

    def test_older_remote_version_is_refused(self):
        self.publish("0.9.0")  # fast-forward, but the version goes backwards
        before = self.head()
        r = self.run_update()
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.dockerlog(), "")

    def test_dirty_checkout_is_refused(self):
        self.publish("1.1.0")
        open(os.path.join(self.proj, "pyproject.toml"), "a").write("# local edit\n")
        r = self.run_update()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.dockerlog(), "")

    def test_diverged_remote_is_refused(self):
        open(os.path.join(self.proj, "local.txt"), "w").write("x")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", "local")
        self.publish("1.1.0")
        before = self.head()
        r = self.run_update()
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(self.head(), before)

    def test_unreachable_remote_changes_nothing(self):
        git(self.proj, "remote", "set-url", "origin", os.path.join(self.t, "missing.git"))
        before = self.head()
        r = self.run_update()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.head(), before)

    # --- the happy path: backup + swap + data kept ---
    def test_good_update_swaps_and_keeps_data(self):
        old = self.head()
        self.publish("1.1.0")
        r = self.run_update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('version = "1.1.0"', self.version())
        self.assertEqual(git(self.proj, "rev-parse", "refs/aiwerkstatt/pre-update"), old)
        self.assertEqual(open(os.path.join(self.proj, ".git", "aiwerkstatt-update-previous")).read().strip(), old)
        log = self.dockerlog()
        self.assertIn("build web agent-runner", log)
        self.assertIn("up -d --no-deps web", log)   # only web is recreated
        self.assert_data_kept()

    # --- broken package: old version stays ---
    def test_broken_build_rolls_back(self):
        old = self.head()
        self.publish("1.1.0", broken=True)
        # the rollback rebuild must succeed: BROKEN vanishes with the reset
        r = self.run_update()
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertEqual(self.head(), old)
        self.assertIn('version = "1.0.0"', self.version())
        self.assertIn("ROLLBACK", r.stderr)
        self.assertIn("rolled back", r.stderr)
        self.assert_data_kept()

    def test_new_version_not_answering_rolls_back(self):
        old = self.head()
        self.publish("1.1.0")
        r = self.run_update(health="down")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertEqual(self.head(), old)
        self.assertIn("did not answer", r.stderr)
        self.assert_data_kept()

    def test_untracked_data_survives_rollback(self):
        self.publish("1.1.0", broken=True)
        os.makedirs(os.path.join(self.proj, "data", "nested"))
        open(os.path.join(self.proj, "data", "nested", "q.json"), "w").write("{}")
        self.run_update()
        self.assertTrue(os.path.exists(os.path.join(self.proj, "data", "nested", "q.json")))


class UpdateSwitch(unittest.TestCase):
    """The search can be switched off: covered by tests.test_update_check_switch; this pins the wiring."""

    def test_switch_is_wired_through_compose_and_config(self):
        compose = open(os.path.join(ROOT, "docker-compose.yml")).read()
        cfg = open(os.path.join(ROOT, "backend", "config.py")).read()
        self.assertIn("AIWERKSTATT_UPDATE_CHECK", compose)
        self.assertIn("UPDATE_CHECK", cfg)

    def test_script_is_not_started_by_the_check(self):
        app = open(os.path.join(ROOT, "backend", "app.py")).read()
        start = app.index("def update_check")
        end = app.index("def update_install")
        self.assertNotIn("self-update.sh", app[start:end])


if __name__ == "__main__":
    unittest.main()
