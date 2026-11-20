"""Reading proof (0.29.0, opt-in): `READ:` line parser, reading list, orchestrator wiring, session behaviour.

The session tests run agent-runner/session.py against a fake `claude`.
Run: cd backend && python3 -m unittest tests.test_reading_proof     (~15 s)
"""
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-read-data-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
RUNNER = os.path.join(HERE, "..", "..", "agent-runner")

import context_pack  # noqa: E402

# Loaded by path: the runner folder has its own `providers` package that must not shadow the backend's.
_spec = importlib.util.spec_from_file_location("reading_proof", os.path.join(RUNNER, "reading_proof.py"))
reading_proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reading_proof)

SESSION = os.path.join(RUNNER, "session.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Parser(unittest.TestCase):
    def test_line_variants(self):
        for text, want in (
            ("Done.\nREAD: alpha-master.md, index_alpha.md", ["alpha-master.md", "index_alpha.md"]),
            ("**READ:** `alpha-master`", ["alpha-master"]),
            ("text\n> read : a.md; b.md\n", ["a.md", "b.md"]),
            ("READ: first\nmore text\nREAD: second", ["second"]),          # the last line counts
            ("READ: none - nothing matched", ["none - nothing matched"]),
        ):
            with self.subTest(text):
                self.assertEqual(reading_proof.parse_read_line(text), want)

    def test_no_line_empty_or_template(self):
        for text in ("", "Done.", "I did read: something in the middle", "READ:", "READ: -", "READ: <file1>, <file2>"):
            with self.subTest(text):
                self.assertIsNone(reading_proof.parse_read_line(text))

    def test_check_with_places_needs_one_of_them(self):
        stems = ["alpha-master", "index_alpha"]
        self.assertEqual(reading_proof.check("READ: Alpha-Master.md", stems)[0], True)
        self.assertEqual(reading_proof.check("READ: `index_alpha`", stems)[0], True)
        ok, entries = reading_proof.check("READ: something-else.md", stems)
        self.assertFalse(ok)
        self.assertEqual(entries, ["something-else.md"])
        self.assertEqual(reading_proof.check("READ: none - no time", stems)[0], False)
        self.assertEqual(reading_proof.check("no line at all", stems), (False, None))

    def test_check_without_places_any_line_counts(self):
        self.assertTrue(reading_proof.check("READ: none - nothing fitting")[0])
        self.assertTrue(reading_proof.check("READ: whatever.md", [])[0])
        self.assertFalse(reading_proof.check("READ: <file1>")[0])
        self.assertFalse(reading_proof.check("Done.")[0])

    def test_nudge_text_lists_places(self):
        t = reading_proof.nudge_text(["/k/alpha master.md"])
        self.assertIn("Reading proof missing: read first, then confirm.", t)
        self.assertIn("/k/alpha master.md", t)
        self.assertIn("no places named", reading_proof.nudge_text([]))


class ReadingList(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="aiw-read-")
        self.addCleanup(shutil.rmtree, self.t, True)
        self.kb = os.path.join(self.t, "kb")
        self.mem = os.path.join(self.t, "mem")
        write(os.path.join(self.kb, "Alpha master.md"), "# Alpha\nintro\n")
        write(os.path.join(self.mem, "index_alpha.md"), "- [alpha thing](a.md) alpha alpha\n")
        write(os.path.join(self.mem, "index_other.md"), "- [other](o.md)\n")
        write(os.path.join(self.mem, "MEMORY.md"), "# index\n")
        write(os.path.join(self.mem, "feedback_calibrate_sensor_drift.md"),
              "---\nname: sensor drift\n---\nCalibrate the sensor before each drift check.\n")
        write(os.path.join(self.mem, "feedback_unrelated_topic.md"), "---\nname: other\n---\nNothing to see.\n")
        for i in range(6):                  # filler notes: a word in all of them is common and must not count
            write(os.path.join(self.mem, "note_%d.md" % i), "---\nname: n%d\n---\nupdate common words here\n" % i)

    def test_docs_index_and_memory_hits(self):
        text, meta = context_pack.reading_list("alpha", "Calibrate sensor drift handling", self.kb, self.mem)
        self.assertFalse(meta["light"])
        self.assertEqual(meta["docs"], ["Alpha master.md"])
        self.assertEqual(meta["index"], "index_alpha.md")
        self.assertEqual(meta["memory"], ["feedback_calibrate_sensor_drift.md"])
        self.assertEqual(meta["stems"], ["alpha master", "index_alpha", "feedback_calibrate_sensor_drift"])
        self.assertEqual(len(meta["paths"]), 3)
        self.assertTrue(meta["required"] and not meta["none"])
        self.assertIn("Reading duty", text)
        self.assertIn("READ:", text)
        self.assertIn(os.path.join(self.mem, "index_alpha.md"), text)
        self.assertNotIn("MEMORY.md", text)
        self.assertNotIn("feedback_unrelated_topic", text)

    def test_light_titles_only_read_the_master_document(self):
        for title in ("Release train: fix the tag step", "Test: repair the sensor drift check", "gate is red again",
                      "alpha tests only", "Tests: fix the drift check"):
            with self.subTest(title):
                text, meta = context_pack.reading_list("alpha", title, self.kb, self.mem)
                self.assertTrue(meta["light"])
                self.assertEqual(meta["docs"], ["Alpha master.md"])
                self.assertEqual(meta["index"], "")
                self.assertEqual(meta["memory"], [])
                self.assertIn("release-train fix or test order", text)
        self.assertFalse(context_pack.is_light("Add a test dashboard"))

    def test_common_word_does_not_count(self):
        words = context_pack.title_words("Update common words")
        self.assertIn("common", words)
        _t, meta = context_pack.reading_list("zzz", "Update common words", self.kb, self.mem)
        self.assertEqual(meta["memory"], [])           # "common" and "words" sit in 6 of 10 notes: rarity filter

    def test_no_folders_still_requires_the_line(self):
        text, meta = context_pack.reading_list("alpha", "something", "", "")
        self.assertTrue(meta["none"] and meta["required"])
        self.assertEqual(meta["stems"], [])
        self.assertIn("READ: none - <reason>", text)
        _t, meta2 = context_pack.reading_list("alpha", "something", os.path.join(self.t, "missing"), "/nonexistent")
        self.assertTrue(meta2["none"])


class OrchestratorWiring(unittest.TestCase):
    def setUp(self):
        import orchestrator
        self.o = orchestrator
        self.t = tempfile.mkdtemp(prefix="aiw-read-orch-")
        self.addCleanup(shutil.rmtree, self.t, True)
        write(os.path.join(self.t, "kb", "Alpha master.md"), "# Alpha\nintro\n")
        for name, val in (("EVENTS_BIND", os.path.join(self.t, "events")), ("KNOWLEDGE_DIR", os.path.join(self.t, "kb")),
                          ("MEMORY_DIR", "")):
            self.addCleanup(setattr, orchestrator, name, getattr(orchestrator, name))
            setattr(orchestrator, name, val)
        os.makedirs(orchestrator.EVENTS_BIND)
        for k in ("AIW_READING_PROOF", "AIW_READING_MAX_NUDGES"):
            old = os.environ.pop(k, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, k, old)
        self.addCleanup(os.environ.pop, "AIW_READING_PROOF", None)
        self.addCleanup(os.environ.pop, "AIW_READING_MAX_NUDGES", None)

    def test_off_by_default(self):
        self.assertEqual(self.o.Orchestrator._reading_proof("alpha", "title", 5), ("", {}))
        self.assertFalse(os.listdir(self.o.EVENTS_BIND))

    def test_on_gives_text_environment_and_stored_list(self):
        os.environ["AIW_READING_PROOF"] = "1"
        text, env = self.o.Orchestrator._reading_proof("alpha", "title", 5)
        self.assertIn("Reading duty", text)
        self.assertEqual(env["AIW_READING_PROOF"], "1")
        self.assertEqual(json.loads(env["AIW_READING_STEMS"]), ["alpha master"])
        self.assertEqual(json.loads(env["AIW_READING_PATHS"]), [os.path.join(self.t, "kb", "Alpha master.md")])
        self.assertEqual(env["AIW_READING_MAX_NUDGES"], "1")
        with open(os.path.join(self.o.EVENTS_BIND, "reading-list-5.json")) as f:
            self.assertEqual(json.load(f)["docs"], ["Alpha master.md"])
        os.environ["AIW_READING_MAX_NUDGES"] = "2"
        self.assertEqual(self.o.Orchestrator._reading_proof("alpha", "title", 6)[1]["AIW_READING_MAX_NUDGES"], "2")

    def test_a_broken_list_never_blocks_a_run(self):
        os.environ["AIW_READING_PROOF"] = "1"
        old = self.o.context_pack.reading_list
        self.o.context_pack.reading_list = lambda *a, **k: 1 / 0
        self.addCleanup(setattr, self.o.context_pack, "reading_list", old)
        self.assertEqual(self.o.Orchestrator._reading_proof("alpha", "title", 5), ("", {}))


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
plan = os.environ["FAKE_PLAN"].split(",")
counter = os.environ["FAKE_COUNTER"]
sid = "11111111-2222-4333-8444-555555555555"


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


out({"type": "system", "subtype": "init"})
for line in sys.stdin:
    if not line.strip():
        continue
    with open(os.environ["FAKE_INPUT"], "a") as f:
        f.write(line)
    try:
        n = int(open(counter).read())
    except (OSError, ValueError):
        n = 0
    open(counter, "w").write(str(n + 1))
    kind = plan[n] if n < len(plan) else "plain"
    text = {"plain": "Done.", "read": "Done.\nREAD: Alpha master.md, index_alpha.md",
            "none": "Done.\nREAD: none - nothing fitting",
            "template": "Done.\nREAD: <file1>, <file2>",
            "wrong": "Done.\nREAD: unrelated.md"}[kind]
    out({"type": "result", "subtype": "success", "is_error": False, "result": text})
'''

PLACES = {"AIW_READING_PROOF": "1", "AIW_READING_STEMS": json.dumps(["alpha master", "index_alpha"]),
          "AIW_READING_PATHS": json.dumps(["/k/Alpha master.md", "/m/index_alpha.md"])}


class SessionReadingProof(unittest.TestCase):
    def run_session(self, plan, env_extra=None, limit=60):
        t = tempfile.mkdtemp(prefix="aiw-read-sess-")
        self.addCleanup(shutil.rmtree, t, True)
        bindir = os.path.join(t, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        for d in ("events", "inbox", "usage", "wt", "vault/demo"):
            os.makedirs(os.path.join(t, d))
        first = os.path.join(t, "first.txt")
        with open(first, "w") as f:
            f.write("Do the task.")
        env = dict(os.environ)
        env.update({
            "PATH": bindir + os.pathsep + env.get("PATH", ""), "WK_SLUG": "demo", "WK_THREAD_ID": "7",
            "WK_REPO": "local/demo", "WK_WT": os.path.join(t, "wt"), "WK_MODEL": "claude-haiku-5-5",
            "WK_FIRST_MSG_FILE": first, "WK_FIRST_TASK": "100", "WK_EVENTS": os.path.join(t, "events"),
            "WK_INBOX": os.path.join(t, "inbox"), "WK_RUNLOG": os.path.join(t, "run.log"),
            "WK_USAGE_DIR": os.path.join(t, "usage"), "WK_IDLE_TIMEOUT": "1", "WK_STALL_TIMEOUT": "20",
            "WK_STREAM_RETRY_MAX": "0", "AIW_API_RETRY_DELAYS": "", "AIW_KNOWLEDGE_DIR": os.path.join(t, "vault/demo"),
            "FAKE_PLAN": ",".join(plan), "FAKE_COUNTER": os.path.join(t, "counter"),
            "FAKE_INPUT": os.path.join(t, "input.jsonl"),
        })
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "AIW_MEMORY_DIR", "AIW_READING_PROOF", "AIW_READING_STEMS",
                  "AIW_READING_PATHS", "AIW_READING_MAX_NUDGES"):
            env.pop(k, None)
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, SESSION], env=env, capture_output=True, text=True, timeout=limit)
        events = []
        for n in sorted(os.listdir(os.path.join(t, "events"))):
            if n.endswith(".json") and not n.startswith(("status", "limit")):
                with open(os.path.join(t, "events", n)) as f:
                    events.append(json.load(f))
        result = json.loads(r.stdout.strip().splitlines()[-1])
        sent = []
        if os.path.exists(env["FAKE_INPUT"]):
            with open(env["FAKE_INPUT"]) as f:
                sent = [json.loads(x) for x in f]
        return result, [(e["type"], e["text"]) for e in events], sent

    @staticmethod
    def replies(pairs):
        return [tx for ty, tx in pairs if ty == "reply"]

    def test_line_present_no_follow_up(self):
        res, pairs, sent = self.run_session(["read"], PLACES)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.replies(pairs)), 1)
        self.assertIn("READ: Alpha master.md", self.replies(pairs)[0])
        self.assertFalse(any("Reading proof missing" in tx for _ty, tx in pairs))
        self.assertEqual(res["reading"], {"required": True, "entries": ["Alpha master.md", "index_alpha.md"],
                                          "valid": True, "nudges": 0, "missing": False})

    def test_none_line_is_valid_without_places(self):
        res, pairs, sent = self.run_session(["none"], {"AIW_READING_PROOF": "1"})
        self.assertEqual(len(sent), 1)
        self.assertTrue(res["reading"]["valid"])

    def test_missing_line_exactly_one_follow_up_then_reply(self):
        res, pairs, sent = self.run_session(["plain", "read"], PLACES)
        self.assertEqual(len(sent), 2)
        self.assertIn("Reading proof missing: read first, then confirm.", json.dumps(sent[1]))
        self.assertIn("/k/Alpha master.md", json.dumps(sent[1]))
        self.assertEqual(len([1 for ty, tx in pairs if ty == "progress" and "Reading proof missing" in tx]), 1)
        self.assertEqual(len(self.replies(pairs)), 1)                  # the first answer is not sent out as the result
        self.assertIn("READ: Alpha master.md", self.replies(pairs)[0])
        self.assertEqual(res["reading"]["nudges"], 1)
        self.assertTrue(res["reading"]["valid"] and not res["reading"]["missing"])
        self.assertEqual(res["requeue"], [])

    def test_still_missing_after_the_follow_up_reply_goes_out_anyway(self):
        for plan in (["plain", "plain"], ["template", "wrong"]):
            with self.subTest(plan):
                res, pairs, sent = self.run_session(plan, PLACES)
                self.assertEqual(len(sent), 2)                         # exactly one follow-up, no loop
                self.assertEqual(len(self.replies(pairs)), 1)
                self.assertTrue(self.replies(pairs)[0].startswith("Done."))
                self.assertEqual(res["reading"]["nudges"], 1)
                self.assertTrue(res["reading"]["missing"] and not res["reading"]["valid"])
                self.assertEqual(res["requeue"], [])

    def test_max_nudges_is_configurable(self):
        res, pairs, sent = self.run_session(["plain", "plain", "plain"], dict(PLACES, AIW_READING_MAX_NUDGES="2"))
        self.assertEqual(len(sent), 3)
        self.assertEqual(res["reading"]["nudges"], 2)
        self.assertEqual(len(self.replies(pairs)), 1)
        res0, pairs0, sent0 = self.run_session(["plain"], dict(PLACES, AIW_READING_MAX_NUDGES="0"))
        self.assertEqual(len(sent0), 1)
        self.assertEqual(len(self.replies(pairs0)), 1)

    def test_without_the_variable_nothing_changes(self):
        res, pairs, sent = self.run_session(["plain"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.replies(pairs), ["Done."])
        self.assertIsNone(res["reading"])
        self.assertFalse(any("Reading proof" in tx for _ty, tx in pairs))


if __name__ == "__main__":
    unittest.main()
