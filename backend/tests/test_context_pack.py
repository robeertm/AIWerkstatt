"""Context pack: selection, size cap, secret omission, no-match (0.17.0).

Run: cd backend && python3 -m unittest discover -s tests
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest

_TMP_DATA = tempfile.mkdtemp(prefix="aiw-ctx-data-")
atexit.register(shutil.rmtree, _TMP_DATA, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP_DATA)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import context_pack  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def doc(order):
    """A master document with dated sections in the given order plus a pitfalls section."""
    secs = {d: "## Change %s\nbody-%s\n" % (d, d) for d in
            ("2026-01-05", "2026-02-10", "10.03.2026", "2026-04-20", "2026-05-01")}
    return ("---\ntitle: x\n---\n# Alpha\nintro-line\n\n" + "".join(secs[d] for d in order)
            + "\n## Pitfalls\nnever-do-this\n\n## Unrelated\nfiller\n")


class ContextPack(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="aiw-ctx-")
        self.vault = os.path.join(self.t, "vault", "alpha")
        self.kb = os.path.join(self.t, "kb")
        self.mem = os.path.join(self.t, "mem")
        write(os.path.join(self.vault, "knowledge.md"), "# Project knowledge\nown-note\n")

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def test_newest_three_either_order_with_head_pitfalls_and_lines(self):
        asc = ["2026-01-05", "2026-02-10", "10.03.2026", "2026-04-20", "2026-05-01"]
        for name, order in (("newest-last", asc), ("newest-first", asc[::-1])):
            with self.subTest(name):
                write(os.path.join(self.kb, "Alpha master.md"), doc(order))
                text, meta = context_pack.build("alpha", "do something", self.vault, self.kb, "")
                for want in ("body-2026-05-01", "body-2026-04-20", "body-10.03.2026", "intro-line", "never-do-this"):
                    self.assertIn(want, text)
                for not_want in ("body-2026-01-05", "body-2026-02-10", "filler", "title: x"):
                    self.assertNotIn(not_want, text)
                self.assertIn("own-note", text)                       # the project's own file is always inserted
                self.assertRegex(text, r"Alpha master\.md, line \d+")   # path + line number
                self.assertFalse(meta["no_match"])
                self.assertEqual(len(meta["docs"]), 2)

    def test_match_by_title_keywords_when_slug_does_not_match(self):
        write(os.path.join(self.kb, "Billing service.md"), "# Billing\nbilling-intro\n")
        write(os.path.join(self.kb, "Other.md"), "# Other\nother-intro\n")
        text, meta = context_pack.build("zzz", "Fix the billing export", "", self.kb, "")
        self.assertIn("billing-intro", text)
        self.assertNotIn("other-intro", text)

    def test_size_cap_is_hard_and_truncation_names_path_and_line(self):
        big = "# Alpha\n" + "x" * 5000 + "\n" + "".join(
            "## Change 2026-03-%02d\n%s\n" % (d, "y" * 4000) for d in range(1, 9)) + "## Pitfalls\n" + "p" * 4000
        write(os.path.join(self.kb, "alpha.md"), big)
        text, meta = context_pack.build("alpha", "", self.vault, self.kb, "")
        self.assertLessEqual(len(text.encode("utf-8")), 12000)
        self.assertEqual(meta["bytes"], len(text.encode("utf-8")))
        self.assertRegex(text, r"truncated — rest in \S*alpha\.md from line \d+")
        small, _ = context_pack.build("alpha", "", self.vault, self.kb, "", max_bytes=3000)
        self.assertLessEqual(len(small.encode("utf-8")), 3000)

    def test_multibyte_text_still_respects_the_byte_cap(self):
        write(os.path.join(self.kb, "alpha.md"), "# Alpha\n" + "ä—" * 6000 + "\n")
        text, _ = context_pack.build("alpha", "", self.vault, self.kb, "")
        self.assertLessEqual(len(text.encode("utf-8")), 12000)

    def test_secret_like_section_is_omitted_with_a_note(self):
        fake = "gh" + "p_" + "A1b2C3d4" * 4          # built by concatenation: no real token in the repo
        write(os.path.join(self.kb, "alpha.md"),
              "# Alpha\nintro\n## Setup 2026-05-01\nuse %s to log in\n## Notes 2026-04-01\nsafe-text\n" % fake)
        text, meta = context_pack.build("alpha", "", self.vault, self.kb, "")
        self.assertNotIn(fake, text)
        self.assertIn("omitted", text)
        self.assertIn("safe-text", text)
        self.assertEqual(meta["omitted"], 1)
        pw = "pass" + "word: " + "Zz9" * 12
        self.assertTrue(context_pack.looks_secret(pw))
        self.assertTrue(context_pack.looks_secret("-----BEGIN RSA PRIV" + "ATE KEY-----"))
        self.assertFalse(context_pack.looks_secret("the password is stored in the vault"))

    def test_no_match_says_so_explicitly(self):
        write(os.path.join(self.kb, "Unrelated.md"), "# Nope\n")
        text, meta = context_pack.build("alpha", "", self.vault, self.kb, "")
        self.assertTrue(meta["no_match"])
        self.assertIn("No master document found", text)
        self.assertIn("say in your result which note you used", text)
        # nothing configured and no own file at all: same sentence
        text2, meta2 = context_pack.build("alpha", "", os.path.join(self.t, "none"), "", "")
        self.assertTrue(meta2["no_match"])
        self.assertIn("No master document found", text2)
        self.assertEqual(meta2["docs"], [])

    def test_memory_index_contributes_newest_lines_but_not_memory_md(self):
        write(os.path.join(self.mem, "MEMORY.md"), "# Memory\n- [secret-standing-order](x.md) - never insert me\n")
        write(os.path.join(self.mem, "index_alpha.md"),
              "# Index\n" + "".join("- [entry %d](e%d.md) - about alpha\n" % (i, i) for i in range(30)))
        write(os.path.join(self.mem, "index_other.md"), "# Index\n- [unrelated](u.md) - nothing\n")
        text, meta = context_pack.build("alpha", "", self.vault, "", self.mem)
        self.assertEqual(meta["index"], "index_alpha.md")
        self.assertIn("- [entry 0]", text)
        self.assertIn("- [entry 11]", text)
        self.assertNotIn("- [entry 12]", text)
        self.assertNotIn("standing-order", text)
        self.assertNotIn("unrelated", text)


class OrchestratorWiring(unittest.TestCase):
    def test_pack_is_built_for_new_runs_and_metadata_stored(self):
        import orchestrator
        t = tempfile.mkdtemp(prefix="aiw-ctx-orch-")
        self.addCleanup(shutil.rmtree, t, True)
        write(os.path.join(t, "vault", "alpha", "knowledge.md"), "# Knowledge\nwired-note\n")
        old = orchestrator.EVENTS_BIND
        orchestrator.EVENTS_BIND = os.path.join(t, "events")
        os.makedirs(orchestrator.EVENTS_BIND)
        self.addCleanup(setattr, orchestrator, "EVENTS_BIND", old)
        pool = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
        text = pool._context_pack("alpha", "title", os.path.join(t, "vault", "alpha"), 5)
        self.assertIn("wired-note", text)
        with open(os.path.join(orchestrator.EVENTS_BIND, "context-pack-5.json")) as f:
            meta = json.load(f)
        self.assertEqual(len(meta["docs"]), 1)
        self.assertGreater(meta["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
