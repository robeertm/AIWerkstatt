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


GATE_OK = ("noise\nGATE: GREEN\nSUMMARY (version 1.2.3, commit abc1234d, image ghcr.io/owner/app:1.2.3)\n")
REL = {"repo": "owner/app", "image": "ghcr.io/owner/app", "identity": "chain",
       "queue_dir": "@Q@", "broker_status": "@S@", "gate_logs": "@G@", "clone": "/nonexistent/clone"}
PROOF = {"ok": True, "reason": "OK", "tag": "v1.2.3", "tag_commit": "abc", "digest": "sha256:aa",
         "latest_digest": "sha256:aa", "latest_same": True}


class GateBase(Base):
    def setUp(self):
        super().setUp()
        self.q = os.path.join(self.root, "queue")
        self.g = os.path.join(self.root, "gates")
        self.st = os.path.join(self.root, "broker-status.json")
        os.makedirs(self.q)
        os.makedirs(self.g)
        self.proof = {"ok": False, "reason": "tag v1.2.3 missing"}
        self.main = ("1.2.3", "abc1234d" + "0" * 32)

    def rel(self, **kw):
        d = {k: v.replace("@Q@", self.q).replace("@S@", self.st).replace("@G@", self.g) if isinstance(v, str) else v
             for k, v in REL.items()}
        d.update(kw)
        return d

    def chain(self, tasks=None, **kw):
        tasks = tasks or [{"id": "a", "track": "x", "release": True}, {"id": "b", "track": "x", "depends_on": ["a"]}]
        ch = self.make(plan(tasks, release=self.rel(), **kw))
        ch.evidence = lambda repo, image, ver: dict(self.proof)
        ch.main_state = lambda: self.main
        return ch

    def log(self, text=GATE_OK, name="gate.log", age_h=0):
        path = os.path.join(self.g, name)
        with open(path, "w") as f:
            f.write(text)
        old = self.clock.now() - age_h * 3600
        os.utime(path, (old, old))
        return name

    def req(self):
        return os.path.join(self.q, "app.req")


class RealClockGate(GateBase):
    def setUp(self):
        super().setUp()
        self.clock = type("C", (), {"now": staticmethod(lambda: __import__("time").time())})()


class GateLog(RealClockGate):
    def check(self, text=GATE_OK, **kw):
        self.log(text, **kw)
        return chain.check_gate_log(self.g, "gate.log", self.clock.now(), 48, "ghcr.io/owner/app")

    def test_green(self):
        ok, why, info = self.check()
        self.assertTrue(ok, why)
        self.assertEqual((info["version"], info["commit"]), ("1.2.3", "abc1234d"))

    def test_rejections(self):
        s = GATE_OK
        cases = [
            (s.replace("GATE: GREEN", "GATE: RED"), "red"),
            (s + "GATE: RED\n", "red"),
            (s.replace("GATE: GREEN\n", ""), "GREEN"),
            ("GATE: GREEN\n", "SUMMARY"),
            (s + "SUMMARY (version 1.2.4, commit abc1234d, image ghcr.io/owner/app:1.2.4)\n", "several"),
            (s.replace("image ghcr.io/owner/app:1.2.3", "image ghcr.io/owner/app:latest"), "published"),
            (s.replace("image ghcr.io/owner/app:1.2.3", "image local/app:1.2.3"), "published"),
            (s.replace("commit abc1234d", "commit zz"), "commit"),
            ("GATE: GREEN\nSUMMARY (broken\n", "readable"),
        ]
        for text, word in cases:
            ok, why, _ = self.check(text)
            self.assertFalse(ok, text)
            self.assertIn(word, why)

    def test_old_symlink_names_size(self):
        ok, why, _ = self.check(age_h=49)
        self.assertFalse(ok)
        self.assertIn("older", why)
        self.log()
        os.symlink(os.path.join(self.g, "gate.log"), os.path.join(self.g, "link.log"))
        self.assertIn("symlink", chain.check_gate_log(self.g, "link.log", self.clock.now(), 48, "ghcr.io/owner/app")[1])
        for bad in ("../x", "/etc/passwd", "a/b", "", ".hidden", None):
            self.assertFalse(chain.check_gate_log(self.g, bad, self.clock.now(), 48, "ghcr.io/owner/app")[0])
        self.assertIn("not found", chain.check_gate_log(self.g, "nope.log", self.clock.now(), 48, "ghcr.io/owner/app")[1])
        self.log(GATE_OK + "x" * (2 * 1024 * 1024 + 1))
        self.assertIn("2 MB", chain.check_gate_log(self.g, "gate.log", self.clock.now(), 48, "ghcr.io/owner/app")[1])
        os.mkdir(os.path.join(self.g, "dir"))
        self.assertIn("regular", chain.check_gate_log(self.g, "dir", self.clock.now(), 48, "ghcr.io/owner/app")[1])


