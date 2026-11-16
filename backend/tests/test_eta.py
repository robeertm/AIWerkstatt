"""Remaining-time estimate (0.25.0): format, with/without history, load factor, test rate, FIFO start times,
missing/broken progress files -> unknown without a crash, writer without write permission stays silent.
Run: cd backend && python3 -m unittest tests.test_eta"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import eta  # noqa: E402

PH = ["prepare", "full", "full_again", "image", "image_again", "release", "verify"]


def hist_entry(i, full=3200, image=3700, again=300, image_again=500, load=None, result="ok"):
    ph = {"prepare": 60, "full": full, "image": image, "release": 120, "verify": 600}
    if again:
        ph["full_again"] = again
    if image_again:
        ph["image_again"] = image_again
    return {"profile": "app", "kind": "gate", "run": "r%d" % i, "end": 1000 + i, "result": result, "phases": ph,
            "tests": {"full": 3574}, "skipped": [k for k, v in (("full_again", again), ("image_again", image_again)) if not v],
            "load": load or {"host": 0.5}}


def run_in(phase, since, now=100000.0, done=None, total=None):
    phases, cur = [], None
    for i, n in enumerate(PH):
        if n == phase:
            phases.append({"name": n, "status": "running", "start": now - since, "done": done, "total": total})
            cur = i
        elif cur is None:
            phases.append({"name": n, "status": "done", "start": 0, "end": 1})
        else:
            phases.append({"name": n, "status": "open"})
    return {"format": eta.FORMAT, "run": "x", "profile": "app", "kind": "gate", "title": "gate", "phases": phases,
            "current": cur, "start": now - 9999, "stamp": now, "end": None, "load": {"host": 0.5}}


class Format(unittest.TestCase):
    def test_minutes_hours_days(self):
        self.assertEqual(eta.duration_text(None), "unknown")
        self.assertEqual(eta.duration_text(30), "under 1 min")
        self.assertEqual(eta.duration_text(12 * 60), "~12 min")
        self.assertEqual(eta.duration_text(37 * 60), "~35 min")
        self.assertEqual(eta.duration_text(130 * 60), "~2 h 10 min")
        self.assertEqual(eta.duration_text(3 * 3600), "~3 h")
        self.assertEqual(eta.duration_text(28 * 3600), "~1 day 4 h")
        self.assertEqual(eta.duration_text(24 * 3600 + 600), "~1 day")
        self.assertEqual(eta.duration_text(50 * 3600), "~2 days")
        self.assertEqual(eta.duration_text(3 * 86400 + 3000), "~3 days")

    def test_eta_text_with_range(self):
        self.assertEqual(eta.eta_text(35 * 60, 25 * 60, 60 * 60), "done in ~35 min (25-60 min)")
        self.assertEqual(eta.eta_text(None), "unknown (first run of this kind)")
        self.assertIn("starts in ~1 h 20 min", eta.eta_text(80 * 60, prefix="starts in"))
        self.assertEqual(eta.eta_text(20), "done in under 1 min")


class Estimate(unittest.TestCase):
    def test_with_history(self):
        hist = [hist_entry(i) for i in range(6)]
        s = eta.estimate(run_in("image", 1700), hist, load={"host": 0.5}, now=100000.0)
        # rest of image 2000 + repeat 500 + release 120 + verify 600 = 3220
        self.assertAlmostEqual(s["eta_s"], 3220, delta=5)
        self.assertEqual(s["reliability"], "good")
        self.assertEqual(s["phase"], "image")
        self.assertEqual(s["after"], ["image again", "release", "verification"])
        self.assertLessEqual(s["lo_s"], s["eta_s"])
        self.assertGreaterEqual(s["hi_s"], s["eta_s"])
        self.assertTrue(s["text"].startswith("done in ~55 min"))
        self.assertIn("image", eta.line(s))

    def test_without_history_is_unknown(self):
        s = eta.estimate(run_in("full", 600), [], now=100000.0)
        self.assertIsNone(s["eta_s"])
        self.assertEqual(s["text"], "unknown (first run of this kind)")
        self.assertEqual(s["reliability"], "unknown")

    def test_defaults_without_history_is_rough(self):
        r = run_in("full", 600)
        r["defaults"] = {"full": 3600, "image": 3900}
        s = eta.estimate(r, [], now=100000.0)
        self.assertEqual(s["eta_s"], 3000 + 3900)
        self.assertEqual(s["reliability"], "rough")

    def test_load_factor(self):
        hist = [hist_entry(i, load={"host": 0.3}) for i in range(5)]
        calm = eta.estimate(run_in("full", 0), hist, load={"host": 0.3}, now=100000.0)
        busy = eta.estimate(run_in("full", 0), hist, load={"host": 1.6}, now=100000.0)
        self.assertEqual(calm["factor"], 1.0)
        self.assertGreater(busy["factor"], 1.3)
        self.assertGreater(busy["eta_s"], calm["eta_s"] * 1.3)
        self.assertEqual(eta.load_factor({"host": 9.0}, [{"host": 0.0}]), eta.FACTOR_MAX)
        self.assertEqual(eta.load_factor({}, [{"host": 0.5}]), 1.0)
        self.assertEqual(eta.load_factor({"host": 0.5}, []), 1.0)

    def test_test_rate(self):
        hist = [hist_entry(i) for i in range(5)]
        s = eta.estimate(run_in("image_again", 300, done=9, total=18), hist, load={"host": 0.5}, now=100000.0)
        # rest of the phase 300 s (same rate) + release + verify
        self.assertAlmostEqual(s["eta_s"], 300 + 120 + 600, delta=5)
        self.assertEqual(s["progress"]["done"], 9)
        self.assertIn("image again 9/18", eta.line(s))

    def test_repeat_phase_weighted_by_frequency(self):
        hist = [hist_entry(i, again=600 if i % 2 else 0) for i in range(6)]
        s = eta.estimate(run_in("full", 3200), hist, load={"host": 0.5}, now=100000.0)
        st, _ = eta.phase_stats(hist, "app", "gate")
        self.assertAlmostEqual(st["full_again"]["rate"], 0.5)
        self.assertAlmostEqual(s["eta_s"], 320 + 300 + 3700 + 500 + 120 + 600, delta=5)

    def test_overrun_stays_positive(self):
        hist = [hist_entry(i) for i in range(5)]
        s = eta.estimate(run_in("verify", 5000), hist, load={"host": 0.5}, now=100000.0)
        self.assertGreater(s["eta_s"], 0)

    def test_vanished_and_done(self):
        r = run_in("full", 100)
        r["state"] = "vanished"
        self.assertIsNone(eta.estimate(r, [hist_entry(1)])["eta_s"])
        self.assertEqual(eta.estimate(dict(run_in("full", 100), end=5, result="ok"), [])["eta_s"], 0)

    def test_writer_knows_the_duration(self):
        r = {"format": eta.FORMAT, "profile": "p/m", "kind": "remote_test", "current": 0, "start": 0, "stamp": 100,
             "phases": [{"name": "queue", "status": "running", "start": 0, "expected_s": 900, "expected_at": 100},
                        {"name": "test", "status": "open"}]}
        hist = [{"profile": "p/m", "kind": "remote_test", "result": "ok", "phases": {"queue": 5, "test": 600},
                 "skipped": []} for _ in range(3)]
        s = eta.estimate(r, hist, load={}, now=400)
        self.assertEqual(s["eta_s"], 600 + 600)
        self.assertEqual(s["phase"], "waiting for a test slot")


class Task(unittest.TestCase):
    def test_run_plus_steps(self):
        r = eta.task_eta(3600, 3, 6, run_est={"eta_s": 1800, "lo_s": 1200, "hi_s": 3000})
        self.assertEqual(r["source"], "run")
        self.assertEqual(r["eta_s"], 1800 + 2 * 1200)

    def test_only_steps(self):
        self.assertEqual(eta.task_eta(1200, 2, 6)["eta_s"], int(600 * 3.5))

    def test_experience_and_unknown(self):
        self.assertEqual(eta.task_eta(600, 0, 0, experience_s=3600)["eta_s"], 3000)
        self.assertIsNone(eta.task_eta(600, None, None)["eta_s"])


class Queue(unittest.TestCase):
    def test_fifo(self):
        # slots free in 10 / 30 min, one free; waiting tasks 20 min each
        self.assertEqual(eta.start_times([600, 1800, 0], [1200, 1200, 1200, 1200]), [0, 600, 1200, 1800])

    def test_unknown_slots(self):
        self.assertEqual(eta.start_times([None, None], [600]), [None])
        self.assertEqual(eta.start_times([None, 900], [600, None]), [900, 1500])


class Files(unittest.TestCase):
    def setUp(self):
        self.o = tempfile.mkdtemp(prefix="aiw-test-eta-")
        self.addCleanup(shutil.rmtree, self.o, ignore_errors=True)

    def test_writer_end_history(self):
        t = [1000.0]
        r = eta.Run("app", "gate", PH, title="gate 9.9.9", folder=self.o, heartbeat=False, clock=lambda: t[0]).start()
        r.phase("prepare")
        t[0] += 60
        r.phase("full", total=100)
        t[0] += 3000
        r.tests(done=100)
        r.phase("image")                          # full_again skipped
        d = json.load(open(r.path))
        self.assertEqual([p["status"] for p in d["phases"]][:4], ["done", "done", "skipped", "running"])
        runs = eta.load_runs(self.o, now=t[0])
        self.assertEqual((len(runs), runs[0]["state"]), (1, "running"))
        t[0] += 3500
        r.end("ok")
        h = eta.load_history(self.o)
        self.assertEqual(h[-1]["phases"], {"prepare": 60, "full": 3000, "image": 3500})
        self.assertEqual(h[-1]["tests"], {"full": 100})
        self.assertIn("release", h[-1]["skipped"])

    def test_aborted_run_does_not_measure_current_phase(self):
        t = [0.0]
        r = eta.Run("p", "a", ["x", "y"], folder=self.o, heartbeat=False, clock=lambda: t[0]).start()
        r.phase("x")
        t[0] = 50
        r.phase("y")
        t[0] = 60
        r.end("aborted")
        self.assertEqual(eta.load_history(self.o)[-1]["phases"], {"x": 50})

    def test_missing_or_broken_file(self):
        gone = os.path.join(self.o, "does-not-exist")
        self.assertEqual(eta.load_runs(gone), [])
        self.assertEqual(eta.load_history(gone), [])
        open(os.path.join(self.o, "broken.json"), "w").write("{not json")
        open(os.path.join(self.o, "history-x.jsonl"), "w").write("broken\n{}\n")
        self.assertEqual(eta.load_runs(self.o), [])
        self.assertEqual(eta.load_history(self.o), [])

    def test_vanished(self):
        with open(os.path.join(self.o, "v.json"), "w") as fh:
            json.dump({"format": eta.FORMAT, "run": "v", "stamp": 1000, "start": 900, "phases": []}, fh)
        runs = eta.load_runs(self.o, now=1000 + eta.VANISHED_S + 5)
        self.assertEqual(runs[0]["state"], "vanished")
        self.assertIsNone(eta.estimate(runs[0], [])["eta_s"])

    def test_writer_without_permission_is_silent(self):
        r = eta.Run("p", "a", ["x"], folder="/proc/no-folder-possible", heartbeat=False).start()
        r.phase("x").tests(1, 2).expected(5).end("ok")      # must not raise
        self.assertFalse(r.on)

    def test_default_folder_from_environment(self):
        old = os.environ.get("AIWERKSTATT_PROGRESS_DIR")
        os.environ["AIWERKSTATT_PROGRESS_DIR"] = self.o
        try:
            self.assertEqual(eta.progress_dir(), self.o)
        finally:
            if old is None:
                del os.environ["AIWERKSTATT_PROGRESS_DIR"]
            else:
                os.environ["AIWERKSTATT_PROGRESS_DIR"] = old


if __name__ == "__main__":
    unittest.main()
