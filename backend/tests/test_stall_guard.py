"""Stall guard (0.44.0): measuring functions, state machine, real process trees (agent-runner/stall_guard.py).

Run: cd backend && python3 -m unittest tests.test_stall_guard     (~10 s)
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agent-runner"))
import stall_guard as sg  # noqa: E402


def mp(t, **kw):
    m = {"time": t, "procs": {}, "files": {}, "git": {}, "gate": {}}
    m.update(kw)
    return m


def pr(ticks, io_c=0, io_b=0, prog="sleep", start=1, protected=False, ppid=1):
    return {"ticks": ticks, "io_c": io_c, "io_b": io_b, "prog": prog, "start": start, "protected": protected, "ppid": ppid}


class Detection(unittest.TestCase):
    def test_program_and_protection(self):
        self.assertEqual(sg.program(["timeout", "600", "nice", "-n", "10", "python3", "-u", "/x/release_check.py", "run"]),
                         "release_check.py")
        self.assertEqual(sg.program(["bash", "-c", "ps aux | grep release_check; sleep 30"]), "bash")
        self.assertEqual(sg.program(["python3", "-m", "pytest", "-q"]), "pytest")
        self.assertTrue(sg.is_protected(["python3", "/a/b/security_gate.py"]))
        self.assertTrue(sg.is_protected(["env", "X=1", "bash", "/a/self-update.sh"]))
        # the word in a shell line never protects (otherwise the polling loop itself would be untouchable)
        self.assertFalse(sg.is_protected(["bash", "-c", "ps aux | grep release_check; sleep 30"]))
        self.assertFalse(sg.is_protected(["sleep", "300"]))

    def test_paths_from_command_lines(self):
        t = tempfile.mkdtemp(prefix="sg-p-")
        self.addCleanup(shutil.rmtree, t, True)
        log = os.path.join(t, "gate.log")
        open(log, "w").close()
        self.assertEqual(sg.paths_from(["tail", "-f", log]), [log])
        self.assertEqual(sg.paths_from(["bash", "-c", "until grep -q DONE %s; do sleep 5; done" % log]), [log])
        self.assertEqual(sg.paths_from(["cat", "gate.log"], cwd=t), [log])
        self.assertEqual(sg.paths_from(["cat", "/etc/passwd", "/proc/self/stat"]), [])

    def test_git_files_of_a_work_tree(self):
        t = tempfile.mkdtemp(prefix="sg-g-")
        self.addCleanup(shutil.rmtree, t, True)
        main = os.path.join(t, "main")
        subprocess.run(["git", "init", "-q", main], check=True)
        names = {os.path.basename(f) for f in sg.git_files(main)}
        self.assertIn("HEAD", names)
        self.assertEqual(sg.git_files(os.path.join(t, "none")), [])

    def test_work_state(self):
        t = tempfile.mkdtemp(prefix="sg-w-")
        self.addCleanup(shutil.rmtree, t, True)
        self.assertEqual(sg.work_state(t), "")
        env = dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@x.test", GIT_COMMITTER_NAME="a",
                   GIT_COMMITTER_EMAIL="a@x.test")
        subprocess.run(["git", "init", "-q", t], check=True)
        open(os.path.join(t, "f"), "w").write("x")
        subprocess.run(["git", "-C", t, "add", "f"], check=True)
        subprocess.run(["git", "-C", t, "commit", "-q", "-m", "first"], check=True, env=env)
        open(os.path.join(t, "g"), "w").write("y")
        s = sg.work_state(t)
        self.assertIn("first", s)
        self.assertIn("Uncommitted changes: 1 file", s)


class Axes(unittest.TestCase):
    def test_polling_loop_is_quiet(self):
        # `ps aux | grep ...; sleep 30`: about 0.3 % of a core, few characters - no axis moves
        a = mp(0, procs={10: pr(100, io_c=5000), 11: pr(3)})
        n = mp(60, procs={10: pr(118, io_c=9000), 11: pr(3)})
        self.assertEqual(sg.active_axes(a, n, hz=100), {})

    def test_cpu_io_files_git_gate(self):
        a = mp(0, procs={10: pr(0, prog="pytest"), 12: pr(0, prog="ssh")}, files={"/x/gate.log": [1, 10]},
               git={"/r/.git/index": [1, 5]})
        n = mp(60, procs={10: pr(500, prog="pytest"), 12: pr(0, io_c=50000, prog="ssh")},
               files={"/x/gate.log": [2, 20]}, git={"/r/.git/index": [2, 5]}, gate={"run-1": 59.0})
        ax = sg.active_axes(a, n, hz=100)
        self.assertIn("pytest", ax["cpu"])
        self.assertEqual(ax["io"], "ssh")
        self.assertEqual(ax["files"], "gate.log")
        self.assertEqual(ax["git"], "index")
        self.assertIn("gate", ax)

    def test_new_process_with_the_same_pid_does_not_count(self):
        a = mp(0, procs={10: pr(0, start=1)})
        n = mp(60, procs={10: pr(9000, start=2)})
        self.assertNotIn("cpu", sg.active_axes(a, n, hz=100))

    def test_gate_heartbeat_fresh_or_moving_only(self):
        a = mp(0)
        self.assertEqual(sg.active_axes(a, mp(100, gate={"r": 99.0})), {"gate": "gate progress"})
        old = mp(900, gate={"r": 100.0})
        self.assertEqual(sg.active_axes(old, mp(1000, gate={"r": 100.0})), {})      # heartbeat silent for 15 min

    def test_gate_state_only_for_this_run(self):
        t = tempfile.mkdtemp(prefix="sg-gate-")
        self.addCleanup(shutil.rmtree, t, True)
        now = time.time()
        for name, d in (("a", {"thread": 7, "end": None}), ("b", {"thread": 8, "end": None}),
                        ("c", {"thread": 7, "end": now}), ("d", {"thread": 9, "pid": 4242, "end": None})):
            d.update({"format": sg.PROGRESS_FORMAT, "run": name, "stamp": now})
            json.dump(d, open(os.path.join(t, name + ".json"), "w"))
        json.dump({"format": "other/1", "run": "e", "thread": 7, "stamp": now}, open(os.path.join(t, "e.json"), "w"))
        self.assertEqual(sorted(sg.gate_state(7, (), t, now)), ["a"])
        self.assertEqual(sorted(sg.gate_state(7, {4242}, t, now)), ["a", "d"])
        self.assertEqual(sg.gate_state(7, (), "", now), {})

    def test_gate_subtree_is_never_endable(self):
        # bash -c "release_check.py | tail -15": tail and bash belong to the gate (SIGPIPE) - only the other subtree may go
        m = mp(0, procs={
            20: pr(0, prog="bash", ppid=1), 21: pr(0, prog="release_check.py", protected=True, ppid=20),
            22: pr(0, prog="ssh", ppid=21), 23: pr(0, prog="tail", ppid=20),
            30: pr(0, prog="bash", ppid=1), 31: pr(0, prog="sleep", ppid=30)})
        to_end, protected = sg.endable(m)
        self.assertEqual(to_end, [31, 30])
        self.assertEqual(protected, [21])


class Machine(unittest.TestCase):
    def test_stages_exactly_once_and_healing(self):
        g = sg.Guard((10, 20, 30), clock=lambda: 0.0)
        self.assertEqual(g.tick(mp(0), 0), 0)
        self.assertEqual(g.tick(mp(9), 9), 0)
        self.assertEqual(g.tick(mp(10), 10), 1)
        self.assertEqual(g.tick(mp(15), 15), 0)                       # never twice
        self.assertEqual(g.tick(mp(21), 21), 2)
        g.output(22)
        self.assertEqual(g.pick_up_healed(), [{"stage": 2, "axis": "output", "text": "", "quiet_s": 22}])
        self.assertEqual(g.stage, 0)
        self.assertEqual(g.tick(mp(60), 60), 3)                       # quiet again from 22: 38 s -> jumps to the reached stage
        g.rest(61)
        self.assertEqual((g.stage, g.quiet_s(61)), (0, 0))
        s = g.state(70)
        self.assertEqual((s["stage"], s["quiet_s"], s["thresholds"]), (0, 9, [10, 20, 30]))

    def test_activity_on_one_axis_stops_the_clock(self):
        g = sg.Guard((10, 20, 30), clock=lambda: 0.0)
        g.tick(mp(0), 0)
        for t in range(5, 100, 5):
            self.assertEqual(g.tick(mp(t, gate={"x": float(t)}), t), 0)
        self.assertEqual(g.state(100)["axes"]["gate"], 5)

    def test_stages_from(self):
        self.assertEqual(sg.stages_from("900,1500,2400"), (900, 1500, 2400))
        self.assertEqual(sg.stages_from("2,1,3"), sg.STAGES_DEFAULT)
        self.assertEqual(sg.stages_from("x"), sg.STAGES_DEFAULT)
        self.assertEqual(sg.stages_from(""), sg.STAGES_DEFAULT)


class RealProcesses(unittest.TestCase):
    def tree(self, *commands):
        root = subprocess.Popen(["bash", "-c", "sleep 60 & " + " & ".join(commands) + " & wait"], start_new_session=True)
        self.addCleanup(lambda: os.killpg(root.pid, signal.SIGKILL))
        time.sleep(0.5)
        return root.pid

    def test_polling_loop_on_a_foreign_process_has_no_io(self):
        # `ps aux | grep -q ...; sleep` - every ps reads hundreds of /proc files; that I/O must not count for the waiting shell
        root = self.tree('for i in $(seq 1 400); do ps aux | grep -q "[n]o_such_gate_process" || true; sleep 0.3; done')
        t = tempfile.mkdtemp(prefix="sg-e0-")
        self.addCleanup(shutil.rmtree, t, True)
        a = sg.measure(root, 999990, progress_dir=t)
        time.sleep(3)
        n = sg.measure(root, 999990, progress_dir=t)
        self.assertNotIn("io", sg.active_axes(a, n))

    def test_hanging_tool_is_quiet_and_endable(self):
        root = self.tree("while :; do sleep 0.5; done")
        t = tempfile.mkdtemp(prefix="sg-e1-")
        self.addCleanup(shutil.rmtree, t, True)
        a = sg.measure(root, 999991, progress_dir=t)
        time.sleep(2)
        n = sg.measure(root, 999991, progress_dir=t)
        self.assertTrue(n["procs"], n)
        self.assertEqual(sg.active_axes(a, n), {})
        to_end, protected = sg.endable(n)
        self.assertTrue(to_end and not protected)
        hit = sg.end(to_end)
        self.assertTrue(hit)
        time.sleep(0.5)
        self.assertFalse(any(sg._alive(p) for p in hit))

    def test_growing_log_is_active(self):
        t = tempfile.mkdtemp(prefix="sg-e3-")
        self.addCleanup(shutil.rmtree, t, True)
        log = os.path.join(t, "run.log")
        open(log, "w").close()
        root = self.tree("for i in $(seq 1 100); do echo line >> %s; sleep 0.2; done" % log)
        a = sg.measure(root, 999993, progress_dir=t)
        time.sleep(1.0)
        n = sg.measure(root, 999993, progress_dir=t)
        self.assertEqual(sg.active_axes(a, n).get("files"), "run.log")


if __name__ == "__main__":
    unittest.main()
