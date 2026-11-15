"""Tests for scripts/chain/chain.py with a fake API and a fake clock.

Run: python3 -m unittest discover -s scripts/chain
"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


class Api(chain.FakeApi):
    def __init__(self):
        super().__init__()
        self.comments = []
        self.threads = {}
        self.fail = None

    def create(self, title, request, model="", effort=""):
        if self.fail:
            raise self.fail
        self.requests = getattr(self, "requests", {})
        n = super().create(title, request, model, effort)
        self.requests[n] = request
        return n

    def get(self, thread):
        return self.threads.get(thread, {"status": "working", "timeline": []})

    def comment(self, thread, text):
        self.comments.append((thread, text))


def plan(tasks, **kw):
    base = []
    for t in tasks:
        d = {"title": "T " + t["id"], "prompt": "do " + t["id"]}
        d.update(t)
        base.append(d)
    d = {"tasks": base}
    d.update(kw)
    return chain.validate_plan(d)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="chain-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = chain.FakeClock()
        self.api = Api()

    def make(self, p):
        return chain.Chain(p, self.root, self.api, self.clock, "[c] ")

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def result(self, tid, **kw):
        d = {"id": tid, "branch": "work/x", "commit": "abc1234", "tests": "green", "gate": "green",
             "manual_steps": []}
        d.update(kw)
        path = os.path.join(self.root, "r-%s.json" % tid)
        with open(path, "w") as f:
            json.dump(d, f)
        return path


class PlanRules(unittest.TestCase):
    def bad(self, raw, text):
        with self.assertRaises(chain.ChainError) as cm:
            chain.validate_plan(raw)
        self.assertIn(text, str(cm.exception))

    def test_limits_only_lower(self):
        p = plan([{"id": "a", "track": "t"}], limits={"per_day": 99, "per_track": 5, "total": 1})
        self.assertEqual(p["limits"], {"per_day": 4, "per_track": 1, "total": 1})
        self.assertEqual(plan([{"id": "a", "track": "t"}])["limits"], chain.HARD_LIMITS)

    def test_rejections(self):
        t = {"id": "a", "track": "t", "title": "x", "prompt": "p"}
        self.bad({"tasks": []}, "non-empty")
        self.bad({"tasks": [dict(t, folder="/x")]}, "unknown field")
        self.bad({"tasks": [t, t]}, "duplicate")
        self.bad({"tasks": [dict(t, depends_on=["zz"])]}, "unknown dependency")
        self.bad({"tasks": [dict(t, depends_on=["a"])]}, "cycle")
        self.bad({"tasks": [t], "order": ["b"]}, "order")
        self.bad({"tasks": [t], "limits": {"weekly": 1}}, "limits")
        self.bad({"tasks": [t], "limits": {"total": 0}}, "limit total")
        self.bad({"tasks": [dict(t, hours=0)]}, "hours")
        self.bad({"tasks": [dict(t, depends_on=["ext:nope"])]}, "external")

    def test_example_plan_valid(self):
        chain.load_plan(os.path.join(HERE, "example-plan.json"))


class ResultRules(unittest.TestCase):
    def test_valid_and_invalid(self):
        ok = {"id": "a", "branch": "work/a", "commit": "abc1234", "tests": "green", "gate": "open",
              "manual_steps": ["do x"]}
        self.assertEqual(chain.validate_result(ok, "a")["gate"], "open")
        for k, v in (("id", "b"), ("tests", "maybe"), ("gate", "x"), ("commit", "zz"),
                     ("branch", "a/../b"), ("manual_steps", "no"), ("manual_steps", [""]),
                     ("extra", 1)):
            with self.assertRaises(chain.ChainError, msg=k):
                chain.validate_result(dict(ok, **{k: v}), "a")


class Flow(Base):
    def test_dependencies_and_limits(self):
        p = plan([{"id": "a", "track": "x"}, {"id": "b", "track": "x"}, {"id": "c", "track": "y"},
                  {"id": "d", "track": "z", "depends_on": ["a"]}])
        ch = self.make(p)
        self.assertEqual(ch.fill(), ["a", "c"])           # one per track, two in total
        self.assertEqual(ch.fill(), [])
        self.assertEqual(ch.ingest("a", self.result("a")), "done")
        self.assertEqual(ch.fill(), ["b"])                # b before d (plan order), total 2
        self.assertTrue(self.api.titles[1].startswith("[c] a: "))
        self.assertIn("result file", self.api.requests[1])

    def test_per_day_limit_and_next_day(self):
        p = plan([{"id": "t%d" % i, "track": "t%d" % i} for i in range(6)], limits={"total": 4})
        ch = self.make(p)
        started = []
        for _ in range(3):
            got = ch.fill()
            for tid in got:
                ch.ingest(tid, self.result(tid))
            started += got
        self.assertEqual(len(started), 4)                 # hard ceiling per day
        self.clock.t += 86400
        self.assertEqual(len(ch.fill()), 2)

    def test_red_three_times_blocks(self):
        ch = self.make(plan([{"id": "a", "track": "x"}, {"id": "b", "track": "x", "depends_on": ["a"]}]))
        ch.fill()
        self.assertEqual(ch.ingest("a", self.result("a", tests="red")), "red 1/3")
        self.assertEqual(ch.ingest("a", self.result("a", gate="red")), "red 2/3")
        self.assertEqual(len(self.api.comments), 2)
        self.assertEqual(ch.ingest("a", self.result("a", tests="red")), "blocked")
        self.assertEqual(ch.fill(), [])
        st = ch.status()
        self.assertEqual(st["tasks"]["a"]["state"], "blocked")
        self.assertTrue(any("blocked: a" in h for h in st["hints"]))

    def test_gate_open_does_not_advance_or_count(self):
        ch = self.make(plan([{"id": "a", "track": "x"}, {"id": "b", "track": "x", "depends_on": ["a"]}]))
        ch.fill()
        self.assertEqual(ch.ingest("a", self.result("a", gate="open")), "gate open")
        self.assertEqual(ch.fill(), [])
        self.assertEqual(ch.load()["tasks"]["a"]["red"], 0)

    def test_invalid_result_changes_nothing(self):
        ch = self.make(plan([{"id": "a", "track": "x"}]))
        ch.fill()
        before = self.read(ch.state_path)
        with self.assertRaises(chain.ChainError):
            ch.ingest("a", self.result("a", commit="not hex"))
        self.assertEqual(self.read(ch.state_path), before)
        with self.assertRaises(chain.ChainError):
            ch.ingest("b", self.result("a"))

    def test_manual_steps_external_release_and_optional(self):
        p = plan([{"id": "a", "track": "x", "release": True},
                  {"id": "b", "track": "x", "depends_on": ["a"]},
                  {"id": "c", "track": "y", "depends_on": ["ext:key"]},
                  {"id": "o", "track": "z", "optional": True}],
                 external={"key": "needs a human"})
        ch = self.make(p)
        self.assertEqual(ch.fill(), ["a"])                # c waits for the external, o is optional
        ch.ingest("a", self.result("a", manual_steps=["flip a switch"]))
        self.assertEqual(ch.fill(), [])                   # b waits for the human steps
        self.assertEqual({m["key"] for m in ch.status()["manual_pending"]}, {"a:1", "release:a"})
        ch.manual_done("a:1")
        self.assertEqual(ch.fill(), [])
        ch.manual_done("release:a")
        self.assertEqual(ch.fill(), ["b"])
        ch.manual_done("key")
        self.assertEqual(ch.fill(), ["c"])
        with self.assertRaises(chain.ChainError):
            ch.approve("a")                               # not optional
        ch.approve("o")
        ch.ingest("b", self.result("b"))
        self.assertEqual(ch.fill(), ["o"])
        with self.assertRaises(chain.ChainError):
            ch.manual_done("nothing")

    def test_kill_switch_and_pause(self):
        ch = self.make(plan([{"id": "a", "track": "x"}, {"id": "b", "track": "y"}]))
        open(os.path.join(self.root, "PAUSE"), "w").close()
        self.assertEqual(ch.fill(), [])
        os.remove(os.path.join(self.root, "PAUSE"))
        ch.fill()
        open(os.path.join(self.root, "KILL"), "w").close()
        for call in (lambda: ch.fill(), lambda: ch.check(), lambda: ch.manual_done("a"),
                     lambda: ch.ingest("a", self.result("a"))):
            with self.assertRaises(chain.Killed):
                call()
        self.assertEqual(ch.load()["tasks"]["a"]["state"], "running")

    def test_api_error_leaves_task_pending(self):
        ch = self.make(plan([{"id": "a", "track": "x"}]))
        self.api.fail = chain.ApiError(429, "slow down")
        self.assertEqual(ch.fill(), [])
        self.assertEqual(ch.load()["tasks"]["a"]["state"], "pending")
        self.assertEqual(ch.load()["created"], [])
        self.assertTrue(any("could not start a" in h for h in ch.hints))
        self.api.fail = None
        self.assertEqual(ch.fill(), ["a"])

    def test_status_file_is_valid_json(self):
        ch = self.make(plan([{"id": "a", "track": "x"}]))
        ch.fill()
        with open(ch.status_path) as f:
            self.assertEqual(json.load(f)["tasks"]["a"]["state"], "running")
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith(".tmp-")], [])


class SelfHealing(Base):
    def setUp(self):
        super().setUp()
        self.ch = self.make(plan([{"id": "a", "track": "x", "hours": 1}]))
        self.ch.fill()

    def test_finished_without_result_one_follow_up(self):
        self.api.threads[1] = {"status": "done", "timeline": []}
        self.ch.check()
        self.ch.check()
        self.assertEqual(len(self.api.comments), 1)
        self.assertIn("no result file", self.api.comments[0][1])

    def test_result_file_is_picked_up(self):
        os.makedirs(os.path.join(self.root, "results"))
        os.replace(self.result("a"), self.ch.result_path("a"))
        self.assertEqual(self.ch.check(), [("a", "done")])
        self.assertEqual(self.api.comments, [])

    def test_stall_after_twice_the_hours_once(self):
        self.api.threads[1] = {"status": "working", "timeline": [
            {"created_at": chain.iso(self.clock.t)}]}
        self.clock.t += 3600 * 1.9
        self.ch.check()
        self.assertEqual(self.api.comments, [])
        self.clock.t += 3600 * 0.2
        self.ch.check()
        self.ch.check()
        self.assertEqual(len(self.api.comments), 1)

    def test_failed_task_is_a_hint_not_a_nudge(self):
        self.api.threads[1] = {"status": "failed", "timeline": []}
        self.ch.check()
        self.assertEqual(self.api.comments, [])
        self.assertTrue(any("needs a human" in h for h in self.ch.hints))


class Simulation(unittest.TestCase):
    def test_example_plan(self):
        lines = []
        left = chain.simulate(chain.load_plan(os.path.join(HERE, "example-plan.json")), lines.append)
        self.assertEqual(left, ["ui-polish"])             # the optional task stays off
        starts = [ln for ln in lines if ln.split(": ")[1].startswith("start")]
        self.assertEqual(len(starts), 7)
        self.assertTrue(lines[-1].startswith("finished"))

    def test_leaves_nothing_behind(self):
        tmp = tempfile.gettempdir()
        before = {n for n in os.listdir(tmp) if n.startswith("chain-sim-")}
        chain.simulate(chain.load_plan(os.path.join(HERE, "example-plan.json")), lambda s: None)
        self.assertEqual({n for n in os.listdir(tmp) if n.startswith("chain-sim-")}, before)


class Cli(Base):
    def run_cli(self, *args):
        lines = []
        err = io.StringIO()
        with redirect_stderr(err):
            code = chain.main(["--root", self.root, "--plan", os.path.join(HERE, "example-plan.json")]
                              + list(args), out=lines.append, api=self.api, clock=self.clock)
        return code, lines, err.getvalue()

    def test_next_status_pause_and_exit_codes(self):
        code, out, _ = self.run_cli("next")
        self.assertEqual((code, out), (0, ["started docs-outline", "started api-schema"]))
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads("\n".join(out))["tasks"]["docs-outline"]["state"], "running")
        self.assertEqual(self.run_cli("pause")[0], 0)
        self.assertEqual(self.run_cli("resume")[0], 0)
        self.assertEqual(self.run_cli("next", "--done", "docs-outline")[0], 2)    # no --result
        self.assertEqual(self.run_cli("manual-done", "nothing")[0], 2)
        self.assertEqual(self.run_cli("--dry-run", "check")[0], 2)
        open(os.path.join(self.root, "KILL"), "w").close()
        self.assertEqual(self.run_cli("next")[0], 3)
        self.assertEqual(self.run_cli("status")[0], 0)

    def test_dry_run_next_touches_nothing(self):
        code, out, _ = self.run_cli("--dry-run", "next")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "state.json")))
        self.assertEqual(self.api.titles, {})

    def test_simulate_command(self):
        code, out, _ = self.run_cli("simulate")
        self.assertEqual(code, 0)
        self.assertTrue(out[-1].startswith("finished"))


class TokenFile(unittest.TestCase):
    def test_permissions_and_empty(self):
        d = tempfile.mkdtemp(prefix="chain-tok-")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "token")
        api = chain.Api("http://127.0.0.1:1", path, "p", "x")
        with self.assertRaises(chain.ChainError):
            api._token()                                   # missing
        with open(path, "w") as f:
            f.write("secret-value\n")
        os.chmod(path, 0o644)
        with self.assertRaises(chain.ChainError) as cm:
            api._token()
        self.assertNotIn("secret-value", str(cm.exception))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(api._token(), "secret-value")
        with open(path, "w") as f:
            f.write("\n")
        with self.assertRaises(chain.ChainError):
            api._token()


if __name__ == "__main__":
    unittest.main()
