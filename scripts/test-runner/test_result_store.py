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


if __name__ == "__main__":
    unittest.main()
