#!/usr/bin/env python3
"""Tests for release_check.py. No network: a fake object replaces GitHub, the registry and the download page.

    python3 -m unittest scripts/release-check/test_release_check.py
"""
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_check as rc  # noqa: E402

T0 = 1795300000.0


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Fake:
    """Plays GitHub (releases, assets, contents, runs), a registry and a download page."""

    def __init__(self):
        self.releases = {}      # repo -> [release]
        self.contents = {}      # (repo, path, ref) -> text
        self.asset_data = {}    # id -> bytes
        self.images = {}        # (name, tag) -> digest
        self.attachments = {}   # (name, digest) -> [predicate type]
        self.pages = {}
        self.runs = {}          # repo -> [run]
        self.tags = {}          # repo -> [tag]
        self.latest = {}
        self.actions = []
        self._id = 100

    def release(self, repo, v, files, t=T0 - 86400, pre=False, body="What is new in this version: plenty.", latest=True):
        assets = []
        for name, data in files.items():
            self._id += 1
            self.asset_data[self._id] = data
            assets.append({"id": self._id, "name": name, "size": len(data), "state": "uploaded",
                           "digest": "sha256:" + hashlib.sha256(data).hexdigest()})
        r = {"tag_name": "v" + v, "draft": False, "prerelease": pre, "body": body, "assets": assets,
             "published_at": iso(t), "created_at": iso(t)}
        self.releases.setdefault(repo, []).insert(0, r)
        if latest:
            self.latest[repo] = r
        return r

    # network interface
    def gh_json(self, path):
        m = re.match(r"repos/([^/]+/[^/]+)/(.*)$", path)
        repo, rest = m.group(1), m.group(2)
        if rest.startswith("releases?"):
            return self.releases.get(repo, [])
        if rest == "releases/latest":
            return self.latest.get(repo)
        if rest.startswith("releases/tags/"):
            t = rest.split("/")[-1]
            return next((r for r in self.releases.get(repo, []) if r["tag_name"] == t), None)
        if rest.startswith("actions/runs"):
            return {"workflow_runs": self.runs.get(repo, [])}
        if rest.startswith("git/matching-refs/tags/"):
            return [{"ref": "refs/tags/" + t} for t in self.tags.get(repo, [])]
        if rest.startswith("commits/"):
            t = rest.split("/", 1)[1]
            return {"commit": {"committer": {"date": iso(T0 - 86400)}}} if t in self.tags.get(repo, []) else None
        raise AssertionError("unexpected: " + path)

    def file(self, repo, path, ref):
        return self.contents.get((repo, path, ref))

    def asset_text(self, repo, asset_id):
        return self.asset_data[asset_id].decode()

    def asset_hash(self, repo, asset_id):
        d = self.asset_data[asset_id]
        return hashlib.sha256(d).hexdigest(), hashlib.blake2b(d, digest_size=64).digest(), len(d)

    def gh_action(self, args, timeout=300):
        self.actions.append(args)
        return True, ""

    def image_digest(self, name, tag):
        return self.images.get((name, tag))

    def image_attachments(self, name, digest):
        return self.attachments.get((name, digest), [])

    def image_tags(self, name):
        return [t for (n, t) in self.images if n == name]

    def page(self, url):
        return self.pages.get(url, "")


# minisign helpers (real Ed25519 keys, so the verification really computes)
def make_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    k = Ed25519PrivateKey.generate()
    pk = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    keyid = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    text = "untrusted comment: minisign public key\n%s\n" % base64.b64encode(b"Ed" + keyid + pk).decode()
    return k, keyid, base64.b64encode(text.encode()).decode()


def sign(k, keyid, data):
    sig = k.sign(hashlib.blake2b(data, digest_size=64).digest())
    trusted = b"timestamp:1\tfile:x"
    glob = k.sign(sig + trusted)
    text = "untrusted comment: sig\n%s\ntrusted comment: %s\n%s\n" % (
        base64.b64encode(b"ED" + keyid + sig).decode(), trusted.decode(), base64.b64encode(glob).decode())
    return base64.b64encode(text.encode()).decode()


