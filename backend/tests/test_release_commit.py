"""A check result is valid for exactly one commit: the release tag goes on that commit.

Run: cd backend && python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import publish  # noqa: E402


class ReleasePayload(unittest.TestCase):
    def test_without_commit_nothing_is_pinned(self):
        body = publish.release_payload("v1.0.0", "notes")
        self.assertNotIn("target_commitish", body)
        self.assertEqual((body["tag_name"], body["draft"], body["prerelease"]), ("v1.0.0", False, False))

    def test_commit_pins_the_tag(self):
        sha = "ab" * 20
        self.assertEqual(publish.release_payload("v1.0.0", "", " %s " % sha.upper())["target_commitish"], sha)

    def test_short_or_odd_commit_is_refused(self):
        for bad in ("abc1234", "main", "g" * 40, "ab" * 21, "HEAD~1"):
            with self.assertRaises(ValueError):
                publish.release_payload("v1.0.0", "", bad)


if __name__ == "__main__":
    unittest.main()
