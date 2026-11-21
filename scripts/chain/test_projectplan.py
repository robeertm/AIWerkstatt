"""Tests for scripts/chain/projectplan.py with a fake API, a fake clock and injected queue/release state.

Run: python3 -m unittest discover -s scripts/chain
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import projectplan as pp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "Project plan · "


def task(i, **kw):
    d = {"id": i, "track": "t", "title": "T " + i, "hours": 1, "prompt": "do " + i}
    d.update(kw)
    return d


def project(pid, tasks, **kw):
    d = {"id": pid, "name": pid, "tasks": tasks}
    d.update(kw)
    return d


def plan(projects, **kw):
    d = {"format": "projectplan/2", "name": "p1", "projects": projects}
    d.update(kw)
    return d


class Api(pp.FakeApi):
    def __init__(self):
        super().__init__()
        self.threads = {}
        self.fail = None

    def create_in(self, project, title, request, model="", effort=""):
        if self.fail:
            raise self.fail
        n = super().create_in(project, title, request, model, effort)
        self.requests = getattr(self, "requests", {})
        self.requests[n] = request
        return n

    def get(self, thread):
        return self.threads.get(thread, {"status": "working", "timeline": []})

    def finish(self, thread, text, status="done"):
        self.threads[thread] = {"status": status, "timeline": [{"type": "reply", "text": text, "created_at": "x"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="projectplan-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clock = pp.chain.FakeClock()
        self.api = Api()
        self.queue = {"waiting": [], "slots_free": 5}
        self.rel = {"repos": []}
        self.lines = []
        self.mk()

    def mk(self, excluded=None):
        self.pl = pp.ProjectPlans(self.root, self.api, PREFIX, excluded or [], self.clock,
                                  load=lambda: self.queue, release_state=lambda: self.rel, out=self.lines.append)
        self.pl.load_state()

    def draft(self, p):
        os.makedirs(self.pl.path("drafts"), exist_ok=True)
        with open(self.pl.path("drafts", p["name"] + ".json"), "w") as f:
            json.dump(p, f)

    def go(self, p, approve=True):
        self.draft(p)
        if approve:
            self.pl.approve(p["name"], "tester")
        return self.pl.check()

    def step(self):
        self.pl.save_state()
        self.pl.load_state()
        return self.pl.check()

    def st(self, name, key):
        return (self.pl.s["tasks"].get("%s:%s" % (name, key)) or {}).get("state")


class Format(unittest.TestCase):
    def bad(self, p, text, excluded=None):
        err = pp.validate_plan(p, excluded or [])
        self.assertTrue(any(text in e for e in err), (text, err))

    def test_example_valid_and_simulates(self):
        with open(os.path.join(HERE, "example-projectplan.json")) as f:
            p = json.load(f)
        self.assertEqual(pp.validate_plan(p, []), [])
        sim = pp.simulate(p)
        self.assertEqual(sim["unscheduled"], [])
        self.assertEqual(len(p["projects"]), 3)

    def test_rejections(self):
        t = task("A1")
        self.assertEqual(pp.validate_plan(plan([project("x", [t])]), []), [])
        self.bad(plan([project("x", [t])], format="projectplan/1"), "format must be")
        self.bad(plan([project("x", [t])], name="Bad Name"), "name missing")
        self.bad(plan([]), "non-empty")
        self.bad(plan([project("x", [t]), project("x", [task("A2")])]), "twice")
        self.bad(plan([project("x", [t, t])]), "twice")
        self.bad(plan([project("x/y", [t])]), "malformed project id")
        self.bad(plan([project("x", [task("a1")])]), "malformed task id")
        self.bad(plan([project("x", [dict(t, model=5)])]), "model")
        self.bad(plan([project("x", [dict(t, effort="extreme")])]), "effort")
        self.bad(plan([project("x", [dict(t, hours=0)])]), "hours")
        self.bad(plan([project("x", [dict(t, hours=99)])]), "hours")
        self.bad(plan([project("x", [dict(t, folder="/x")])]), "unknown field")
        self.bad(plan([project("x", [dict(t, depends_on=["Z9"])])]), "unknown dependency")
        self.bad(plan([project("x", [dict(t, depends_on=["other/A1"])])]), "unknown dependency")
        self.bad(plan([project("x", [dict(t, depends_on=["A1"])])]), "cycle")
        self.bad(plan([project("x", [task("A1", depends_on=["y/B1"])]), project("y", [task("B1", depends_on=["x/A1"])])]),
                 "cycle")
        self.bad(plan([project("x", [t])], order=["x/A9"]), "order must list")
        self.bad(plan([project("x", [t, task("A2", optional=True), task("A3", depends_on=["A2"])])]), "optional")
        self.bad(plan([project("x", [dict(t, release="weekly")])]), "release must be")
        self.bad(plan([project("x", [t], release={"kind": "sometimes"})]), "release.kind")
        self.bad(plan([project("x", [t], history=[{"title": "no date"}])]), "history")

    def test_limits_only_lower(self):
        t = [project("x", [task("A1")])]
        self.bad(plan(t, limits={"total": 5}), "above the ceiling")
        self.bad(plan(t, limits={"per_project": 3}), "above the ceiling")
        self.bad(plan(t, limits={"per_day": 61}), "above the ceiling")
        self.bad(plan(t, limits={"priority": "never"}), "priority")
        self.bad(plan(t, limits={"weekly": 1}), "unknown")
        self.bad(plan([project("x", [task("A1")], limits={"per_project": 3})]), "limits.per_project")
        self.assertEqual(pp.limits_of(plan(t)), {"total": 3, "per_project": 1, "per_day": 30, "priority": "free"})
        self.assertEqual(pp.limits_of(plan(t, limits={"total": 2, "priority": "queue"}))["total"], 2)

    def test_excluded_projects(self):
        p = plan([project("secret-tool", [task("A1")])])
        self.bad(p, "excluded", excluded=["Secret-Tool"])
        self.assertEqual(pp.validate_plan(p, []), [])

    def test_release_target_inherits_and_overrides(self):
        pr = {"release": {"kind": "train", "slug": "s"}}
        self.assertEqual(pp.release_of({}, pr)["kind"], "train")
        self.assertEqual(pp.release_of({"release": "none"}, pr)["kind"], "none")
        self.assertEqual(pp.release_of({"release": "own"}, pr)["kind"], "own")
        r = pp.release_of({"release": "1.2.3"}, pr)
        self.assertEqual((r["kind"], r["version"], r["slug"]), ("train", "1.2.3", "s"))
        self.assertEqual(pp.release_of({}, {})["kind"], "none")


class Approval(Base):
    def test_draft_never_runs_by_itself(self):
        p = plan([project("x", [task("A1")])])
        self.assertEqual(self.go(p, approve=False), [])
        self.assertEqual(self.api.created, [])
        st = self.pl.status()
        self.assertFalse(st["plans"][0]["approved"])
        self.assertEqual(st["plans"][0]["tasks"][0]["state"], "draft")

    def test_approve_files_what_is_ready_and_records_who(self):
        p = plan([project("x", [task("A1")])])
        self.draft(p)
        self.pl.approve("p1", "alice")
        self.assertEqual(self.pl.fill(), ["x/A1"])
        self.assertEqual(self.pl.s["plans"]["p1"]["by"], "alice")
        self.assertTrue(self.api.created[0][1].startswith(PREFIX + "x A1: "))
        self.assertEqual(self.api.created[0][0], "x")

    def test_invalid_draft_is_refused(self):
        self.draft(plan([project("x", [task("A1", hours=0)])]))
        with self.assertRaises(pp.Refused):
            self.pl.approve("p1")
        self.assertFalse(os.path.exists(self.pl.path("active", "p1.json")))
        with self.assertRaises(pp.Refused):
            self.pl.approve("nope")

    def test_excluded_project_cannot_be_approved(self):
        self.mk(excluded=["x"])
        self.draft(plan([project("x", [task("A1")])]))
        with self.assertRaises(pp.Refused):
            self.pl.approve("p1")

    def test_withdraw_stops_new_filing_only(self):
        p = plan([project("x", [task("A1"), task("A2", track="u")])], limits={"total": 1})
        self.go(p)
        self.pl.withdraw("p1", "changed my mind")
        self.api.finish(1, "ok")
        self.pl.save_state()
        self.assertEqual(self.pl.check(), [])
        self.assertEqual(len(self.api.created), 1)

    def test_pause_and_kill_switch(self):
        p = plan([project("x", [task("A1")])])
        self.draft(p)
        self.pl.approve("p1")
        self.pl.s["plans"]["p1"]["paused"] = True
        self.assertEqual(self.pl.fill(), [])
        self.assertEqual(self.pl.halt["p1"], "paused")
        self.pl.s["plans"]["p1"]["paused"] = False
        open(os.path.join(self.root, "KILL"), "w").close()
        self.assertEqual(self.pl.fill(), [])
        with self.assertRaises(pp.chain.Killed):
            self.pl.check()
        os.remove(os.path.join(self.root, "KILL"))
        os.makedirs(self.pl.base, exist_ok=True)
        open(self.pl.path("OFF"), "w").close()
        self.assertEqual(self.pl.fill(), [])


class Brakes(Base):
    def test_total_brake_and_hard_ceiling(self):
        ps = [project("p%d" % i, [task("A1")]) for i in range(6)]
        self.assertEqual(len(self.go(plan(ps))), 3)                                   # default 3
        self.assertEqual(self.pl.halt["p1"], "total brake 3 reached")

    def test_total_brake_can_be_lowered(self):
        ps = [project("p%d" % i, [task("A1")]) for i in range(4)]
        self.assertEqual(len(self.go(plan(ps, limits={"total": 2}))), 2)

    def test_per_project_brake_and_track_rule(self):
        p = plan([project("x", [task("A1", track="a"), task("A2", track="b"), task("A3", track="a")])])
        self.assertEqual(self.go(p), ["x/A1"])                                         # default 1 per project
        self.assertIn("project brake", self.pl.halt["x/A2"])

    def test_two_per_project_and_track_rule(self):
        q = plan([project("x", [task("A1", track="a"), task("A2", track="a"), task("A3", track="b")])],
                 limits={"per_project": 2})
        self.assertEqual(self.go(q), ["x/A1", "x/A3"])                                 # A2: same track as A1
        self.assertIn("track a", self.pl.halt["x/A2"])

    def test_project_may_lower_per_project(self):
        p = plan([project("x", [task("A1", track="a"), task("A2", track="b")], limits={"per_project": 1})],
                 limits={"per_project": 2})
        self.assertEqual(self.go(p), ["x/A1"])

    def test_daily_brakes(self):
        ps = [project("x", [task("A1"), task("A2", depends_on=["A1"])]), project("y", [task("B1")])]
        p = plan(ps, limits={"per_day": 1})
        self.assertEqual(len(self.go(p)), 1)
        self.assertIn("daily brake", self.pl.halt["p1"])
        self.clock.t += 86400
        self.pl.s["tasks"]["p1:x/A1"]["state"] = "done"
        self.assertEqual(len(self.pl.fill()), 1)                                       # next day

    def test_daily_brake_of_a_project(self):
        p = plan([project("x", [task("A1"), task("A2", depends_on=["A1"])], limits={"per_day": 1}),
                  project("y", [task("B1")])])
        self.assertEqual(sorted(self.go(p)), ["x/A1", "y/B1"])
        self.pl.s["tasks"]["p1:x/A1"]["state"] = "done"
        self.assertEqual(self.pl.fill(), [])
        self.assertIn("daily brake of the project", self.pl.halt["x/A2"])

    def test_cross_project_dependency_waits(self):
        p = plan([project("x", [task("A1", depends_on=["y/B1"])]), project("y", [task("B1")])])
        self.assertEqual(self.go(p), ["y/B1"])
        self.assertEqual(self.st("p1", "x/A1") or "waiting", "waiting")
        self.api.finish(1, "all good")
        self.assertEqual(self.step(), ["x/A1"])

    def test_optional_waits_for_option(self):
        p = plan([project("x", [task("A1", optional=True)])])
        self.assertEqual(self.go(p), [])
        self.assertEqual(self.pl.status()["plans"][0]["tasks"][0]["state"], "held")
        self.pl.option("p1:x/A1")
        self.assertEqual(self.pl.fill(), ["x/A1"])

    def test_api_failure_files_nothing_and_leaves_no_state(self):
        self.api.fail = pp.chain.ChainError("down")
        self.assertEqual(self.go(plan([project("x", [task("A1")])])), [])
        self.assertIsNone(self.st("p1", "x/A1"))
        self.api.fail = None
        self.assertEqual(self.pl.fill(), ["x/A1"])

    def test_excluded_is_checked_when_filing_too(self):
        self.go(plan([project("x", [task("A1")])]), approve=True)
        self.pl.s["tasks"] = {}
        self.pl.excluded = ["x"]
        self.assertEqual(self.pl.fill(), [])
        self.assertEqual(self.pl.halt.get("p1"), None)
        self.assertEqual(self.pl.load_plans("active"), {})                             # invalid for the exclusion list


class Priority(Base):
    def test_foreign_work_waiting_holds_the_plan(self):
        self.queue = {"waiting": [{"thread": 99, "urgent": False}], "slots_free": 3}
        self.assertEqual(self.go(plan([project("x", [task("A1")])])), [])
        self.assertIn("foreign", self.pl.halt["p1"])

    def test_urgent_task_holds_the_plan(self):
        self.queue = {"waiting": [{"thread": 99, "urgent": True}], "slots_free": 3}
        self.assertEqual(self.go(plan([project("x", [task("A1")])])), [])
        self.assertIn("urgent", self.pl.halt["p1"])

    def test_no_free_slot_holds_the_plan(self):
        self.queue = {"waiting": [], "slots_free": 0}
        self.assertEqual(self.go(plan([project("x", [task("A1")])])), [])
        self.assertIn("slot", self.pl.halt["p1"])

    def test_unknown_queue_fails_closed(self):
        self.queue = None
        self.assertEqual(self.go(plan([project("x", [task("A1")])])), [])
        self.assertIn("unknown", self.pl.halt["p1"])

    def test_slots_are_shared_with_tasks_filed_in_the_same_round(self):
        self.queue = {"waiting": [], "slots_free": 2}
        ps = [project("p%d" % i, [task("A1")]) for i in range(3)]
        self.assertEqual(len(self.go(plan(ps))), 2)

    def test_own_waiting_tasks_do_not_count_as_foreign(self):
        p = plan([project("x", [task("A1")]), project("y", [task("B1")])])
        self.go(p)
        self.queue = {"waiting": [{"thread": 1, "urgent": False}], "slots_free": 2}
        self.assertEqual(self.pl.fill(), [])        # both already filed
        self.assertNotIn("foreign", self.pl.halt.get("p1", ""))

    def test_queue_mode_ignores_the_queue(self):
        self.queue = {"waiting": [{"thread": 99, "urgent": False}], "slots_free": 0}
        self.assertEqual(self.go(plan([project("x", [task("A1")])], limits={"priority": "queue"})), ["x/A1"])


class Releases(Base):
    def repo_plan(self, **kw):
        pr = {"kind": "train", "slug": "s", "repo": "owner/s"}
        return plan([project("x", [task("A1"), task("A2", depends_on=["A1"], **kw)], release=pr)])

    def test_backlog_blocks_the_repo(self):
        self.rel = {"repos": [{"slug": "s", "level": "critical"}]}
        self.assertEqual(self.go(self.repo_plan()), [])
        self.assertIn("release backlog", self.pl.halt["x/A1"])
        self.rel = {"repos": [{"slug": "s", "level": "ok", "train": {"status": "red"}}]}
        self.assertEqual(self.pl.fill(), [])
        self.assertIn("is red", self.pl.halt["x/A1"])

    def test_release_fix_may_pass_the_backlog(self):
        self.rel = {"repos": [{"slug": "s", "level": "stale"}]}
        p = plan([project("x", [task("A1", release_fix=True)], release={"kind": "train", "slug": "s"})])
        self.assertEqual(self.go(p), ["x/A1"])

    def test_one_release_way_per_repo(self):
        pr = {"kind": "train", "slug": "s", "repo": "owner/s"}
        p = plan([project("x", [task("A1", track="a", release="own"), task("A2", track="b", release="own")],
                          release=pr)], limits={"per_project": 2})
        self.assertEqual(self.go(p), ["x/A1"])
        self.assertIn("another own release", self.pl.halt["x/A2"])

    def test_own_release_never_next_to_a_running_train(self):
        self.rel = {"repos": [{"slug": "s", "level": "ok", "train": {"status": "running"}}]}
        p = plan([project("x", [task("A1", release="own")], release={"kind": "train", "slug": "s"})])
        self.assertEqual(self.go(p), [])
        self.assertIn("train s is running", self.pl.halt["x/A1"])

    def test_built_but_not_published_waits_then_publishes(self):
        self.assertEqual(self.go(self.repo_plan()), ["x/A1"])
        self.api.finish(1, "built\nRELEASE STATE: waiting s 1.5.0")
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "waiting_release")
        self.assertEqual(self.pl.s["tasks"]["p1:x/A1"]["release_version"], "1.5.0")
        self.assertEqual(self.st("p1", "x/A2"), "running")                              # waiting_release counts as met
        self.rel = {"repos": [{"slug": "s", "level": "ok", "published": "1.4.0"}]}
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "waiting_release")
        self.rel = {"repos": [{"slug": "s", "level": "ok", "published": "1.5.0", "published_ts": 5}]}
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "published")

    def test_no_release_target_is_done_at_once(self):
        p = plan([project("x", [task("A1")], release={"kind": "none"})])
        self.go(p)
        self.api.finish(1, "checked, nothing to do")
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "done")


class Detection(Base):
    def setUp(self):
        super().setUp()
        self.go(plan([project("x", [task("A1"), task("A2", depends_on=["A1"])])]))

    def test_still_working_stays_running(self):
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "running")

    def test_finished_with_answer_is_done_and_starts_the_next(self):
        self.api.finish(1, "Done: the cart page is built.")
        self.assertEqual(self.step(), ["x/A2"])
        self.assertEqual(self.st("p1", "x/A1"), "done")

    def test_question_waits_and_blocks_dependents(self):
        self.api.finish(1, "QUESTION FOR THE COORDINATOR: which colour?")
        self.assertEqual(self.step(), [])
        self.assertEqual(self.st("p1", "x/A1"), "question")
        self.api.threads[1] = {"status": "working", "timeline": []}                     # a follow-up resumed it
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "running")

    def test_stuck_is_not_refiled(self):
        self.api.finish(1, "STUCK: waiting for a background job")
        self.assertEqual(self.step(), [])
        self.assertEqual(self.st("p1", "x/A1"), "stuck")
        self.pl.retry("p1:x/A1")
        self.assertEqual(self.pl.fill(), ["x/A1"])
        self.assertEqual(len(self.api.created), 2)

    def test_stopped_and_failed_are_not_refiled(self):
        self.api.threads[1] = {"status": "stopped", "timeline": []}
        self.assertEqual(self.step(), [])
        self.assertEqual(self.st("p1", "x/A1"), "stopped")
        with self.assertRaises(pp.Refused):
            self.pl.retry("p1:x/A2")

    def test_failed(self):
        self.api.threads[1] = {"status": "failed", "timeline": []}
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "failed")

    def test_finished_without_answer_waits(self):
        self.api.threads[1] = {"status": "done", "timeline": []}
        self.clock.t += 3600
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "running")

    def test_api_unreachable_counts_as_active(self):
        self.api.get = lambda t: (_ for _ in ()).throw(pp.chain.ChainError("down"))
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "running")

    def test_done_by_hand(self):
        self.pl.mark_done("p1:x/A1", "checked by a person")
        self.assertEqual(self.st("p1", "x/A1"), "done")
        with self.assertRaises(pp.Refused):
            self.pl.mark_done("garbage")


class StatusAndSimulation(Base):
    def test_status_file_and_history(self):
        p = plan([project("x", [task("A1")], history=[{"date": "2026-01-02", "title": "Old", "version": "1.0.0"}])])
        self.go(p)
        st = self.pl.write_status()
        self.assertEqual(st["format"], "projectplan-status/1")
        self.assertEqual(st["plans"][0]["projects"][0]["history"][0]["version"], "1.0.0")
        self.assertNotIn("do A1", json.dumps(st))                                       # prompts never appear
        pp.chain.write_atomic(self.pl.path("status.json"), json.dumps(st))
        self.assertTrue(os.path.exists(self.pl.path("status.json")))

    def test_simulation_respects_brakes_and_dependencies(self):
        p = plan([project("x", [task("A1", hours=2), task("A2", hours=1, depends_on=["A1"])]),
                  project("y", [task("B1", hours=1, depends_on=["x/A1"])])])
        sim = pp.simulate(p)
        self.assertEqual(sim["tasks"]["x/A2"][0], 2.0)
        self.assertEqual(sim["tasks"]["y/B1"][0], 2.0)
        self.assertEqual(sim["end_h"], 3.0)
        one = pp.simulate(plan([project("x", [task("A1", track="a"), task("A2", track="b")])]))
        self.assertEqual(one["end_h"], 2.0)                                              # one per project by default
        self.assertEqual(pp.simulate(plan([project("x", [task("A1", optional=True)])]))["tasks"], {})


class CommandLine(Base):
    def cli(self, *args, **kw):
        out = []
        err = io.StringIO()
        with redirect_stderr(err):
            rc = pp.main(["--root", self.root, "--prefix", PREFIX, "--excluded", kw.pop("excluded", "")] + list(args),
                         out=out.append, api=self.api, clock=self.clock, load=lambda: self.queue,
                         release_state=lambda: self.rel)
        return rc, "\n".join(out), err.getvalue()

    def test_validate_and_simulate_files(self):
        path = os.path.join(HERE, "example-projectplan.json")
        self.assertEqual(self.cli("validate", path)[0], 0)
        rc, out, _ = self.cli("simulate", path)
        self.assertEqual(rc, 0)
        self.assertIn("end_h", out)
        rc, out, _ = self.cli("validate", path, excluded="webshop")
        self.assertEqual(rc, 1)
        self.assertIn("excluded", out)

    def test_approve_status_and_exit_codes(self):
        self.draft(plan([project("x", [task("A1")])]))
        rc, out, _ = self.cli("status")
        self.assertIn("DRAFT", out)
        rc, out, _ = self.cli("approve", "p1", "--by", "tester")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.api.created), 1)
        rc, out, _ = self.cli("status", "--json")
        self.assertEqual(json.loads(out)["plans"][0]["approved"], True)
        self.assertEqual(self.cli("approve", "missing")[0], 1)
        self.assertEqual(self.cli("retry", "p1:x/A1")[0], 1)
        open(os.path.join(self.root, "KILL"), "w").close()
        self.assertEqual(self.cli("check")[0], 3)
        self.assertEqual(self.cli("status")[0], 0)                                       # status still readable

    def test_token_file_must_be_private(self):
        tok = os.path.join(self.root, "token")
        with open(tok, "w") as f:
            f.write("x")
        os.chmod(tok, 0o644)
        api = pp.PlanApi("http://127.0.0.1:9", tok, "", PREFIX)
        with self.assertRaises(pp.chain.ChainError):
            api._token()


class LiveCheck(Base):
    """Roll out + measure: mandatory steps in the task text, the ``LIVE:`` line, a follow-up when it is missing or stale."""

    def live_plan(self, **live):
        kw = dict(kind="rollout", instances=["i1", "i2"], how="the project roll-out command")
        kw.update(live)
        return plan([project("x", [task("A1")], release={"kind": "own", "slug": "s"}, live=kw),
                     project("y", [task("B1")], release={"kind": "own", "slug": "t"}, live="none"),
                     project("z", [task("C1")], release={"kind": "none"})], limits={"per_project": 1})

    def pub(self, ver, ts=100):
        self.rel = {"repos": [{"slug": "s", "level": "ok", "published": ver, "published_ts": ts, "unpublished": False}]}

    def test_check_rules(self):
        lv = {"instances": ["a", "b"]}
        self.assertEqual(pp.live_check("no line", lv)[0], "missing")
        self.assertEqual(pp.live_check("LIVE: fine", lv)[0], "missing")
        self.assertEqual(pp.live_check("LIVE: a=1.2.3", lv)[0], "incomplete")
        self.assertEqual(pp.live_check("LIVE: a=1.2.2 b=1.2.3", lv, "1.2.3")[0], "stale")
        self.assertEqual(pp.live_check("LIVE: a=1.2.3 b=v1.2.3", lv, "1.2.3")[0], "ok")
        self.assertEqual(pp.live_check("LIVE: a=1.0.0 b=1.0.0\nx\nLIVE: a=1.2.3 b=1.2.3", lv, "1.2.3")[0], "ok")
        self.assertEqual(pp.live_check("LIVE: q=9.9.9", {"instances": []})[0], "ok")

    def test_live_of_inherits_and_overrides(self):
        proj = {"live": {"kind": "rollout", "instances": ["i1"], "how": "w"}, "release": {"kind": "train"}}
        self.assertTrue(pp.live_of({}, proj)["required"])
        self.assertFalse(pp.live_of({"release": "none"}, proj)["required"])
        self.assertFalse(pp.live_of({"live": "none"}, proj)["required"])
        self.assertFalse(pp.live_of({"live": {"kind": "workstation"}}, proj)["required"])
        self.assertTrue(pp.live_of({}, {"release": {"kind": "own"}})["required"])

    def test_validation(self):
        for bad in ("whatever", {"kind": "x"}, {"instances": "a"}, {"instances": ["a b"]}, 5):
            err = pp.validate_plan(plan([project("x", [task("A1")], live=bad)]), [])
            self.assertTrue(any("live" in e for e in err), bad)
        self.assertEqual(pp.validate_plan(plan([project("x", [task("A1", live="workstation")])]), []), [])

    def test_task_text_carries_the_mandatory_steps(self):
        self.go(self.live_plan(), approve=True)
        r = self.api.requests
        self.assertIn("Roll out + measure (mandatory", r[1])
        self.assertIn("LIVE: <instance>=", r[1])
        self.assertIn("i1, i2", r[1])
        self.assertIn("the project roll-out command", r[1])
        self.assertTrue(any("Roll-out: not needed" in v for v in r.values()))
        self.assertTrue(all("Roll out + measure" not in v for k, v in r.items() if "no release in this task" in v))

    def test_missing_line_follow_up_then_ok(self):
        self.go(self.live_plan(), approve=True)
        self.pub("1.5.0")
        self.api.finish(1, "done, published 1.5.0")
        self.step()
        self.assertEqual(len(self.api.comments), 1)
        self.assertIn("LIVE:", self.api.comments[0][1])
        self.assertEqual(self.pl.s["tasks"]["p1:x/A1"]["live"]["state"], "followup")
        self.step()
        self.assertEqual(len(self.api.comments), 1)                       # same answer: no duplicate
        self.api.finish(1, "rolled out\nLIVE: i1=1.5.0 i2=1.5.0")
        self.step()
        self.assertEqual(self.pl.s["tasks"]["p1:x/A1"]["live"]["state"], "ok")
        self.assertEqual(len(self.api.comments), 1)
        st = self.pl.status()
        row = {r["key"]: r for r in st["plans"][0]["tasks"]}["x/A1"]
        self.assertFalse(row["live"]["not_live"])

    def test_release_pending_checks_after_publication_and_catches_stale(self):
        self.go(self.live_plan(), approve=True)
        self.api.finish(1, "built\nRELEASE STATE: waiting s 1.5.0\nLIVE: i1=1.4.0 i2=1.4.0")
        self.rel = {"repos": [{"slug": "s", "level": "ok", "published": "1.4.0", "unpublished": True}]}
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "waiting_release")
        self.assertEqual(getattr(self.api, "comments", []), [])           # not published yet: nothing demanded
        self.pub("1.5.0")
        self.step()
        self.assertEqual(self.st("p1", "x/A1"), "published")
        self.assertEqual(len(self.api.comments), 1)                      # LIVE shows 1.4.0: stale
        self.assertIn("1.5.0", self.api.comments[0][1])
        st = self.pl.status()
        self.assertTrue({r["key"]: r for r in st["plans"][0]["tasks"]}["x/A1"]["live"]["not_live"])
        self.assertEqual(st["live"]["not_live"], 1)

    def test_after_two_follow_ups_it_is_held_and_quiet(self):
        self.go(self.live_plan(), approve=True)
        self.pub("1.5.0")
        self.api.finish(1, "done")
        for i in range(pp.LIVE_FOLLOWUP_MAX):
            self.step()
            self.assertEqual(len(self.api.comments), i + 1)
            self.api.finish(1, "again without a line %d" % i)
        self.step()
        self.assertEqual(len(self.api.comments), pp.LIVE_FOLLOWUP_MAX)
        self.assertEqual(self.pl.s["tasks"]["p1:x/A1"]["live"]["state"], "held")
        self.step()
        self.assertEqual(len(self.api.comments), pp.LIVE_FOLLOWUP_MAX)

    def test_no_instance_and_no_release_need_nothing(self):
        self.go(self.live_plan(), approve=True)
        self.api.finish(2, "done, no LIVE")                               # project y: live none
        self.api.finish(3, "analysis")                                    # project z: release none
        self.step()
        self.assertEqual(getattr(self.api, "comments", []), [])

    def test_old_state_without_marker_is_left_alone(self):
        self.go(self.live_plan(), approve=True)
        self.pl.s["tasks"]["p1:x/A1"].pop("live_required", None)
        self.pub("1.5.0")
        self.api.finish(1, "done without LIVE")
        self.step()
        self.assertEqual(getattr(self.api, "comments", []), [])
        row = {r["key"]: r for r in self.pl.status()["plans"][0]["tasks"]}["x/A1"]
        self.assertEqual(row["live"]["state"], "n/a")
        self.assertEqual(self.pl.status()["live"]["not_live"], 0)


if __name__ == "__main__":
    unittest.main()
