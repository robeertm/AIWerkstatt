"""Tests for the result store ("green stays green"): a result per test, only red, affected or never-run tests run again.

Real git in a throw-away repo, a real SQLite file in a temp folder. Run: python3 -m unittest discover -s scripts/test-runner
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import result_store as R  # noqa: E402

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_a" name="test_one" time="0.5" />
<testcase classname="tests.test_a" name="test_two[x y]" time="1.0"><failure message="AssertionError: too slow">x</failure></testcase>
<testcase classname="tests.test_b" name="test_three" time="0.1"><skipped message="no browser" /></testcase>
<testcase classname="tests.test_b" name="test_four" time="2.0" />
<testcase classname="tests.test_b" name="test_four" time="0.2"><error message="teardown broken" /></testcase>
<testcase classname="tests.sub.test_c.TestK" name="test_five" time="0.3" />
</testsuite></testsuites>"""

RED_TWO = "tests/test_a.py::test_two[x y]"
RED_FOUR = "tests/test_b.py::test_four"
ALL_FILES = ["tests/sub/test_c.py", "tests/test_a.py", "tests/test_b.py"]


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "P", "GIT_AUTHOR_EMAIL": "p@example.invalid",
                               "GIT_COMMITTER_NAME": "P", "GIT_COMMITTER_EMAIL": "p@example.invalid"}).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="result-store-")
        self.dir = os.path.join(self.t, "store")
        self.repo = os.path.join(self.t, "repo")
        os.makedirs(os.path.join(self.repo, "tests", "sub"))
        os.makedirs(os.path.join(self.repo, "app"))
        self.write("app/calc.py", "def plus(a, b):\n    return a + b\n")
        self.write("app/view.py", "import calc\n")
        self.write("app/style.css", "a{}\n")
        self.write("tests/helper.py", "X = 1\n")
        self.write("tests/conftest.py", "\n")
        self.write("tests/test_a.py", "import calc\n")
        self.write("tests/test_b.py", "import helper\n")
        self.write("tests/sub/test_c.py", "import os\n")
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        self.c1 = self.commit("start")
        self.store = R.Store("demo", self.dir)
        self.env = "image+FLAG=0"

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.t, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def ingest_full(self, commit, run="R1", xml=JUNIT, ts=None):
        files = R.list_test_files(self.repo, commit)
        return self.store.ingest_junit(xml, self.env, commit, run, files, whole_files=files, ts=ts)

    def plan(self, selector=None, only=None):
        commit = git(self.repo, "rev-parse", "HEAD")
        deps = R.Dependencies(self.repo, commit, selector=selector)
        return R.plan(self.store, self.env, commit, deps.tests, deps, only=only), deps


class JunitAndOutput(Base):
    def test_junit_node_ids_and_worst_outcome(self):
        files = R.list_test_files(self.repo, self.c1)
        self.assertEqual(files, ALL_FILES)
        f = {x[0]: x for x in R.junit_read(JUNIT, files)}
        self.assertEqual(f["tests/test_a.py::test_one"][2], "passed")
        self.assertEqual(f[RED_TWO][2], "failed")
        self.assertEqual(f[RED_TWO][4], "AssertionError: too slow")
        self.assertEqual(f["tests/test_b.py::test_three"][2], "skipped")
        self.assertEqual(f[RED_FOUR][2], "error")                       # an error in teardown counts although the call was green
        self.assertAlmostEqual(f[RED_FOUR][3], 2.2)
        self.assertIn("tests/sub/test_c.py::TestK::test_five", f)        # class behind the file path

    def test_output_lines_xdist_and_serial(self):
        self.assertEqual(R.output_line("[gw3] [ 12%] PASSED tests/test_a.py::test_one "), ("tests/test_a.py::test_one", "passed"))
        self.assertEqual(R.output_line("[gw0] [100%] FAILED tests/test_a.py::test_two[x y]"), (RED_TWO, "failed"))
        self.assertEqual(R.output_line("tests/test_b.py::test_three SKIPPED (no browser)                [ 50%]"),
                         ("tests/test_b.py::test_three", "skipped"))
        self.assertEqual(R.output_line("tests/test_b.py::test_four ERROR                            [ 75%]"), (RED_FOUR, "error"))
        self.assertIsNone(R.output_line("FAILED tests/test_a.py::test_two - AssertionError"))
        self.assertIsNone(R.output_line("3 passed in 1.00s"))