class Release(GateBase):
    def setUp(self):
        super().setUp()
        self.ch = self.chain()
        self.ch.fill()
        self.ch.ingest("a", self.result("a"))

    def test_request_is_written_once(self):
        rc, text = self.ch.release("a", self.log())
        self.assertEqual(rc, 0, text)
        with open(self.req()) as f:
            self.assertEqual(json.load(f), {"mode": "release", "by": "chain", "gate": "green", "gate_log": "gate.log"})
        self.assertEqual([n for n in os.listdir(self.q)], ["app.req"])        # no temp leftovers
        rc, text = self.ch.release("a", "gate.log")
        self.assertEqual(rc, 0)
        self.assertIn("already requested", text)
        self.assertEqual(self.ch.load()["releases"]["1.2.3"]["state"], "requested")
        self.assertEqual(self.ch.fill(), [])                                  # b waits for the release

    def test_already_out_requests_nothing(self):
        self.proof = dict(PROOF)
        rc, text = self.ch.release("a", self.log())
        self.assertEqual(rc, 0)
        self.assertIn("already out", text)
        self.assertFalse(os.path.exists(self.req()))
        self.assertEqual(self.ch.load()["releases"]["1.2.3"]["state"], "published")
        self.assertEqual(self.ch.fill(), ["b"])                               # the release step is done

    def test_waiting_request_file_blocks(self):
        open(self.req(), "w").close()
        rc, text = self.ch.release("a", self.log())
        self.assertEqual(rc, 1)
        self.assertIn("unprocessed", text)

    def test_only_release_tasks_and_known_ids(self):
        self.log()
        self.assertEqual(self.ch.release("b", "gate.log")[0], 1)
        self.assertEqual(self.ch.release("zz", "gate.log")[0], 1)
        plain = self.make(plan([{"id": "a", "track": "x", "release": True}]))
        self.assertEqual(plain.release("a", "gate.log")[0], 1)                # no release block
        self.assertFalse(os.path.exists(self.req()))

    def test_fail_closed(self):
        name = self.log()
        self.main = (None, None)
        self.assertIn("not readable", self.ch.release("a", name)[1])
        self.main = ("1.2.4", "abc1234d" + "0" * 32)
        self.assertIn("run the gate again", self.ch.release("a", name)[1])
        self.main = ("1.2.3", "ffffffff" + "0" * 32)
        self.assertIn("commit", self.ch.release("a", name)[1])
        self.assertEqual(self.ch.release("a", self.log(GATE_OK.replace("GREEN", "RED"), "red.log"))[0], 1)
        self.assertEqual(self.ch.release("a", "missing.log")[0], 1)
        self.assertFalse(os.path.exists(self.req()))

    def test_release_off_holds_but_building_goes_on(self):
        open(os.path.join(self.root, "RELEASE_OFF"), "w").close()
        rc, text = self.ch.release("a", self.log())
        self.assertEqual(rc, 3)
        self.assertFalse(os.path.exists(self.req()))
        self.assertEqual(self.ch.load()["releases"]["1.2.3"]["state"], "held")
        self.assertTrue(self.ch.status()["release_off"])
        self.assertEqual(self.ch.fill(), [])                                  # b still waits for the step ...
        self.ch.manual_done("release:a")
        self.assertEqual(self.ch.fill(), ["b"])                               # ... and building goes on afterwards
        os.remove(os.path.join(self.root, "RELEASE_OFF"))
        self.assertEqual(self.ch.release("a", "gate.log")[0], 0)              # held is not requested: now it goes

    def test_hard_kill_stops_release(self):
        name = self.log()
        open(os.path.join(self.root, "KILL"), "w").close()
        with self.assertRaises(chain.Killed):
            self.ch.release("a", name)
        self.assertFalse(os.path.exists(self.req()))

    def test_verify_publishes_and_resolves_steps(self):
        self.ch.release("a", self.log())
        self.assertEqual(self.ch.verify()[0]["state"], "requested")
        self.proof = dict(PROOF)
        self.assertEqual(self.ch.verify("1.2.3")[0]["state"], "published")
        s = self.ch.load()
        self.assertEqual(s["releases"]["1.2.3"]["proof"]["digest"], "sha256:aa")
        self.assertTrue(s["manual"]["release:a"]["done"])
        self.assertEqual(self.ch.fill(), ["b"])

    def test_latest_mismatch_raises_alert(self):
        self.ch.release("a", self.log())
        self.proof = dict(PROOF, latest_same=False)
        self.ch.verify()
        self.assertIn("latest:1.2.3", self.ch.status()["alerts"])

    def test_broker_refusal_is_failed_and_alerted(self):
        self.ch.release("a", self.log())
        with open(self.st, "w") as f:
            json.dump({"version": "1.2.3", "state": "failed", "error": "tag exists",
                       "ts": chain.iso(self.clock.now() + 5)}, f)
        self.assertEqual(self.ch.verify()[0]["state"], "failed")
        self.assertIn("tag exists", self.ch.status()["alerts"]["release:1.2.3"])
        os.remove(self.req())                                                 # the broker took the old request
        self.assertEqual(self.ch.release("a", "gate.log")[0], 0)              # a new green gate may ask again
        self.assertEqual(self.ch.load()["releases"]["1.2.3"]["state"], "requested")

    def test_old_broker_answer_is_ignored(self):
        self.ch.release("a", self.log())
        with open(self.st, "w") as f:
            json.dump({"version": "1.2.3", "state": "failed", "ts": chain.iso(self.clock.now() - 7200)}, f)
        self.assertEqual(self.ch.verify()[0]["state"], "requested")

    def test_request_without_answer_alerts_after_three_hours(self):
        self.ch.release("a", self.log())
        self.clock.t += 3 * 3600 + 5
        self.ch.verify()
        self.assertIn("release:1.2.3", self.ch.status()["alerts"])

    def test_check_measures_open_requests(self):
        self.ch.release("a", self.log())
        self.proof = dict(PROOF)
        self.ch.check()
        self.assertEqual(self.ch.load()["releases"]["1.2.3"]["state"], "published")

    def test_result_release_field(self):
        chain.validate_result({"id": "a", "tests": "green", "gate": "green", "release": "held"}, "a")
        with self.assertRaises(chain.ChainError):
            chain.validate_result({"id": "a", "tests": "green", "gate": "green", "release": "yes"}, "a")


