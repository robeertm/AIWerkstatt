"""Live feed vital signs: "is the agent alive?" (0.16.0).

Run: cd backend && python3 -m unittest tests.test_agent_vitals   (or: python3 -m unittest discover -s tests)

Covers: open command (never a stall warning, even after 40 min), finished command, silent
thinking counted through the runner's `last_output`, quiet / yellow (10 min) / red (30 min),
a running subagent, empty feed, only the tail of a big file is read, and the API fields.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-vit-data-")
_TMP_WS = tempfile.mkdtemp(prefix="aiw-vit-ws-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
atexit.register(shutil.rmtree, _TMP_WS, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP_WS)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine  # noqa: E402

T0 = 1_800_000_000


def tool(at, text="$ pytest -q"):
    return {"act": "tool", "text": text, "at": at}


def result(at):
    return {"act": "result", "text": "ok", "at": at}


class Vitals(unittest.TestCase):
    def v(self, entries, vor=0, last_output=0):
        return engine.vitals_from(entries, last_output, now=T0 + vor)

    def test_open_command_has_since_and_no_stall_warning(self):
        d = self.v([{"act": "say", "text": "go", "at": T0}, tool(T0 + 10)], vor=250)
        self.assertEqual(d["state"], "command")
        self.assertEqual(d["command_since"], T0 + 10)
        self.assertEqual(d["command"], "$ pytest -q")
        self.assertEqual(d["last_activity"], T0 + 10)
        self.assertEqual(d["quiet_s"], 240)
        self.assertEqual(self.v([tool(T0)], vor=40 * 60)["state"], "command")   # a long test is not suspicious

    def test_finished_command_is_not_open(self):
        d = self.v([tool(T0), result(T0 + 2)], vor=30)
        self.assertEqual(d["state"], "thinking")
        self.assertNotIn("command_since", d)

    def test_silent_thinking_counts_through_last_output(self):
        entries = [tool(T0), result(T0 + 2)]
        self.assertEqual(self.v(entries, vor=12 * 60)["state"], "stalled_yellow")
        d = self.v(entries, vor=12 * 60, last_output=T0 + 12 * 60 - 5)      # runner saw bytes 5 s ago
        self.assertEqual((d["state"], d["quiet_s"]), ("thinking", 5))

    def test_quiet_then_yellow_then_red(self):
        e = [tool(T0), result(T0 + 1)]
        self.assertEqual(self.v(e, vor=3 * 60)["state"], "quiet")
        self.assertEqual(self.v(e, vor=9 * 60 + 59)["state"], "quiet")
        self.assertEqual(self.v(e, vor=11 * 60)["state"], "stalled_yellow")
        self.assertEqual(self.v(e, vor=29 * 60)["state"], "stalled_yellow")
        self.assertEqual(self.v(e, vor=31 * 60)["state"], "stalled_red")

    def test_thresholds_are_the_agreed_ones(self):
        self.assertEqual((engine.THINKING_S, engine.QUIET_YELLOW_S, engine.QUIET_RED_S), (90, 600, 1800))

    def test_subagent_is_not_a_plain_command(self):
        e = [tool(T0, "Agent: search the repo")]
        self.assertEqual(self.v(e, vor=60)["state"], "subagent")
        self.assertEqual(self.v(e, vor=31 * 60)["state"], "stalled_red")        # an open Agent call excuses nothing

    def test_plan_calls_pair_with_their_result(self):
        e = [{"act": "plan", "text": "plan", "at": T0}, result(T0 + 1)]
        self.assertEqual(self.v(e, vor=5)["state"], "thinking")

    def test_nothing_to_judge(self):
        self.assertEqual(self.v([]), {})
        self.assertEqual(self.v([{"act": "say", "text": "x"}]), {})             # no timestamps at all


class FromFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aiw-vit-ev-")
        self.old = engine.EVENTS_DIR
        engine.EVENTS_DIR = self.dir

    def tearDown(self):
        engine.EVENTS_DIR = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, lines):
        with open(os.path.join(self.dir, "live-7.jsonl"), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))

    def test_only_the_tail_of_a_big_file_is_read(self):
        big = {"act": "result", "text": "y", "full": "y" * 4000, "at": T0}
        self.feed([big] * 300 + [tool(T0 + 5, "$ sleep 600")])
        self.assertGreater(os.path.getsize(os.path.join(self.dir, "live-7.jsonl")), engine.LIVE_TAIL_BYTES)
        d = engine.live_vitals(7, now=T0 + 60)
        self.assertEqual((d["state"], d["command_since"]), ("command", T0 + 5))

    def test_read_live_carries_the_new_fields(self):
        now = int(time.time())
        self.feed([tool(now - 240)])
        with open(os.path.join(self.dir, "status-7.json"), "w") as f:
            json.dump({"alive": True, "ctx_pct": 3, "pending": 1, "last_output": now - 200}, f)
        d = engine.read_live(7)
        self.assertEqual(d["state"], "command")
        self.assertAlmostEqual(d["command_since"], now - 240, delta=2)
        self.assertAlmostEqual(d["last_activity"], now - 200, delta=2)    # runner saw bytes later than the last line
        self.assertAlmostEqual(d["quiet_s"], 200, delta=3)
        self.assertAlmostEqual(d["now"], now, delta=3)
        self.assertEqual(len(d["entries"]), 1)                              # the feed itself is unchanged


if __name__ == "__main__":
    unittest.main()
