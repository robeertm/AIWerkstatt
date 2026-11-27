"""AI-free review runner (build/review split, part 1): hand-off, phases, limits per target, green cache, emergency
stop, restart recovery, finding format, mode switch, API and the Docker executor's container settings.

Run: cd backend && python3 -m unittest tests.test_review_runner
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-reviewrun-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-reviewrun-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import review_jobs as rj  # noqa: E402
import review_runner as rr  # noqa: E402

C1 = "a" * 40
C2 = "b" * 40
PROJECT = {"id": "demo-app"}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeHandle:
    def __init__(self, ex, kind, job, step, log_path):
        self.ex, self.kind, self.job, self.step, self.log_path = ex, kind, job, step, log_path
        self.rc = None
        self.killed = False
        self.closed = False
        self.out = ex.clock()
        with open(log_path, "a") as f:
            f.write("%s started\n" % kind)

    def poll(self):
        return self.rc

    def last_output(self):
        return self.out

    def kill(self):
        self.killed = True
        self.rc = 137

    def close(self):
        self.closed = True

    def finish(self, rc, text=""):
        with open(self.log_path, "a") as f:
            f.write(text)
        self.rc = rc


class FakeExecutor:
    available = True

    def __init__(self, clock):
        self.clock = clock
        self.handles = []
        self.cleaned = []
        self.resets = []

    def prepare(self, job, log_path):
        h = FakeHandle(self, "prepare", job, None, log_path)
        self.handles.append(h)
        return h

    def start(self, job, step, target, log_path):
        h = FakeHandle(self, "step", job, step, log_path)
        h.target = target
        self.handles.append(h)
        return h

    def cleanup(self, job):
        self.cleaned.append(job["id"])

    def reset(self, job):
        self.resets.append(job["id"])

    def live(self):
        return [h for h in self.handles if h.rc is None]


def req(tid, commit=C1, checks=None, **kw):
    d = {"thread_id": tid, "commit": commit, "checks": checks if checks is not None else ["make test"]}
    d.update(kw)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="aiw-review-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.env = mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_MODE": "on"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = Clock()
        self.ex = FakeExecutor(self.clock)
        self.runner = rr.ReviewRunner(self.ex, data=self.data, clock=self.clock)

    def hand(self, tid, commit=C1, checks=None, **kw):
        job, created, err = rj.hand_over(req(tid, commit, checks, **kw), PROJECT, "tester", data=self.data,
                                         now=self.clock())
        self.assertIsNone(err)
        return job

    def job(self, jid):
        return rj.read_job(jid, self.data)

    def finish_prepare(self, rc=0):
        h = [x for x in self.ex.live() if x.kind == "prepare"][-1]
        h.finish(rc, "prepared\n" if rc == 0 else "fatal: bad object\n")
        return h

    def finish_step(self, rc=0, nr=None):
        cands = [x for x in self.ex.live() if x.kind == "step" and (nr is None or x.step["nr"] == nr)]
        cands[0].finish(rc, "ok\n" if rc == 0 else "FAILED test_x\n")
        return cands[0]


class HandOver(Base):
    def test_validation(self):
        bad = [(req(1, commit="abc"), "full commit hash"),
               (req(1, checks=[]), "at least one"),
               (req(1, checks=[{"cmd": "x", "target": "gpu"}]), "unknown target"),
               (req(1, checks=["x"] * 21), "at most 20"),
               (req("x"), "thread_id"),
               (req(1, branch="feature;rm -rf"), "branch"),
               (req(1, checks=[{"cmd": ""}]), "checks[0].cmd")]
        for r, msg in bad:
            job, created, err = rj.hand_over(r, PROJECT, "t", data=self.data)
            self.assertIsNone(job)
            self.assertIn(msg, err)
        job, created, err = rj.hand_over(req(1), {"id": "../x"}, "t", data=self.data)
        self.assertEqual(err, "project not found")

    def test_job_record_and_steps(self):
        j = self.hand(7, checks=["npm test", {"cmd": "npm ci && npm run build", "target": "online"}], branch="feat/x",
                      hint="look at the queue", release="train")
        self.assertEqual(j["id"], "j7-r1")
        self.assertEqual(j["phase"], "waiting_runner")
        self.assertEqual([(s["nr"], s["target"]) for s in j["checks"]], [(1, "local"), (2, "online")])
        self.assertEqual(j["branch"], "feat/x")

    def test_same_commit_is_one_job_new_commit_next_round(self):
        a = self.hand(3)
        again, created, err = rj.hand_over(req(3), PROJECT, "t", data=self.data)
        self.assertFalse(created)
        self.assertEqual(again["id"], a["id"])
        b = self.hand(3, commit=C2)
        self.assertEqual(b["id"], "j3-r2")
        old = self.job("j3-r1")
        self.assertEqual(old["phase"], "cancelled")
        self.assertIn("superseded by j3-r2", old["run"]["cancel_reason"])

    def test_same_commit_after_rework_is_a_new_round(self):
        self.hand(4)
        rj.write_run("j4-r1", {"status": "ran", "steps": [{"nr": 1, "status": "red"}]}, self.data)
        j, err = rj.record_finding("j4-r1", {"decision": "rework", "findings": [{"expected": "tests pass"}]}, "rev",
                                   store_dir=self.data)
        self.assertIsNone(err)
        self.assertEqual(j["phase"], "rework")
        again = self.hand(4)
        self.assertEqual(again["id"], "j4-r2")

    def test_green_commit_is_not_checked_twice(self):
        self.hand(5)
        rj.write_run("j5-r1", {"status": "ran", "green": True, "steps": [{"nr": 1, "status": "green"}]}, self.data)
        rj.record_finding("j5-r1", {"decision": "green"}, "rev", store_dir=self.data)
        j, created, err = rj.hand_over(req(5), PROJECT, "t", data=self.data)
        self.assertFalse(created)
        self.assertEqual(j["phase"], "green")


class Mode(Base):
    def test_default_is_off_and_refuses(self):
        with mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_MODE": ""}):
            self.assertEqual(rj.load_mode(self.data)["mode"], "off")
            job, created, err = rj.hand_over(req(1), PROJECT, "t", data=self.data)
        self.assertIsNone(job)
        self.assertIn("off", err)

    def test_probe_limits_open_jobs(self):
        res, err = rj.set_mode("probe", "admin", probe_max=2, data=self.data)
        self.assertIsNone(err)
        self.assertEqual(res["mode"], "probe")
        self.hand(1)
        self.hand(2)
        job, created, err = rj.hand_over(req(3), PROJECT, "t", data=self.data)
        self.assertIn("at most 2", err)
        rj.cancel("j1-r1", "admin", data=self.data)
        self.assertEqual(self.hand(3)["id"], "j3-r1")

    def test_on_has_no_total_limit(self):
        rj.set_mode("on", "admin", data=self.data)
        for t in range(1, 9):
            self.hand(t)
        self.assertEqual(rj.counts(rj.list_jobs(self.data))["waiting_runner"], 8)

    def test_set_mode_validates_and_logs(self):
        self.assertIsNotNone(rj.set_mode("maybe", "a", data=self.data)[1])
        self.assertIsNotNone(rj.set_mode("probe", "a", probe_max=0, data=self.data)[1])
        self.assertIsNotNone(rj.set_mode("probe", "a", probe_max="x", data=self.data)[1])
        rj.set_mode("probe", "admin", probe_max=3, reason="first try", data=self.data)
        rj.set_mode("off", "admin", data=self.data)
        with open(rj.mode_log(self.data)) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual([(x["from"], x["to"]) for x in lines], [("on", "probe"), ("probe", "off")])
        self.assertEqual(lines[0]["reason"], "first try")
        self.assertEqual(rj.load_mode(self.data)["probe_max"], 3)   # kept when only the mode changes

    def test_broken_file_falls_back_to_environment(self):
        with open(rj.mode_file(self.data), "w") as f:
            f.write("{nope")
        self.assertEqual(rj.load_mode(self.data)["mode"], "on")
        self.assertEqual(rj.load_mode(self.data)["source"], "environment")

    def test_off_still_finishes_open_jobs(self):
        self.hand(1)
        rj.set_mode("off", "admin", data=self.data)
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        self.runner.tick()
        self.finish_step()
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(self.job("j1-r1")["phase"], "waiting_review")


class Targets(unittest.TestCase):
    def test_defaults(self):
        t, err = rj.load_targets("")
        self.assertIsNone(err)
        self.assertEqual(t["local"], {"limit": 1, "network": "none"})
        self.assertEqual(t["online"]["network"], "bridge")

    def test_custom_replaces_defaults(self):
        t, err = rj.load_targets(json.dumps({"local": {"limit": 2}, "gate": {"limit": 1, "network": "bridge",
                                                                              "cpus": "1.5", "memory": "3g",
                                                                              "image": "example/checks:1"}}))
        self.assertIsNone(err)
        self.assertEqual(sorted(t), ["gate", "local"])
        self.assertEqual(t["gate"]["cpus"], "1.5")

    def test_invalid_keeps_defaults(self):
        for raw in ("{", "[]", "{}", json.dumps({"x": {"network": "host"}}), json.dumps({"x": {"limit": 0}}),
                    json.dumps({"Bad Name": {}}), json.dumps({"x": {"memory": "lots"}}),
                    json.dumps({"x": {"cpus": "999"}}), json.dumps({"x": {"image": "a b"}})):
            t, err = rj.load_targets(raw)
            self.assertIsNotNone(err, raw)
            self.assertEqual(sorted(t), ["local", "online"])


class Runner(Base):
    def test_full_green_run(self):
        self.hand(1, checks=["make lint", "make test"])
        self.runner.tick()
        self.assertEqual(self.job("j1-r1")["phase"], "running")
        self.assertEqual([h.kind for h in self.ex.handles], ["prepare"])
        self.runner.tick()                                     # prepare still running: nothing else starts
        self.assertEqual(len(self.ex.handles), 1)
        self.finish_prepare()
        self.runner.tick()                                     # collects prepare, starts step 1
        self.assertEqual([h.kind for h in self.ex.handles], ["prepare", "step"])
        self.clock.t += 30
        self.finish_step()
        self.runner.tick()                                     # step 1 green, step 2 starts
        run = self.job("j1-r1")["run"]
        self.assertEqual(run["steps"][0]["status"], "green")
        self.assertEqual(run["steps"][0]["duration_s"], 30)
        self.assertEqual(run["steps"][1]["status"], "running")
        self.finish_step()
        self.runner.tick()
        self.runner.tick()
        j = self.job("j1-r1")
        self.assertEqual(j["phase"], "waiting_review")
        self.assertTrue(j["run"]["green"])
        self.assertIn("j1-r1", self.ex.cleaned)               # job volume removed once the runner finished
        self.assertTrue(all(h.closed for h in self.ex.handles))

    def test_red_step_does_not_stop_later_steps(self):
        self.hand(1, checks=["a", "b"])
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        self.finish_step(rc=1)
        self.runner.tick()
        self.finish_step(rc=0)
        self.runner.tick()
        self.runner.tick()
        j = self.job("j1-r1")
        self.assertEqual([s["status"] for s in j["run"]["steps"]], ["red", "green"])
        self.assertEqual(j["run"]["steps"][0]["rc"], 1)
        self.assertIn("FAILED test_x", j["run"]["steps"][0]["tail"])
        self.assertFalse(j["run"]["green"])
        self.assertEqual(j["phase"], "waiting_review")

    def run_to_end(self, jid):
        for _ in range(30):
            if self.job(jid)["phase"] != "running" and self.job(jid)["phase"] != "waiting_runner":
                return
            self.runner.tick()
            for h in self.ex.live():
                h.finish(0)

    def test_green_cache_per_commit_and_command(self):
        self.hand(1, checks=["make test", "make lint"])
        self.run_to_end("j1-r1")
        n = len(self.ex.handles)
        self.hand(2, checks=["make test", "make lint"])      # another task, same project + commit + commands
        self.runner.tick()
        self.runner.tick()
        j = self.job("j2-r1")
        self.assertEqual(j["phase"], "waiting_review")
        self.assertTrue(all(s["from_cache"] for s in j["run"]["steps"]))
        self.assertEqual(len(self.ex.handles), n)              # no prepare, no step container
        self.hand(3, checks=["make test", "make e2e"])         # only the new command runs
        self.run_to_end("j3-r1")
        steps = self.job("j3-r1")["run"]["steps"]
        self.assertTrue(steps[0].get("from_cache"))
        self.assertFalse(steps[1].get("from_cache"))

    def test_cache_ignores_other_commit_target_and_expires(self):
        self.hand(1)
        self.run_to_end("j1-r1")
        self.assertEqual(len(rj.cache_load(self.data, self.clock())), 1)
        j = self.job("j1-r1")
        other = dict(j, commit=C2)
        self.assertNotEqual(rj.cache_key(j, j["checks"][0]), rj.cache_key(other, j["checks"][0]))
        self.assertNotEqual(rj.cache_key(j, j["checks"][0]), rj.cache_key(j, dict(j["checks"][0], target="online")))
        self.assertNotEqual(rj.cache_key(j, j["checks"][0]), rj.cache_key(j, j["checks"][0], {"image": "x"}))
        self.assertEqual(rj.cache_load(self.data, self.clock() + 8 * 86400), {})
        with mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_CACHE_DAYS": "0"}):
            self.assertEqual(rj.cache_load(self.data, self.clock()), {})

    def test_red_is_never_cached(self):
        self.hand(1)
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        self.finish_step(rc=2)
        self.runner.tick()
        self.assertEqual(rj.cache_load(self.data, self.clock()), {})

    def test_limit_per_target_no_total_limit(self):
        self.hand(1, checks=["a"])
        self.hand(2, checks=["b"])
        self.hand(3, checks=[{"cmd": "c", "target": "online"}])
        self.runner.tick()
        # prepare counts against the target of the step it prepares: one local + one online
        self.assertEqual(sorted(h.job["id"] for h in self.ex.live()), ["j1-r1", "j3-r1"])
        self.assertEqual(self.runner.busy("local"), 1)
        self.assertEqual(self.runner.busy("online"), 1)
        with mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_TARGETS": json.dumps({"local": {"limit": 3}})}):
            r2 = rr.ReviewRunner(FakeExecutor(self.clock), data=tempfile.mkdtemp(prefix="aiw-review-"), clock=self.clock)
        self.addCleanup(shutil.rmtree, r2.data, True)
        for t in (1, 2, 3):
            rj.hand_over(req(t), PROJECT, "t", targets=r2.targets, data=r2.data)
        r2.tick()
        self.assertEqual(len(r2.ex.live()), 3)

    def test_rework_round_goes_first(self):
        self.hand(1)
        self.clock.t += 10
        self.hand(2)
        rj.write_run("j2-r1", {"status": "ran", "steps": [{"nr": 1, "status": "red"}]}, self.data)
        rj.record_finding("j2-r1", {"decision": "rework", "findings": [{"expected": "x"}]}, "rev", store_dir=self.data)
        self.clock.t += 10
        self.hand(2, commit=C2)                                 # round 2, handed over last
        order = [j["id"] for j in rj.runner_order(rj.list_jobs(self.data)) if j["phase"] == "waiting_runner"]
        self.assertEqual(order, ["j2-r2", "j1-r1"])
        self.runner.tick()
        self.assertEqual([h.job["id"] for h in self.ex.live()], ["j2-r2"])

    def test_emergency_stop_on_silence(self):
        self.hand(1)
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        h = self.ex.live()[0]
        self.clock.t += 44 * 60
        self.runner.tick()
        self.assertFalse(h.killed)
        h.out = self.clock.t                                   # output keeps it alive
        self.clock.t += 44 * 60
        self.runner.tick()
        self.assertFalse(h.killed)
        self.clock.t += 46 * 60
        self.runner.tick()
        self.assertTrue(h.killed)
        s = self.job("j1-r1")["run"]["steps"][0]
        self.assertEqual(s["status"], "red")
        self.assertIn("no output for 45 min", s["reason"])

    def test_emergency_stop_after_max_hours(self):
        self.hand(1)
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        h = self.ex.live()[0]
        for _ in range(13):                                     # 6.5 h with steady output
            self.clock.t += 30 * 60
            h.out = self.clock.t
            self.runner.tick()
        self.assertTrue(h.killed)
        self.assertIn("longer than 360 min", self.job("j1-r1")["run"]["steps"][0]["reason"])

    def test_source_not_available(self):
        self.hand(1, checks=["a", "b"])
        self.runner.tick()
        self.finish_prepare(rc=128)
        self.runner.tick()
        self.runner.tick()
        j = self.job("j1-r1")
        self.assertEqual(j["phase"], "waiting_review")
        self.assertEqual([s["status"] for s in j["run"]["steps"]], ["red", "red"])
        self.assertIn("bad object", j["run"]["source_error"])
        self.assertEqual([h.kind for h in self.ex.handles], ["prepare"])

    def test_cancel_ends_running_step(self):
        self.hand(1)
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        h = self.ex.live()[0]
        j, err = rj.cancel("j1-r1", "admin", "wrong branch", data=self.data)
        self.assertIsNone(err)
        self.runner.tick()
        self.assertTrue(h.killed)
        self.runner.tick()
        j = self.job("j1-r1")
        self.assertEqual(j["phase"], "cancelled")
        self.assertIn("wrong branch", j["run"]["cancel_reason"])
        self.assertIn("j1-r1", self.ex.cleaned)
        self.assertEqual(rj.cache_load(self.data, self.clock()), {})
        self.assertIsNotNone(rj.cancel("j1-r1", "admin", data=self.data)[1])   # not open any more

    def test_restart_recovers_running_steps(self):
        self.hand(1, checks=["a", "b"])
        self.runner.tick()
        self.finish_prepare()
        self.runner.tick()
        fresh = rr.ReviewRunner(self.ex, data=self.data, clock=self.clock)   # web app restarted
        fresh.tick()
        self.assertEqual(self.ex.resets, ["j1-r1"])
        run = self.job("j1-r1")["run"]
        # rebuilt from the commit: a new prepare runs, step 1 waits again
        self.assertEqual(run["steps"][0]["status"], "waiting")
        self.assertEqual(run.get("prepare"), "running")
        self.assertEqual(self.ex.handles[-1].kind, "prepare")

    def test_stop_file_starts_nothing_new(self):
        self.hand(1)
        os.makedirs(rj.base_dir(self.data), exist_ok=True)
        open(rj.stop_file(self.data), "w").close()
        self.runner.tick()
        self.assertEqual(self.ex.handles, [])
        self.assertTrue(self.runner.status()["stopped"])
        os.remove(rj.stop_file(self.data))
        self.runner.tick()
        self.assertEqual(len(self.ex.handles), 1)

    def test_status(self):
        self.hand(1)
        self.runner.tick()
        st = self.runner.status()
        self.assertEqual(st["mode"], "on")
        self.assertEqual(st["targets"]["local"], {"limit": 1, "network": "none", "busy": 1})
        self.assertEqual(st["running"][0]["step"], "prepare")
        self.assertEqual(st["counts"]["running"], 1)


class Finding(Base):
    def ran(self, tid=1, statuses=("green",)):
        self.hand(tid)
        rj.write_run("j%d-r1" % tid, {"status": "ran", "steps": [{"nr": i + 1, "status": s} for i, s in enumerate(statuses)]},
                     self.data)
        return "j%d-r1" % tid

    def test_format(self):
        good = {"decision": "rework", "summary": "button cut off",
                "findings": [{"file": "frontend/src/App.tsx", "line": "12", "photo": "shots/390.png",
                              "expected": "button fully visible", "measured": "right edge at 402 px of 390",
                              "severity": "HIGH"}]}
        f, err = rj.check_finding(good)
        self.assertIsNone(err)
        self.assertEqual(f["findings"][0]["line"], 12)
        self.assertEqual(f["findings"][0]["severity"], "high")
        self.assertEqual(f["format"], "review-finding/1")
        for bad, msg in [([], "JSON object"), ({"decision": "maybe"}, "decision"),
                         ({"decision": "rework"}, "at least one finding"),
                         ({"decision": "rework", "findings": [{"severity": "x", "expected": "e"}]}, "severity"),
                         ({"decision": "rework", "findings": [{"expected": " "}]}, "expected is missing"),
                         ({"decision": "rework", "findings": [{"expected": "e", "line": "x"}]}, "line must be a number"),
                         ({"decision": "rework", "findings": [{"expected": "e", "line": True}]}, "line must be a number"),
                         ({"decision": "rework", "findings": [{"expected": "e", "line": 0}]}, "1 or more"),
                         ({"decision": "rework", "findings": "x"}, "must be a list"),
                         ({"decision": "rework", "findings": [{"expected": "e"}] * 51}, "at most 50")]:
            f, err = rj.check_finding(bad)
            self.assertIsNone(f)
            self.assertIn(msg, err)

    def test_green_with_red_steps_needs_reason(self):
        jid = self.ran(statuses=("green", "red"))
        j, err = rj.record_finding(jid, {"decision": "green"}, "rev", store_dir=self.data)
        self.assertIn("override_reason", err)
        j, err = rj.record_finding(jid, {"decision": "green", "override_reason": "flaky network test, passed on rerun"},
                                   "rev", store_dir=self.data)
        self.assertIsNone(err)
        self.assertEqual(j["phase"], "green")
        self.assertEqual(j["finding"]["by"], "rev")
        self.assertEqual(j["finding"]["commit"], C1)

    def test_only_after_the_runner(self):
        self.hand(1)
        j, err = rj.record_finding("j1-r1", {"decision": "green"}, "rev", store_dir=self.data)
        self.assertIn("after the runner finished", err)
        self.assertEqual(rj.record_finding("j9-r1", {"decision": "green"}, "r", store_dir=self.data)[1], "not found")
        jid = self.ran(tid=2)
        rj.record_finding(jid, {"decision": "green"}, "rev", store_dir=self.data)
        self.assertIsNotNone(rj.record_finding(jid, {"decision": "green"}, "rev", store_dir=self.data)[1])

    def test_in_review_phase_and_rework_text(self):
        jid = self.ran()
        run = self.job(jid)["run"]
        run["reviewer"] = {"thread_id": 99}
        rj.write_run(jid, run, self.data)
        self.assertEqual(self.job(jid)["phase"], "in_review")
        j, err = rj.record_finding(jid, {"decision": "rework", "summary": "two issues", "findings": [
            {"file": "a.py", "line": 3, "expected": "no crash", "measured": "KeyError", "severity": "critical"},
            {"expected": "docs mention the flag", "photo": "p.png"}]}, "rev", store_dir=self.data)
        text = rj.rework_text(j, j["finding"])
        self.assertIn("1. [critical] a.py:3 — expected: no crash · measured: KeyError", text)
        self.assertIn("2. [medium] (no file) — expected: docs mention the flag · photo: p.png", text)
        run = dict(j["run"], escalated=1)
        rj.write_run(jid, run, self.data)
        self.assertEqual(self.job(jid)["phase"], "escalated")


class FakeContainer:
    def __init__(self, kw):
        self.kw = kw
        self.attrs = {"State": {"Status": "exited", "ExitCode": 0}}
        self.removed = False

    def logs(self, stream=True, follow=True):
        return iter([b"hello\n"])

    def reload(self):
        pass

    def kill(self):
        pass

    def remove(self, force=False):
        self.removed = True


class FakeDocker:
    def __init__(self):
        self.runs = []
        self.created_volumes = []
        outer = self

        class Containers:
            def run(self, image, **kw):
                c = FakeContainer(dict(kw, image=image))
                outer.runs.append(c)
                return c

            def list(self, all=True, filters=None):
                return [c for c in outer.runs if c.kw["labels"].get("aiwerkstatt.review") == filters["label"].split("=", 1)[1]]

        class Volume:
            def __init__(self, name):
                self.name = name

            def remove(self, force=False):
                outer.created_volumes.remove(self.name)

        class Volumes:
            def get(self, name):
                if name not in outer.created_volumes:
                    raise RuntimeError("404 Client Error: volume not found")
                return Volume(name)

            def create(self, name, labels=None):
                outer.created_volumes.append(name)
                return Volume(name)

        self.containers = Containers()
        self.volumes = Volumes()


class DockerExecutorSettings(unittest.TestCase):
    def setUp(self):
        self.client = FakeDocker()
        with mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_LABELS": "cleanup.throwaway=1,bad key=x"}):
            self.ex = rr.DockerExecutor(self.client, image="runner:test", workspaces_volume="ws-vol", uid="10001")
        self.tmp = tempfile.mkdtemp(prefix="aiw-review-log-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job = {"id": "j5-r2", "project": "demo-app", "commit": C1}

    def common(self, kw):
        self.assertEqual(kw["user"], "10001:10001")
        self.assertEqual(kw["cap_drop"], ["ALL"])
        self.assertEqual(kw["security_opt"], ["no-new-privileges"])
        self.assertEqual(kw["labels"]["aiwerkstatt.review"], "j5-r2")
        self.assertEqual(kw["labels"]["cleanup.throwaway"], "1")
        self.assertNotIn("bad key", kw["labels"])
        self.assertNotIn("privileged", kw)
        for v in kw["volumes"]:
            self.assertFalse(str(v).startswith("/"), "named volumes only, never a host path: %s" % v)
        env = json.dumps(kw.get("environment") or {})
        for secret in ("KEY", "TOKEN", "SECRET", "ANTHROPIC", "OPENAI", "GEMINI"):
            self.assertNotIn(secret, env.replace("AIWERKSTATT_REVIEW", ""))

    def test_prepare_container(self):
        h = self.ex.prepare(self.job, os.path.join(self.tmp, "p.log"))
        kw = self.client.runs[0].kw
        self.common(kw)
        self.assertEqual(kw["network_mode"], "none")
        self.assertEqual(kw["volumes"]["ws-vol"], {"bind": "/repo", "mode": "ro"})
        self.assertEqual(kw["volumes"]["aiwerkstatt-review-j5-r2"], {"bind": "/workspace", "mode": "rw"})
        script = kw["command"][0]
        self.assertIn("-C /repo/demo-app archive --format=tar %s" % C1, script)
        self.assertIn("cat-file -e %s^{commit}" % C1, script)
        self.assertEqual(kw["image"], "runner:test")
        self.assertEqual(h.poll(), 0)
        with self.assertRaises(ValueError):
            self.ex.prepare(dict(self.job, project="../etc"), os.path.join(self.tmp, "x.log"))
        with self.assertRaises(ValueError):
            self.ex.prepare(dict(self.job, commit="HEAD; rm -rf /"), os.path.join(self.tmp, "x.log"))

    def test_step_container(self):
        step = {"nr": 2, "cmd": "npm ci && npm test", "target": "online"}
        h = self.ex.start(self.job, step, {"limit": 1, "network": "bridge", "cpus": "1.5", "memory": "3g",
                                           "image": "checks:node"}, os.path.join(self.tmp, "s.log"))
        kw = self.client.runs[0].kw
        self.common(kw)
        self.assertEqual(kw["image"], "checks:node")
        self.assertEqual(kw["network_mode"], "bridge")
        self.assertEqual(kw["nano_cpus"], 1_500_000_000)
        self.assertEqual(kw["mem_limit"], "3g")
        self.assertEqual(kw["command"], ["npm ci && npm test"])
        self.assertEqual(list(kw["volumes"]), ["aiwerkstatt-review-j5-r2"])       # never the workspaces volume
        self.assertEqual(kw["environment"]["CI"], "1")
        self.assertEqual(kw["environment"]["AIWERKSTATT_REVIEW_COMMIT"], C1)
        h.pump.t.join(2)
        self.assertEqual(h.poll(), 0)
        with open(os.path.join(self.tmp, "s.log")) as f:
            self.assertIn("hello", f.read())
        local = self.ex.start(self.job, dict(step, nr=3), {"limit": 1, "network": "none"}, os.path.join(self.tmp, "t.log"))
        self.assertEqual(self.client.runs[1].kw["network_mode"], "none")
        self.assertNotIn("nano_cpus", self.client.runs[1].kw)
        local.close()
        self.assertTrue(self.client.runs[1].removed)

    def test_cleanup_removes_containers_and_volume(self):
        self.ex.prepare(self.job, os.path.join(self.tmp, "p.log"))
        self.assertEqual(self.client.created_volumes, ["aiwerkstatt-review-j5-r2"])
        self.ex.cleanup(self.job)
        self.assertEqual(self.client.created_volumes, [])
        self.assertTrue(self.client.runs[0].removed)
        self.ex.cleanup(self.job)                              # a second cleanup is harmless


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global db, engine, store, wk
        import db  # noqa: F401
        import engine
        import store
        import app as wk
        cls.wk = wk
        cls.coord = store.api_token_create("review-coord", "coordinator")
        cls.restricted = store.api_token_create("review-restricted")
        cls.c = wk.app.test_client()
        # one project for the class: the test modules share one data folder and its port range is finite
        cls.pid = engine.create_project("Review API", "x", "d", "admin", provider="demo")["id"]

    @classmethod
    def tearDownClass(cls):
        engine.delete_project(cls.pid)

    def setUp(self):
        shutil.rmtree(rj.base_dir(), ignore_errors=True)
        try:
            os.remove(rj.mode_file())
        except OSError:
            pass
        self.wk.WRITE_LIMIT.reset()
        # a bare thread row (no task, so no queue entry another module's self-healer could pick up)
        db.execute("INSERT INTO threads(project_id,title,created_at) VALUES(?,?,?)", (self.pid, "t", store.now()))
        self.tid = db.query_one("SELECT MAX(id) AS m FROM threads")["m"]

    def admin(self):
        c = self.wk.app.test_client()
        with c.session_transaction() as s:
            s["user"] = "admin"
        return c

    def h(self, tok):
        return {"Authorization": "Bearer " + tok}

    def body(self, **kw):
        d = {"thread_id": self.tid, "commit": C1, "checks": ["python3 -m unittest"]}
        d.update(kw)
        return d

    def test_mode_switch_admin_only(self):
        r = self.c.post("/api/review/runner/mode", json={"mode": "on"}, headers=self.h(self.coord))
        self.assertEqual(r.status_code, 401)
        a = self.admin()
        self.assertEqual(a.post("/api/review/runner/mode", json={"mode": "sideways"}).status_code, 400)
        r = a.post("/api/review/runner/mode", json={"mode": "probe", "probe_max": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["mode"], "probe")

    def test_handover_off_then_on(self):
        a = self.admin()
        with mock.patch.dict(os.environ, {"AIWERKSTATT_REVIEW_MODE": "off"}):
            r = a.post("/api/review/jobs", json=self.body())
        self.assertEqual(r.status_code, 409)
        a.post("/api/review/runner/mode", json={"mode": "on"})
        r = a.post("/api/review/jobs", json=self.body(branch="feat/review"))
        self.assertEqual(r.status_code, 201, r.get_json())
        job = r.get_json()["job"]
        self.assertEqual(job["id"], "j%d-r1" % self.tid)
        self.assertEqual(job["project"], self.pid)
        self.assertEqual(job["by"], "admin")
        r = self.c.post("/api/review/jobs", json=self.body(), headers=self.h(self.coord))
        self.assertEqual(r.status_code, 200)                    # same commit: the existing job
        self.assertFalse(r.get_json()["created"])
        self.assertEqual(a.post("/api/review/jobs", json=self.body(commit="xyz")).status_code, 400)
        self.assertEqual(a.post("/api/review/jobs", json=self.body(thread_id=999999)).status_code, 404)

    def test_token_profiles(self):
        self.admin().post("/api/review/runner/mode", json={"mode": "on"})
        r = self.c.post("/api/review/jobs", json=self.body(), headers=self.h(self.restricted))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.c.get("/api/review/runner", headers=self.h(self.restricted)).status_code, 401)
        r = self.c.post("/api/review/jobs", json=self.body(), headers=self.h(self.coord))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["job"]["by"], "api:review-coord")
        self.assertEqual(self.c.get("/api/review/runner", headers=self.h(self.coord)).status_code, 200)
        self.assertEqual(self.c.get("/api/review/jobs").status_code, 401)
        member = self.wk.app.test_client()
        if not store.get_user("reviewmember2"):
            store.add_user("reviewmember2", "4321", "member", "x")
        with member.session_transaction() as s:
            s["user"] = "reviewmember2"
        self.assertEqual(member.get("/api/review/jobs").status_code, 403)

    def test_job_views_finding_cancel_and_log(self):
        a = self.admin()
        a.post("/api/review/runner/mode", json={"mode": "on"})
        jid = a.post("/api/review/jobs", json=self.body()).get_json()["job"]["id"]
        r = a.post("/api/review/jobs/%s/finding" % jid, json={"decision": "green"})
        self.assertEqual(r.status_code, 409)
        rj.write_run(jid, {"status": "ran", "green": True, "steps": [{"nr": 1, "cmd": "x", "target": "local",
                                                                       "status": "green", "rc": 0, "tail": "OK"}]})
        os.makedirs(os.path.join(rj.logs_dir(), jid), exist_ok=True)
        with open(os.path.join(rj.logs_dir(), jid, "step-1.log"), "w") as f:
            f.write("Ran 3 tests\nOK\n")
        self.assertEqual(a.get("/api/review/jobs/%s/steps/1/log" % jid).get_data(as_text=True), "Ran 3 tests\nOK\n")
        self.assertEqual(a.get("/api/review/jobs/%s/steps/2/log" % jid).status_code, 404)
        r = a.get("/api/review/jobs/%s" % jid)
        self.assertEqual(r.get_json()["phase"], "waiting_review")
        self.assertEqual(r.get_json()["run"]["steps"][0]["tail"], "OK")
        self.assertEqual(a.post("/api/review/jobs/%s/finding" % jid, json={"decision": "rework"}).status_code, 400)
        r = self.c.post("/api/review/jobs/%s/finding" % jid, json={"decision": "green", "summary": "all fine"},
                        headers=self.h(self.coord))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["phase"], "green")
        self.assertEqual(a.post("/api/review/jobs/%s/cancel" % jid).status_code, 409)
        self.assertEqual(a.get("/api/review/jobs/j1-x").status_code, 404)
        lst = a.get("/api/review/jobs?thread_id=%d" % self.tid).get_json()["jobs"]
        self.assertEqual([j["id"] for j in lst], [jid])
        self.assertEqual(a.get("/api/review/jobs?open=1").get_json()["jobs"], [])
        st = a.get("/api/review/runner").get_json()
        self.assertEqual(st["counts"]["green"], 1)
        self.assertIn("local", st["targets"])


if __name__ == "__main__":
    unittest.main()