DECL_APP = {
    "name": "App", "repo": "o/app", "release": "github", "notes": True, "latest": True, "grace_min": 60,
    "assets": [
        {"pattern": "app-mobile-{v}.apk", "title": "Mobile package (APK)"},
        {"pattern": "app_{v}_amd64.AppImage", "title": "Linux", "signature": ".sig"},
        {"pattern": "latest.json"},
        {"pattern": "SHA256SUMS.txt"},
    ],
    "checksums": {"file": "SHA256SUMS.txt", "all": True},
    "updater_manifest": {"file": "latest.json"},
    "minisign": {"repo_file": "updater.conf.json", "json_path": "updater.pubkey"},
    "images": [{"name": "registry.example/o/app", "tags": ["{v}", "latest"], "latest_equal": True, "signed": True}],
    "pages": [{"probe": "app-dl", "title": "Download page", "patterns": ["app-mobile-{v}.apk"]}],
    "version_files": [{"repo": "o/addons", "path": "app/config.yaml", "ref": "main", "pattern": "^version: \"{v}\"$",
                       "title": "Add-on"}],
    "heal": [{"for": ["app-mobile-{v}.apk"], "kind": "command", "command": ["true"], "title": "attach package"},
             {"kind": "rerun_ci"}, {"kind": "promote"}],
}
IMG = "registry.example/o/app"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rc-test-")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir)
        self.projects = {"app": json.loads(json.dumps(DECL_APP))}
        self.on_red = ["notify", "--x"]
        self.excluded = {"legacy": "no longer shipped"}
        self.net = Fake()
        self.clock = [T0]
        self.hooks, self.ran = [], []
        self.run_heals = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def full(self, v="1.2.0", **opt):
        """A complete App release (everything green)."""
        k, keyid, pub = getattr(self, "key", None) or make_key()
        self.key = (k, keyid, pub)
        app = b"APPIMAGE-" + v.encode()
        files = {"app-mobile-%s.apk" % v: b"APK" + v.encode(), "app_%s_amd64.AppImage" % v: app,
                 "app_%s_amd64.AppImage.sig" % v: sign(k, keyid, app).encode()}
        sig = files["app_%s_amd64.AppImage.sig" % v].decode()
        files["latest.json"] = json.dumps({"version": v, "platforms": {"linux-x86_64": {
            "url": "https://example.test/o/app/releases/download/v%s/app_%s_amd64.AppImage" % (v, v),
            "signature": sig}}}).encode()
        for name in opt.get("without", []):
            files.pop(name % v if "%s" in name else name, None)
        sums = "".join("%s  %s\n" % (hashlib.sha256(d).hexdigest(), n) for n, d in sorted(files.items()))
        if opt.get("bad_sums"):
            sums = sums.replace(hashlib.sha256(files["app_%s_amd64.AppImage" % v]).hexdigest(), "0" * 64)
        files["SHA256SUMS.txt"] = sums.encode()
        self.net.release("o/app", v, files, t=opt.get("t", T0 - 86400), pre=opt.get("pre", False))
        self.net.contents[("o/app", "updater.conf.json", "v" + v)] = json.dumps({"updater": {"pubkey": pub}})
        self.net.contents[("o/addons", "app/config.yaml", "main")] = 'name: App\nversion: "%s"\n' % v
        self.net.images[(IMG, v)] = "sha256:" + "a" * 64
        self.net.images[(IMG, "latest")] = opt.get("latest_digest", "sha256:" + "a" * 64)
        self.net.attachments[(IMG, "sha256:" + "a" * 64)] = ["https://sigstore.dev/cosign/sign/v1"]
        self.net.pages["https://dl.example/download"] = opt.get("page", "<a>app-mobile-%s.apk</a>" % v)

    def checker(self, **kw):
        cfg = {"format": "deliverables/1", "excluded": self.excluded, "projects": self.projects, "on_red": self.on_red}
        if self.on_red is None:
            del cfg["on_red"]
        rc.write_json(os.path.join(self.state_dir, "probes.json"), {"app-dl": "https://dl.example/download"})

        def runner(project, h, res):
            self.ran.append((project, h["kind"]))
            if h["kind"] == "command" and self.run_heals:
                self.run_heals()
            return True, "ok"

        return rc.Checker(cfg, self.state_dir, os.path.join(self.tmp, "out", "status.json"), net=self.net,
                          now=lambda: self.clock[0], out=lambda t: None, runner=runner,
                          hook=lambda cmd, path: self.hooks.append((cmd, path)) or True, **kw)

    def item(self, status, name="app"):
        return next(r for r in status["projects"] if r["project"] == name)

    def go(self, name="app", **kw):
        return self.item(self.checker(**kw).check(), name)

    def confirmed(self, name="app", **kw):
        """First run sees the gap (waiting, confirmation), the second after CONFIRM_S acts."""
        r = self.go(name, **kw)
        self.assertEqual(r["stage"], "waiting", r)
        self.assertIn("confirmed", r["text"])
        self.clock[0] += rc.CONFIRM_S + 30
        return self.go(name, **kw)

    def until_red(self, name="app"):
        r = None
        for _ in range(6):
            r = self.go(name)
            if r["stage"] == "red":
                return r
            self.clock[0] += rc.HEAL_WAIT_S + 60
        self.fail("never red: %s" % r)


