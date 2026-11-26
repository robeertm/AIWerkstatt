"""THIRD-PARTY-NOTICES.md must list every backend dependency and every production
frontend package, and the web image must carry it together with LICENSE.

Run: cd backend && python3 -m unittest tests.test_third_party_notices
Plain file reads only - no dependencies imported, no network.
"""
import json
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
        return f.read()


class ThirdPartyNotices(unittest.TestCase):
    notices = read("THIRD-PARTY-NOTICES.md")

    def test_every_backend_requirement_is_listed(self):
        missing = []
        for line in read("backend", "requirements.txt").splitlines():
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#;]+)", line.strip())
            if not m:
                continue
            want = "- `%s` %s - " % (m.group(1), m.group(2))
            # distribution names compare case-insensitively (Flask / flask)
            if want.lower() not in self.notices.lower():
                missing.append("%s %s" % m.groups())
        self.assertEqual(missing, [], "run scripts/drittlizenzen.py (see its header)")

    def test_every_production_frontend_package_is_listed(self):
        lock = json.loads(read("frontend", "package-lock.json"))
        missing = []
        for key, meta in lock["packages"].items():
            if not key or meta.get("dev"):
                continue
            name = key.split("node_modules/")[-1]
            if "- `%s` %s - " % (name, meta["version"]) not in self.notices:
                missing.append("%s %s" % (name, meta["version"]))
        self.assertEqual(missing, [], "run scripts/drittlizenzen.py (see its header)")

    def test_provider_clis_of_the_runner_image_are_listed(self):
        for pkg in re.findall(r"(@[\w-]+/[\w-]+)(?:@[\w.]+)?", read("agent-runner", "Dockerfile")):
            self.assertIn("`%s`" % pkg, self.notices, pkg)

    def test_web_image_ships_licence_and_notices(self):
        self.assertIn("COPY LICENSE THIRD-PARTY-NOTICES.md", read("Dockerfile"))
        ignore = read(".dockerignore").splitlines()
        self.assertNotIn("LICENSE", ignore)
        self.assertNotIn("THIRD-PARTY-NOTICES.md", ignore)


if __name__ == "__main__":
    unittest.main()