class ExternalRelease(GateBase):
    def make_chain(self):
        ch = self.chain([{"id": "c", "track": "y", "depends_on": ["ext:lib"]}],
                        external={"lib": "library 2.4.0"},
                        extern_release={"lib": {"repo": "owner/lib", "version": "2.4.0"}})
        self.calls = []
        ch.evidence = lambda repo, image, ver: (self.calls.append((repo, image, ver)), dict(self.proof))[1]
        return ch

    def test_resolves_itself_and_is_throttled(self):
        ch = self.make_chain()
        self.assertEqual(ch.fill(), [])
        self.assertEqual(ch.check_external_releases(), [])
        self.assertEqual(ch.check_external_releases(), [])                    # within 10 minutes: not asked again
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], ("owner/lib", "ghcr.io/owner/lib", "2.4.0"))
        self.clock.t += 601
        self.proof = dict(PROOF)
        self.assertEqual(ch.check_external_releases(), ["lib"])
        self.assertEqual(ch.fill(), ["c"])
        self.assertIn("lib", ch.status()["external_auto"])
        self.assertEqual(ch.check_external_releases(), [])                    # done stays done

    def test_check_resolves_and_manual_done_stays_as_emergency(self):
        ch = self.make_chain()
        ch.check()
        self.assertEqual(ch.status()["external_pending"][0]["key"], "lib")
        ch.manual_done("lib")
        self.assertEqual(ch.fill(), ["c"])

    def test_plan_rules(self):
        base = {"tasks": [{"id": "a", "track": "x", "title": "t", "prompt": "p"}], "external": {"k": "d"}}
        for bad in ({"k": {"repo": "x", "version": "1.0.0"}}, {"k": {"repo": "o/r", "version": "1.0"}},
                    {"other": {"repo": "o/r", "version": "1.0.0"}}, {"k": {"repo": "o/r"}}):
            with self.assertRaises(chain.ChainError):
                chain.validate_plan(dict(base, extern_release=bad))
        for bad in ({"repo": "o/r"}, {"repo": "o/r", "queue_dir": "rel", "clone": "/c"},
                    {"repo": "o/r", "queue_dir": "/q", "clone": "/c", "unknown": 1},
                    {"repo": "o/r", "queue_dir": "/q", "clone": "/c", "version_file": "../x"}):
            with self.assertRaises(chain.ChainError):
                chain.validate_plan(dict(base, release=bad))
        ok = chain.validate_plan(dict(base, release={"repo": "o/r", "queue_dir": "/q", "clone": "/c"}))
        self.assertEqual((ok["release"]["identity"], ok["release"]["slug"], ok["release"]["image"]),
                         ("chain", "r", "ghcr.io/o/r"))