class Complete(Base):
    def test_everything_present_is_green(self):
        self.full()
        st = self.checker().check()
        r = self.item(st)
        self.assertEqual(r["stage"], "green", r)
        self.assertEqual(r["gaps"], [])
        self.assertGreaterEqual(r["checked"], 12)
        self.assertEqual(st["line"], "Release check: all green (1 projects)")
        self.assertEqual(st["format"], "release-check/1")
        self.assertEqual(st["overall"], "green")
        self.assertEqual(self.hooks, [])
        self.assertEqual(rc.summary_line(st), "RELEASE-CHECK: green (1 projects)")

    def test_minisign_is_downloaded_only_once(self):
        self.full()
        self.checker().check()
        c2 = self.checker()
        loaded = []
        orig = self.net.asset_hash
        self.net.asset_hash = lambda *a: loaded.append(a) or orig(*a)
        c2.check()
        self.assertEqual(loaded, [], "a confirmed signature must not be downloaded again")


class MissingPackage(Base):
    """A release that came out without the mobile package although the README offers it."""

    def test_missing_inside_grace_waits(self):
        self.full(without=["app-mobile-%s.apk"], t=T0 - 600)
        self.assertEqual(self.go()["stage"], "waiting")
        self.assertEqual(self.ran, [])

    def test_heal_then_red_hook_exactly_once(self):
        self.full(without=["app-mobile-%s.apk"])
        r = self.confirmed()
        self.assertEqual(r["stage"], "gap")
        self.assertIn(("app", "command"), self.ran)
        self.assertTrue(any("APK" in x["text"] for x in r["gaps"]))
        self.assertTrue(any("missing in SHA256SUMS" in x["text"] or "missing:" in x["text"] for x in r["gaps"]))
        self.assertEqual(self.hooks, [])
        self.clock[0] += rc.HEAL_WAIT_S + 60
        st = self.checker().check()
        self.assertEqual(self.item(st)["stage"], "red")
        self.assertEqual(len(self.hooks), 1)
        cmd, path = self.hooks[0]
        self.assertEqual(cmd, ["notify", "--x"])
        rep = rc.read_json(path)
        self.assertEqual((rep["project"], rep["version"], rep["format"]), ("app", "1.2.0", "release-check/1"))
        self.assertTrue(any("Mobile package (APK) missing" in x["text"] for x in rep["gaps"]))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertEqual(st["overall"], "gaps")
        self.assertTrue(rc.summary_line(st).startswith("RELEASE-CHECK: gaps (app 1.2.0)"))
        self.assertIn("gaps -", self.checker().check()["line"])
        self.assertEqual(len(self.hooks), 1, "never a second hook per version")
        self.assertEqual(self.ran.count(("app", "command")), 1, "every heal way only once per version")

    def test_heal_works_green_no_hook(self):
        self.full(without=["app-mobile-%s.apk"])
        self.run_heals = lambda: (self.net.releases["o/app"].clear(), self.full())
        self.confirmed()
        self.clock[0] += 60
        r = self.go()
        self.assertEqual(r["stage"], "green")
        self.assertTrue(r.get("healed"))
        self.assertEqual(self.hooks, [])

    def test_page_does_not_offer_package(self):
        self.full(page="Mobile: coming soon")
        self.clock[0] += 3600
        r = self.go()
        self.assertIn("page", {x["kind"] for x in r["gaps"]})
        self.assertNotEqual(r["stage"], "green")

    def test_ci_running_waits_until_cap(self):
        self.full(without=["app-mobile-%s.apk"], t=T0 - 2 * 3600)
        self.net.runs["o/app"] = [{"id": 7, "head_branch": "v1.2.0", "status": "in_progress", "name": "mobile"}]
        self.assertEqual(self.go()["stage"], "waiting")
        self.clock[0] += rc.CI_CAP_S
        self.assertNotEqual(self.go()["stage"], "waiting")

    def test_failed_run_is_rerun(self):
        self.full(without=["app-mobile-%s.apk"])
        self.net.runs["o/app"] = [{"id": 9, "head_branch": "v1.2.0", "status": "completed", "conclusion": "failure",
                                   "name": "mobile"}]
        self.confirmed()
        self.assertIn(("app", "rerun_ci"), self.ran)