class GreenStaysGreen(Base):
    def test_first_plan_runs_everything(self):
        plan, _ = self.plan()
        self.assertEqual(sorted(plan.whole), ALL_FILES)
        self.assertEqual(plan.green, {})

    def test_same_commit_only_reds(self):
        self.ingest_full(self.c1)
        plan, _ = self.plan()
        self.assertEqual(plan.whole, [])
        self.assertEqual(sorted(plan.single), sorted([RED_TWO, RED_FOUR]))
        self.assertEqual(sorted(plan.red_before), sorted(plan.single))
        self.assertEqual(len(plan.green), 3)                              # one, three (skipped counts), five
        self.assertEqual(plan.sources, {"R1": 3})
        self.assertEqual(plan.targets(), [RED_TWO, RED_FOUR])

    def test_flaky_serial_green_then_nothing_more(self):
        self.ingest_full(self.c1)
        self.store.record(self.env, RED_TWO, "passed", self.c1, "R1", kind="serial", attempt=1)
        self.store.record(self.env, RED_FOUR, "failed", self.c1, "R1", kind="serial", attempt=1)
        rows = self.store.flaky_since(0)
        self.assertEqual([x["test_id"] for x in rows], [RED_TWO])         # never silent: one flaky row
        plan, _ = self.plan()
        self.assertEqual(plan.single, [RED_FOUR])                         # stably red runs again

    def test_only_test_file_changed_only_that_one_runs(self):
        self.ingest_full(self.c1)
        self.store.record(self.env, RED_TWO, "passed", self.c1, "R1", kind="serial", attempt=1)
        self.store.record(self.env, RED_FOUR, "passed", self.c1, "R1", kind="serial", attempt=1)
        self.write("tests/test_b.py", "import helper\n# test fixed\n")
        self.commit("tests only")
        plan, deps = self.plan()
        self.assertEqual(plan.whole, ["tests/test_b.py"])
        self.assertEqual(plan.single, [])
        self.assertEqual(len(plan.green), 3)                              # test_a (2) + test_c stay green
        self.assertIn("1 test-side", list(deps.reasons.values())[0])

    def test_conftest_keeps_greens(self):
        self.ingest_full(self.c1)
        self.write("tests/conftest.py", "# new fixture\n")
        self.commit("conftest")
        plan, _ = self.plan()
        self.assertEqual(plan.whole, [])
        self.assertEqual(sorted(plan.single), sorted([RED_TWO, RED_FOUR]))   # only the reds

    def test_test_helper_changed_importers_run(self):
        self.ingest_full(self.c1)
        self.write("tests/helper.py", "X = 2\n")
        self.commit("helper")
        plan, _ = self.plan()
        self.assertEqual(plan.whole, ["tests/test_b.py"])
        self.assertEqual(plan.single, [RED_TWO])

    def test_app_module_changed_only_importers(self):
        self.ingest_full(self.c1)
        self.write("app/calc.py", "def plus(a, b):\n    return b + a\n")
        self.commit("app")
        plan, _ = self.plan()
        self.assertEqual(plan.whole, ["tests/test_a.py"])                 # imports calc
        self.assertEqual(plan.single, [RED_FOUR])                         # red stays red
        self.assertIn("tests/sub/test_c.py::TestK::test_five", plan.green)

    def test_app_module_transitive(self):
        self.write("tests/sub/test_c.py", "import view\n")
        c = self.commit("c imports view")
        self.ingest_full(c)
        self.write("app/calc.py", "def plus(a, b):\n    return 0 + a + b\n")
        self.commit("app")
        plan, _ = self.plan()
        self.assertEqual(sorted(plan.whole), ["tests/sub/test_c.py", "tests/test_a.py"])   # view imports calc

    def test_non_python_app_file_everything(self):
        self.ingest_full(self.c1)
        self.write("app/style.css", "a{color:red}\n")
        self.commit("css")
        plan, deps = self.plan()
        self.assertEqual(sorted(plan.whole), ALL_FILES)
        self.assertIn("everything", list(deps.reasons.values())[0])

    def _selector(self, body):
        path = os.path.join(self.t, "selector.py")
        with open(path, "w") as fh:
            fh.write(body)
        return "%s %s" % (sys.executable, path)

    def test_selector_hook_list_and_full(self):
        """A project selector decides for app-side files: a list of test files or the word FULL."""
        selector = self._selector("import sys\nfiles = sys.argv[1:]\n"
                                  "print('FULL' if any(x.endswith('.css') for x in files) else 'tests/sub/test_c.py')\n")
        self.ingest_full(self.c1)
        self.write("app/calc.py", "def plus(a, b):\n    return a+b\n")
        self.commit("app")
        plan, _ = self.plan(selector=selector)
        self.assertEqual(plan.whole, ["tests/sub/test_c.py"])             # the project rule instead of import analysis
        self.write("app/style.css", "b{}\n")
        self.commit("css")
        plan, _ = self.plan(selector=selector)
        self.assertEqual(len(plan.whole), 3)

    def test_selector_failure_means_everything(self):
        selector = self._selector("import sys\nsys.exit(3)\n")
        self.ingest_full(self.c1)
        self.write("app/calc.py", "def plus(a, b):\n    return a+b\n")
        self.commit("app")
        plan, _ = self.plan(selector=selector)
        self.assertEqual(len(plan.whole), 3)

    def test_unknown_commit_everything(self):
        files = R.list_test_files(self.repo, self.c1)
        self.store.ingest_junit(JUNIT, self.env, "f" * 40, "R0", files, whole_files=files)
        plan, _ = self.plan()
        self.assertEqual(len(plan.whole), 3)

    def test_older_than_seven_days_everything(self):
        self.ingest_full(self.c1, ts=time.time() - 8 * 86400)
        plan, _ = self.plan()
        self.assertEqual(len(plan.whole), 3)

    def test_environments_never_mix(self):
        self.ingest_full(self.c1)
        commit = git(self.repo, "rev-parse", "HEAD")
        deps = R.Dependencies(self.repo, commit)
        plan = R.plan(self.store, "other+FLAG=0", commit, deps.tests, deps)
        self.assertEqual(len(plan.whole), 3)

    def test_targets_collapse_when_too_many(self):
        p = R.Plan("u", "c")
        p.single = ["tests/test_%d.py::t%d" % (i % 7, i) for i in range(50)]
        self.assertEqual(len(p.targets(max_targets=400)), 50)
        self.assertEqual(p.targets(max_targets=10), sorted({"tests/test_%d.py" % i for i in range(7)}))
        p2 = R.Plan("u", "c")
        p2.single = ["tests/test_x.py::" + "a" * 500]
        self.assertEqual(p2.targets(), ["tests/test_x.py"])