class StepSorting(Base):
    def run_steps(self, steps, release_task=False, release_block=None, **res):
        extra = {"release": release_block} if release_block else {}
        p = plan([{"id": "a", "track": "x", "release": release_task}], **extra)
        ch = self.make(p)
        ch.fill()
        ch.ingest("a", self.result("a", manual_steps=steps, **res))
        return ch, ch.status()

    def test_only_real_reasons_wait_for_a_human(self):
        ch, st = self.run_steps(["Sign the collector manifest with the signing key", "Confirm with a passkey",
                                 "Check it in the real desktop browser", "Decide about the new name",
                                 "Write a note into the vault"])
        self.assertEqual(len(st["waiting_for_human"]), 3)
        self.assertTrue(all(h["reason"] for h in st["waiting_for_human"]))
        self.assertEqual([c["text"] for c in st["waiting_for_coordinator"]], ["Decide about the new name"])
        self.assertEqual(st["chain_open"], [])
        self.assertEqual(len(st["taken_over"]), 1)                            # the note went to the inbox
        self.assertNotIn("vault", json.dumps(st["manual_pending"]))

    def test_release_steps(self):
        rel = {"repo": "o/r", "queue_dir": "/q", "clone": "/c"}
        ch, st = self.run_steps(["Release the new version"], True, rel)
        self.assertEqual({c["key"] for c in st["chain_open"]}, {"a:1", "release:a"})
        os.remove(os.path.join(self.root, "state.json"))
        ch, st = self.run_steps(["Release the new version"], True, rel, release="requested")
        self.assertEqual({c["key"] for c in st["chain_open"]}, {"release:a"})
        os.remove(os.path.join(self.root, "state.json"))
        ch, st = self.run_steps(["Tag the new version"])                       # no release path: coordinator
        self.assertEqual(len(st["waiting_for_coordinator"]), 1)
        self.assertEqual(st["waiting_for_human"], [])

    def test_plan_may_add_reasons(self):
        p = plan([{"id": "a", "track": "x"}], human_reasons=[{"pattern": "hardware token", "reason": "token is plugged in"}])
        ch = self.make(p)
        ch.fill()
        ch.ingest("a", self.result("a", manual_steps=["Touch the hardware token"]))
        self.assertEqual(ch.status()["waiting_for_human"][0]["reason"], "token is plugged in")

    def test_old_entries_are_sorted_on_loading(self):
        ch = self.make(plan([{"id": "a", "track": "x"}]))
        ch.fill()
        s = ch.load()
        s["manual"]["a:1"] = {"text": "Confirm with a passkey", "task": "a", "done": False}
        s["manual"]["a:2"] = {"text": "Add a note to the vault", "task": "a", "done": False}
        ch.save(s)
        st = ch.status()
        self.assertEqual([h["key"] for h in st["waiting_for_human"]], ["a:1"])
        self.assertEqual([h["key"] for h in st["waiting_for_coordinator"]], ["a:2"])

    def test_status_text_says_nothing_waits(self):
        p = plan([{"id": "a", "track": "x"}])
        lines = []
        chain.main(["--root", self.root, "--plan", self._write(p), "status"], out=lines.append, api=self.api, clock=self.clock)
        self.assertIn("  waiting for the main workstation: nothing", lines)

    def _write(self, p):
        path = os.path.join(self.root, "p.json")
        with open(path, "w") as f:
            json.dump({"tasks": [{"id": "a", "track": "x", "title": "t", "prompt": "p"}]}, f)
        return path


class GateCli(GateBase):
    def test_release_command_exit_codes(self):
        raw = {"tasks": [{"id": "a", "track": "x", "title": "t", "prompt": "p", "release": True}], "release": self.rel()}
        path = os.path.join(self.root, "plan.json")
        with open(path, "w") as f:
            json.dump(raw, f)
        lines = []
        err = io.StringIO()
        self.log()

        def run(*args):
            with redirect_stderr(err):
                return chain.main(["--root", self.root, "--plan", path] + list(args), out=lines.append,
                                  api=self.api, clock=self.clock)
        with self.assertRaises(SystemExit):
            run("release", "--id", "a")                                       # --gate-log is required
        self.assertEqual(run("release", "--id", "a", "--gate-log", "gate.log"), 1)   # clone does not exist: fail closed
        self.assertFalse(os.path.exists(self.req()))
        self.assertEqual(run("verify", "9.9"), 2)


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