class SingleRules(Base):
    def gaps(self, **kw):
        self.full(**kw)
        return self.item(self.checker(heal=False).check())["gaps"]

    def test_checksum_mismatch(self):
        self.assertIn("checksum", {x["kind"] for x in self.gaps(bad_sums=True)})

    def test_signature_missing(self):
        kinds = {x["kind"] for x in self.gaps(without=["app_%s_amd64.AppImage.sig"])}
        self.assertIn("signature", kinds)
        self.assertIn("updater", kinds)

    def test_signature_invalid(self):
        self.full()
        rel = self.net.releases["o/app"][0]
        app = next(a for a in rel["assets"] if a["name"].endswith(".AppImage"))
        self.net.asset_data[app["id"]] = b"TAMPERED"
        app["digest"] = "sha256:" + hashlib.sha256(b"TAMPERED").hexdigest()
        r = self.item(self.checker(heal=False).check())
        self.assertTrue(any("Signature invalid" in x["text"] for x in r["gaps"]), r["gaps"])

    def test_updater_manifest_old_version(self):
        self.full()
        rel = self.net.releases["o/app"][0]
        lj = next(a for a in rel["assets"] if a["name"] == "latest.json")
        d = json.loads(self.net.asset_data[lj["id"]])
        d["version"] = "1.1.9"
        self.net.asset_data[lj["id"]] = json.dumps(d).encode()
        r = self.item(self.checker(heal=False).check())
        self.assertTrue(any("names version 1.1.9" in x["text"] for x in r["gaps"]))

    def test_stale_latest_image_heals_by_promote(self):
        self.full(latest_digest="sha256:" + "b" * 64)
        r = self.confirmed()
        self.assertIn("latest", {x["kind"] for x in r["gaps"]})
        self.assertIn(("app", "promote"), self.ran)

    def test_latest_belongs_to_newer_version_no_alarm_no_promote(self):
        """:latest already points to the next version, the release of that version follows minutes later."""
        self.full(latest_digest="sha256:" + "c" * 64)
        self.net.images[(IMG, "1.3.0")] = "sha256:" + "c" * 64
        self.clock[0] += 3 * 3600
        r = None
        for _ in range(3):
            r = self.go()
            self.clock[0] += rc.HEAL_WAIT_S + 60
        self.assertEqual(r["stage"], "green", r)
        self.assertTrue(any("being built" in h for h in r["hints"]))
        self.assertNotIn(("app", "promote"), self.ran, "never promote back to the older version")
        self.assertEqual(self.hooks, [])

    def test_new_tag_without_release_latest_is_only_a_hint(self):
        self.full()
        self.net.latest["o/app"] = {"tag_name": "v1.1.0"}
        self.net.tags["o/app"] = ["v1.2.0", "v1.3.0"]
        self.assertNotIn("latest", {x["kind"] for x in self.go(heal=False)["gaps"]})
        self.net.tags["o/app"] = ["v1.2.0"]
        self.assertIn("latest", {x["kind"] for x in self.go(heal=False)["gaps"]})

    def test_transient_state_heals_before_confirmation(self):
        self.full(without=["app-mobile-%s.apk"])
        self.assertEqual(self.go()["stage"], "waiting")
        self.net.releases["o/app"].clear()
        self.full()
        self.clock[0] += rc.CONFIRM_S + 30
        self.assertEqual(self.go()["stage"], "green")
        self.assertEqual((self.ran, self.hooks), ([], []))

    def test_dry_run_leaves_no_state(self):
        self.full(without=["app-mobile-%s.apk"])
        c = None
        for _ in range(4):
            c = self.checker(dry_run=True)
            c.check()
            self.clock[0] += rc.HEAL_WAIT_S + 60
        self.assertFalse(os.path.exists(os.path.join(c.state_dir, "state.json")))
        self.assertEqual((self.ran, self.hooks), ([], []))

    def test_image_without_signature(self):
        self.full()
        self.net.attachments.clear()
        r = self.item(self.checker(heal=False).check())
        self.assertTrue(any("no signature attached" in x["text"] for x in r["gaps"]))

    def test_version_file_old(self):
        self.full()
        self.net.contents[("o/addons", "app/config.yaml", "main")] = 'version: "1.1.0"\n'
        self.assertIn("version", {x["kind"] for x in self.go(heal=False)["gaps"]})

    def test_prerelease_and_foreign_version(self):
        self.full(pre=True)
        rel = self.net.releases["o/app"][0]
        self.net._id += 1
        self.net.asset_data[self.net._id] = b"x"
        rel["assets"].append({"id": self.net._id, "name": "app-mobile-1.1.0.apk", "size": 1, "state": "uploaded",
                              "digest": "sha256:" + hashlib.sha256(b"x").hexdigest()})
        kinds = {x["kind"] for x in self.go(heal=False)["gaps"]}
        self.assertIn("prerelease", kinds)
        self.assertIn("version", kinds)

    def test_empty_notes(self):
        self.full()
        self.net.releases["o/app"][0]["body"] = ""
        self.assertIn("notes", {x["kind"] for x in self.go(heal=False)["gaps"]})

    def test_tag_only_project_with_changelog(self):
        self.projects = {"svc": {"name": "Svc", "repo": "o/svc", "release": "tag", "notes": True, "version_files": [
            {"repo": "o/svc", "path": "VERSION", "ref": "v{v}", "pattern": "^{v}\\s*$"}]}}
        self.net.tags["o/svc"] = ["v0.9.0", "v0.10.0"]
        self.net.contents[("o/svc", "CHANGELOG.md", "v0.10.0")] = "# Changelog\n\n## [0.10.0] - 2026-11-21\n- x\n"
        self.net.contents[("o/svc", "VERSION", "v0.10.0")] = "0.10.0\n"
        r = self.go("svc")
        self.assertEqual((r["stage"], r["version"]), ("green", "0.10.0"))
        self.net.contents[("o/svc", "CHANGELOG.md", "v0.10.0")] = "# Changelog\n\n## [0.9.0]\n"
        self.clock[0] += 7200
        self.assertIn("notes", {x["kind"] for x in self.go("svc", heal=False)["gaps"]})

    def test_dot_in_version_is_not_a_wildcard(self):
        self.full()
        self.net.contents[("o/addons", "app/config.yaml", "main")] = 'version: "1x2x0"\n'
        self.assertIn("version", {x["kind"] for x in self.go(heal=False)["gaps"]})