class ResumeAndReport(Base):
    def test_abort_mid_run_resumes_with_deselect(self):
        """A run is cut off after part of its tests (no junit): what was measured stays, only the rest runs afterwards."""
        self.ingest_full(self.c1)                                         # test lists from an earlier full run
        self.write("app/style.css", "c{}\n")
        c2 = self.commit("css -> everything again")
        self.store.run_begin("R2", self.env, c2)
        for line in ("[gw0] [ 20%] PASSED tests/test_a.py::test_one", "[gw1] [ 40%] PASSED tests/test_b.py::test_three",
                     "[gw1] [ 50%] FAILED tests/test_b.py::test_four"):
            tid, outcome = R.output_line(line)
            self.store.record(self.env, tid, outcome, c2, "R2")
        self.store.close()                                                # "process gone"
        self.store = R.Store("demo", self.dir)
        plan, _ = self.plan()
        # css affects every file, so they run whole (new tests would be missing otherwise), but what is already green on c2
        # is deselected and not repeated; the red one runs again.
        self.assertEqual(sorted(plan.whole), ALL_FILES)
        self.assertEqual(sorted(plan.deselect), ["tests/test_a.py::test_one", "tests/test_b.py::test_three"])
        self.assertIn("tests/test_a.py::test_one", plan.green)
        targets = plan.targets()
        self.assertEqual(plan.deselect_args(targets),
                         ["--deselect=tests/test_a.py::test_one", "--deselect=tests/test_b.py::test_three"])
        self.assertEqual(plan.deselect_args(targets, max_targets=4), [])  # does not fit the budget -> they run again

    def test_older_result_never_overwrites_newer(self):
        self.store.record(self.env, "tests/test_a.py::test_one", "failed", self.c1, "R2", ts=2000)
        self.store.record(self.env, "tests/test_a.py::test_one", "passed", self.c1, "R1", ts=1000)
        self.assertEqual(self.store.state_of(self.env)["tests/test_a.py::test_one"]["outcome"], "failed")

    def test_report_for_the_nightly_summary(self):
        self.ingest_full(self.c1)
        self.store.run_begin("R1", self.env, self.c1)
        self.store.record(self.env, RED_TWO, "passed", self.c1, "R1", kind="serial", attempt=1)
        self.store.run_end("R1", "red", {"new": 5, "reused": 0, "flaky": 1, "red": 1})
        d = R.report(24, self.dir)["projects"]["demo"]
        self.assertEqual(d["flaky"], 1)
        self.assertEqual(d["flaky_tests"], [[RED_TWO, 1]])
        self.assertEqual([x["test"] for x in d["stably_red"]], [RED_FOUR])
        self.assertEqual(d["runs"][0]["new"], 5)
        json.dumps(d)

    def test_cli_ingest_and_plan(self):
        x = os.path.join(self.t, "j.xml")
        with open(x, "w") as fh:
            fh.write(JUNIT)
        env = dict(os.environ, AIWERKSTATT_RESULT_STORE_DIR=self.dir)
        tool = os.path.join(HERE, "result_store.py")
        p = subprocess.run([sys.executable, tool, "ingest", "--project", "demo", "--env", self.env, "--commit", self.c1, "--run", "R9",
                            "--junit", x, "--repo", self.repo, "--whole"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("5 tests (2 green, 1 skipped, 2 red)", p.stdout)
        p = subprocess.run([sys.executable, tool, "plan", "--project", "demo", "--env", self.env, "--repo", self.repo, "--json"],
                           capture_output=True, text=True, env=env)
        d = json.loads(p.stdout)
        self.assertEqual(d["reused"], 3)
        self.assertEqual(sorted(d["single"]), sorted([RED_TWO, RED_FOUR]))
        p = subprocess.run([sys.executable, tool, "status", "--project", "demo", "--red"], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("red %s %s" % (self.env, RED_FOUR), p.stdout)

    def test_invalid_project_name_refused(self):
        with self.assertRaises(ValueError):
            R.Store("../evil", self.dir)


class ImageEnvironment(Base):
    """1.1.0: image id + environment fingerprint per result; a green result of ANOTHER environment never counts."""

    def test_old_store_is_migrated(self):
        import sqlite3
        old = os.path.join(self.dir, "old.sqlite")
        db = sqlite3.connect(old)
        db.executescript("CREATE TABLE state (env TEXT NOT NULL, test_id TEXT NOT NULL, file TEXT, outcome TEXT, commit_ TEXT, run TEXT,"
                         " kind TEXT, attempt INTEGER, duration_s REAL, ts REAL, last TEXT, message TEXT, PRIMARY KEY (env, test_id));"
                         "CREATE TABLE history (nr INTEGER PRIMARY KEY, env TEXT, test_id TEXT, file TEXT, outcome TEXT, commit_ TEXT,"
                         " run TEXT, kind TEXT, attempt INTEGER, duration_s REAL, ts REAL, last TEXT, message TEXT);"
                         "INSERT INTO state (env, test_id, outcome, commit_, run, ts) VALUES ('e', 't', 'passed', 'c', 'r', 1);")
        db.commit()
        db.close()
        s = R.Store("old", self.dir)
        try:
            for table in ("state", "history"):
                cols = {r[1] for r in s.db.execute("PRAGMA table_info(%s)" % table)}
                self.assertTrue({"image", "env_fp"} <= cols, table)
            self.assertEqual(s.state_of("e")["t"]["outcome"], "passed")
            self.assertIsNone(s.state_of("e")["t"]["env_fp"])
            s.record("e", "t2", "passed", "c", "r2", image="sha256:1", env_fp="f1")
            self.assertEqual(s.state_of("e")["t2"]["env_fp"], "f1")
        finally:
            s.close()
        R.Store("old", self.dir).close()                                   # opening twice does not fail

    def test_attach_image_only_fills_empty_rows(self):
        self.ingest_full(self.c1, run="R1")
        self.store.record(self.env, "tests/x.py::t", "passed", self.c1, "R1", image="sha256:old", env_fp="old")
        self.assertEqual(self.store.attach_image(self.env, "R1", "sha256:new", "fp1"), 5)
        st = self.store.state_of(self.env)
        self.assertEqual(st[RED_TWO]["env_fp"], "fp1")
        self.assertEqual(st["tests/x.py::t"]["env_fp"], "old")
        self.assertEqual(self.store.attach_image(self.env, "R1", None, None), 0)

    def _plan(self, env_fp):
        commit = git(self.repo, "rev-parse", "HEAD")
        deps = R.Dependencies(self.repo, commit)
        return R.plan(self.store, self.env, commit, deps.tests, deps, env_fp=env_fp)

    def test_other_environment_never_counts(self):
        files = R.list_test_files(self.repo, self.c1)
        self.store.ingest_junit(JUNIT, self.env, self.c1, "R1", files, whole_files=files, image="sha256:a", env_fp="aaa")
        same = self._plan("aaa")
        self.assertEqual(len(same.green), 3)
        other = self._plan("bbb")
        self.assertEqual(other.green, {})
        self.assertEqual(sorted(other.whole), ALL_FILES)
        self.assertIn("other image environment", other.reasons[ALL_FILES[0]])
        unknown = self._plan(None)                                          # fingerprint of the run not known yet: as before
        self.assertEqual(len(unknown.green), 3)

    def test_result_without_fingerprint_is_reused(self):
        self.ingest_full(self.c1)
        self.assertEqual(len(self._plan("bbb").green), 3)                   # results from before 1.1.0 carry none

    def test_foreign_env_after_the_run(self):
        self.store.record(self.env, "a::1", "passed", self.c1, "R1", env_fp="aaa")
        self.store.record(self.env, "a::2", "passed", self.c1, "R1", env_fp="bbb")
        self.store.record(self.env, "a::3", "passed", self.c1, "R1")
        green = self.store.state_of(self.env)
        self.assertEqual(R.foreign_env(green, "bbb"), ["a::1"])
        self.assertEqual(R.foreign_env(green, None), [])
        self.assertEqual(R.foreign_env({}, "x"), [])

    def test_foreign_green_on_same_commit_is_not_deselected(self):
        files = R.list_test_files(self.repo, self.c1)
        self.store.ingest_junit(JUNIT, self.env, self.c1, "R1", files, whole_files=[], image="i", env_fp="aaa")
        plan = self._plan("bbb")
        self.assertEqual(plan.deselect, [])


class Variants(Base):
    def setUp(self):
        super().setUp()
        self.other = "image+FLAG=1"
        self.files = R.list_test_files(self.repo, self.c1)

    def _plan(self, variants):
        commit = git(self.repo, "rev-parse", "HEAD")
        deps = R.Dependencies(self.repo, commit)
        return R.plan(self.store, self.env, commit, deps.tests, deps, variants=variants)

    def test_sibling_results_count(self):
        self.store.ingest_junit(JUNIT, self.other, self.c1, "V1", self.files, whole_files=self.files)
        plan = self._plan([(self.other, set())])
        self.assertEqual(plan.whole, [])
        self.assertEqual(len(plan.green), 3)
        self.assertEqual(plan.variants, {self.other: 3})
        self.assertEqual(sorted(plan.single), sorted([RED_TWO, RED_FOUR]))          # red stays red
        self.assertEqual(R.plan(self.store, self.env, self.c1, self.files).green, {})   # without variants nothing is taken over

    def test_newest_result_wins(self):
        now = time.time()
        self.ingest_full(self.c1, run="R1", ts=now - 300)                          # own: green one, red two
        self.store.record(self.other, RED_TWO, "passed", self.c1, "V1", ts=now - 200)   # sibling newer and green
        self.store.record(self.other, "tests/test_a.py::test_one", "failed", self.c1, "V1", ts=now - 100)   # sibling newer, red
        self.store.file_complete(self.other, "tests/test_a.py", ["tests/test_a.py::test_one", RED_TWO], self.c1, "V1", ts=now - 100)
        plan = self._plan([(self.other, set())])
        self.assertIn(RED_TWO, plan.green)
        self.assertNotIn("tests/test_a.py::test_one", plan.green)
        self.assertIn("tests/test_a.py::test_one", plan.single)

    def test_excluded_files_are_not_taken_over(self):
        self.store.ingest_junit(JUNIT, self.other, self.c1, "V1", self.files, whole_files=self.files)
        plan = self._plan([(self.other, {"tests/test_b.py"})])
        self.assertEqual(plan.whole, ["tests/test_b.py"])
        self.assertEqual(plan.variants, {self.other: 2})                          # test_a: one green, test_c: one green

    def test_none_means_no_takeover(self):
        self.store.ingest_junit(JUNIT, self.other, self.c1, "V1", self.files, whole_files=self.files)
        plan = self._plan([(self.other, None)])
        self.assertEqual(sorted(plan.whole), ALL_FILES)
        self.assertEqual(plan.variants, {})

    def test_variant_exclusions_with_git_grep(self):
        self.write("tests/test_a.py", "import os\nos.environ.get('MODE_FLAG')\n")
        c = self.commit("test reads the variable")
        self.assertEqual(R.variant_exclusions(self.repo, c, ["MODE_FLAG"]), {"tests/test_a.py"})
        self.assertEqual(R.variant_exclusions(self.repo, c, ["NOWHERE_USED"]), set())
        self.write("app/calc.py", "import os\nos.environ.get('MODE_FLAG')\n")
        c2 = self.commit("app reads it, too")
        self.assertIsNone(R.variant_exclusions(self.repo, c2, ["MODE_FLAG"]))     # app code: every test may be affected
        self.write("app/calc.py", "def plus(a, b):\n    return a + b\n")
        self.write("tests/conftest.py", "import os\nos.environ.get('MODE_FLAG')\n")
        c3 = self.commit("conftest reads it")
        self.assertIsNone(R.variant_exclusions(self.repo, c3, ["MODE_FLAG"]))
        self.assertIsNone(R.variant_exclusions(self.repo, c3, ["not a name; rm"]))
        self.assertIsNone(R.variant_exclusions(self.t, c3, ["MODE_FLAG"]))         # not a repository: take nothing over

    def test_cli_variant(self):
        self.store.ingest_junit(JUNIT, self.other, self.c1, "V1", self.files, whole_files=self.files)
        env = dict(os.environ, AIWERKSTATT_RESULT_STORE_DIR=self.dir)
        tool = os.path.join(HERE, "result_store.py")
        p = subprocess.run([sys.executable, tool, "plan", "--project", "demo", "--env", self.env, "--repo", self.repo, "--json",
                            "--variant", "%s:SOME_FLAG" % self.other], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(p.stdout)
        self.assertEqual(d["reused"], 3)
        self.assertEqual(d["variants"], {self.other: 3})


class FullRuns(Base):
    def test_rule_per_minor_version_and_environment(self):
        fam = "image"
        self.assertIn("no full image run for 1.2.x", R.full_run_reason(self.store, fam, "1.2.3"))
        self.assertIn("unknown", R.full_run_reason(self.store, fam, ""))
        R.record_full_run(self.store, fam, "image+FLAG=0", "F1", self.c1, "1.2.3", env_fp="aaa", image="sha256:a", total=300)
        self.assertIsNone(R.full_run_reason(self.store, fam, "1.2.9", "aaa"))        # same minor, same environment
        self.assertIsNone(R.full_run_reason(self.store, fam, "1.2.9"))               # environment not known yet
        self.assertIn("no full image run for 1.3.x", R.full_run_reason(self.store, fam, "1.3.0", "aaa"))
        self.assertIn("environment changed", R.full_run_reason(self.store, fam, "1.2.4", "bbb"))
        R.record_full_run(self.store, fam, "image+FLAG=0", "F2", self.c1, "1.2.4", env_fp="bbb")
        self.assertIsNone(R.full_run_reason(self.store, fam, "1.2.5", "bbb"))
        self.assertEqual([r["run"] for r in self.store.full_runs(fam, "1.2")], ["F1", "F2"])
        self.assertEqual(self.store.env_last(fam)["env_fp"], "bbb")

    def test_transition_runs_without_fingerprint_count_until_another_environment_is_known(self):
        fam = "image"
        R.record_full_run(self.store, fam, "image", "F0", self.c1, "2.0.1")
        self.assertIsNone(R.full_run_reason(self.store, fam, "2.0.2", "aaa"))
        self.store.env_note(fam, "zzz", run="x")
        self.assertIn("environment changed", R.full_run_reason(self.store, fam, "2.0.2", "aaa"))

    def test_everything_reuses_nothing(self):
        self.ingest_full(self.c1)
        commit = git(self.repo, "rev-parse", "HEAD")
        deps = R.Dependencies(self.repo, commit)
        plan = R.plan(self.store, self.env, commit, deps.tests, deps, everything=True)
        self.assertEqual(sorted(plan.whole), ALL_FILES)
        self.assertEqual(plan.green, {})
        self.assertEqual(plan.deselect, [])

    def test_backfill_from_the_run_table(self):
        self.write("VERSION", "3.4.5\n")
        c = self.commit("version")
        self.store.run_begin("big", self.env, c)
        self.store.run_end("big", "green", {"new": 400, "reused": 0, "red": 0, "env_fp": "aaa", "image": "sha256:a"})
        self.store.run_begin("small", self.env, c)
        self.store.run_end("small", "green", {"new": 5, "reused": 0, "red": 0})
        self.store.run_begin("partial", self.env, c)
        self.store.run_end("partial", "green", {"new": 400, "reused": 10, "red": 0})
        self.store.run_begin("red", self.env, c)
        self.store.run_end("red", "red", {"new": 400, "reused": 0, "red": 2})
        self.store.run_begin("other", "elsewhere/x", c)
        self.store.run_end("other", "green", {"new": 400, "reused": 0, "red": 0})
        self.assertEqual(R.backfill_full_runs(self.store, self.repo, "image"), [("big", "3.4.5")])
        self.assertEqual(R.backfill_full_runs(self.store, self.repo, "image"), [])   # idempotent
        row = self.store.full_runs("image")[0]
        self.assertEqual((row["minor"], row["env_fp"], row["source"], row["total"]), ("3.4", "aaa", "backfill", 400))
        self.assertIsNone(R.full_run_reason(self.store, "image", "3.4.9", "aaa"))

    def test_cli_full_runs_and_backfill(self):
        self.write("VERSION", "1.0.0\n")
        c = self.commit("version")
        self.store.run_begin("big", self.env, c)
        self.store.run_end("big", "green", {"new": 250, "reused": 0, "red": 0})
        env = dict(os.environ, AIWERKSTATT_RESULT_STORE_DIR=self.dir)
        tool = os.path.join(HERE, "result_store.py")
        p = subprocess.run([sys.executable, tool, "backfill-full-runs", "--project", "demo", "--repo", self.repo, "--family", "image"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("backfilled: big (1.0.0)", p.stdout)
        p = subprocess.run([sys.executable, tool, "full-runs", "--project", "demo", "--family", "image"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("1.0.0", p.stdout)
        self.assertIn("backfill", p.stdout)

    def test_minor_and_family_helpers(self):
        self.assertEqual(R.minor_of("1.22.3"), "1.22")
        self.assertEqual(R.minor_of("1.2.3-rc1"), "1.2")
        self.assertIsNone(R.minor_of("abc"))
        self.assertIsNone(R.minor_of(None))
        self.assertEqual(R.family_of("profile/mode+A=1+B=2"), "profile/mode")


if __name__ == "__main__":
    unittest.main()