class Control(Base):
    def test_stop_file_no_heal_no_hook(self):
        self.full(without=["app-mobile-%s.apk"])
        c = self.checker()
        open(os.path.join(c.state_dir, "STOP"), "w").close()
        c.check()
        self.clock[0] += rc.HEAL_WAIT_S * 3
        st = self.checker().check()
        self.assertEqual(self.item(st)["stage"], "red")
        self.assertTrue(st["stopped"])
        self.assertEqual((self.ran, self.hooks), ([], []))

    def test_report_only_hooks_without_heal(self):
        self.projects["app"]["report_only"] = True
        self.full(without=["app-mobile-%s.apk"])
        r = self.confirmed()
        self.assertEqual(r["stage"], "red")
        self.assertEqual(self.ran, [])
        self.assertEqual(len(self.hooks), 1)

    def test_assigned_ticket_suppresses_hook(self):
        self.full(without=["app-mobile-%s.apk"])
        c = self.checker()
        c.state["app"] = {"version": "1.2.0", "ticket": "T-423", "seen_ts": T0 - 86400}
        c.check()
        self.clock[0] += rc.HEAL_WAIT_S + 60
        r = self.go()
        self.assertEqual(r["ticket"], "T-423")
        self.assertEqual(self.hooks, [])

    def test_no_hook_configured_still_red(self):
        self.on_red = None
        self.full(without=["app-mobile-%s.apk"])
        r = self.until_red()
        self.assertEqual(r["stage"], "red")
        self.assertEqual(self.hooks, [])

    def test_new_version_resets_state(self):
        self.full(without=["app-mobile-%s.apk"])
        self.until_red()
        self.assertEqual(len(self.hooks), 1)
        self.full("1.3.0", without=["app-mobile-%s.apk"])
        self.clock[0] += 3 * 3600
        r = self.until_red()
        self.assertEqual((r["version"], r["stage"]), ("1.3.0", "red"))
        self.assertEqual(len(self.hooks), 2)

    def test_network_error_is_unknown_never_red(self):
        def broken(path):
            raise rc.NetError("network down")
        self.net.gh_json = broken
        st = self.checker().check()
        self.assertEqual(self.item(st)["stage"], "unknown")
        self.assertEqual(st["overall"], "waiting")
        self.assertEqual(self.hooks, [])

    def test_status_file_is_world_readable_state_is_private(self):
        self.full()
        c = self.checker()
        c.check()
        self.assertEqual(os.stat(c.status_path).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(os.path.join(c.state_dir, "state.json")).st_mode & 0o777, 0o600)

    def test_excluded_listed_in_status(self):
        self.full()
        self.assertEqual(self.checker().check()["excluded"], ["legacy"])

    def test_no_declaration_is_bad_usage(self):
        p = os.path.join(self.tmp, "none.json")
        self.assertEqual(rc.main(["check", "--config", p, "--state-dir", self.state_dir]), 2)
        with open(p, "w") as f:
            json.dump({"format": "wrong/1", "projects": {}}, f)
        self.assertEqual(rc.main(["check", "--config", p, "--state-dir", self.state_dir]), 2)
        self.assertEqual(rc.main(["bogus"]), 2)

    def test_hook_command_really_runs_with_report_path(self):
        marker = os.path.join(self.tmp, "hook-ran.txt")
        script = os.path.join(self.tmp, "hook.py")
        with open(script, "w") as f:
            f.write("import sys\nopen(%r, 'w').write(sys.argv[1])\n" % marker)
        self.on_red = [sys.executable, script]
        self.full(without=["app-mobile-%s.apk"])
        c = self.checker()
        c._hook = None
        self.projects["app"]["report_only"] = True
        c.cfg["projects"] = self.projects
        c.check()
        self.clock[0] += rc.CONFIRM_S + 30
        c.check()
        self.assertTrue(os.path.exists(marker))
        with open(marker) as f:
            self.assertTrue(rc.read_json(f.read())["gaps"])


class Minisign(unittest.TestCase):
    def test_real_valid_and_tampered(self):
        k, keyid, pub = make_key()
        d = b"package" * 1000
        sig = rc.minisign_sig(sign(k, keyid, d))
        p = rc.minisign_pubkey(pub)
        self.assertTrue(rc.minisign_verify(p, sig, hashlib.blake2b(d, digest_size=64).digest()))
        self.assertFalse(rc.minisign_verify(p, sig, hashlib.blake2b(d + b"x", digest_size=64).digest()))
        _, _, pub2 = make_key()
        self.assertFalse(rc.minisign_verify(rc.minisign_pubkey(pub2), sig, hashlib.blake2b(d, digest_size=64).digest()))


class Triggers(unittest.TestCase):
    def test_drop_and_take(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertEqual(rc.main(["trigger", "app", "1.2.0", "--state-dir", tmp]), 0)
            self.assertEqual(rc.take_triggers(tmp), {"app"})
            self.assertEqual(rc.take_triggers(tmp), set())
            self.assertEqual(rc.main(["trigger", "../x", "--state-dir", tmp]), 2)
        finally:
            shutil.rmtree(tmp)

    def test_assign_records_ticket(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = os.path.join(tmp, "d.json")
            with open(cfg, "w") as f:
                json.dump({"format": "deliverables/1", "projects": {"app": {"repo": "o/app"}}}, f)
            args = ["--config", cfg, "--state-dir", tmp]
            self.assertEqual(rc.main(["assign", "app", "1.2.0", "T-1", "--reason", "fix running"] + args), 0)
            self.assertEqual(rc.read_json(os.path.join(tmp, "state.json"))["app"]["ticket"], "T-1")
            self.assertEqual(rc.main(["assign", "nope", "1.2.0", "T-1", "--reason", "x"] + args), 2)
        finally:
            shutil.rmtree(tmp)


class RegistryParsing(unittest.TestCase):
    def test_split_image(self):
        s = rc.Net._split_image
        self.assertEqual(s("ghcr.io/o/app"), ("ghcr.io", "o/app"))
        self.assertEqual(s("registry.example:5000/a/b"), ("registry.example:5000", "a/b"))
        self.assertEqual(s("docker.io/o/app"), ("registry-1.docker.io", "o/app"))
        self.assertEqual(s("o/app"), ("registry-1.docker.io", "o/app"))
        self.assertEqual(s("nginx"), ("registry-1.docker.io", "library/nginx"))


class ExampleConfig(unittest.TestCase):
    PATH = os.path.join(HERE, "deliverables.example.json")

    def test_valid(self):
        cfg, err = rc.load_config(self.PATH)
        self.assertIsNone(err)
        self.assertGreaterEqual(len(cfg["projects"]), 3)
        for name, p in cfg["projects"].items():
            self.assertIn(p.get("release", "github"), ("github", "tag"), name)
            for h in p.get("heal") or []:
                self.assertIn(h["kind"], rc.HEAL_KINDS, name)

    def test_no_addresses_or_names_in_example(self):
        with open(self.PATH, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotRegex(raw, r"https?://")
        self.assertNotRegex(raw, r"\b\d+\.\d+\.\d+\.\d+\b")
        for word in ("robeertm", "tauri", "linkwache", "lanloom"):
            self.assertNotIn(word, raw.lower())


if __name__ == "__main__":
    unittest.main()
