#!/usr/bin/env python3
"""Release check: does the PUBLISHED release contain everything the project advertises?

A release can be tagged and "green" in CI and still miss a file the README or the download page promises (an installer, a
mobile package, a signature, a container tag). This tool declares per project what is offered (`deliverables.json`) and,
after each release, checks the published result:

  * every declared asset is present (state uploaded, size > 0), `.sig` partners exist
  * checksum file entries equal the asset digest GitHub computed; no asset carries a foreign version in its name
  * updater manifest (`latest.json`): version, every platform URL points to an asset of THIS release, signature == .sig
  * minisign signatures are verified cryptographically (Ed25519 via the optional `cryptography` package, BLAKE2b prehash)
  * container image tags: `:<version>` exists, `:latest` == `:<version>` (unless a newer version is already being built),
    signature / SBOM attached (OCI referrers tag scheme `sha256-<hex>`)
  * version files at a ref, release notes not empty, the "latest release" marker, a download page probe

    release_check.py check [--project P ...] [--dry-run] [--no-heal]    # timer (15 min) or after a release
    release_check.py status                                             # one block of text per project
    release_check.py history <project> [--count N]                      # older releases, check only (never heals)
    release_check.py assign <project> <version> <ticket> --reason TEXT  # register a running fix; no hook for it
    release_check.py trigger <project> [<version>]                      # drop file for a path unit
    release_check.py run-triggers                                       # consume trigger files (path unit service)

Common options: --config FILE (default: $RELEASE_CHECK_CONFIG or deliverables.json next to this script), --state-dir DIR
(default: $RELEASE_CHECK_STATE or ~/.local/state/release-check), --status FILE (default: <state-dir>/status.json).

Flow per project (newest release): a gap inside the grace period (`grace_min`), or while a CI run of the tag is running
(cap 6 h), is WAITING. Every gap must persist CONFIRM_S before anything happens (transient states heal themselves). Then
the heal ways run, each once per version: a command of the repo, re-run failed CI runs of the tag, the promote workflow
(never when a newer version is in flight). Still open after HEAL_WAIT_S, or no way exists: RED. On red the optional
`on_red` command from the config runs once per project+version and receives the path of a JSON report as its last argument.

Output: JSON (format `release-check/1`) in the status file (0644) and, as the LAST stdout line,
    RELEASE-CHECK: green|waiting|gaps (...)
Exit 0 = green or waiting, 1 = gaps (red or healing in progress), 2 = bad usage. A STOP file in the state directory turns
heal and hook off (check and status stay on). Never tags, never creates or deletes a release.
Standard library only (+ optional `cryptography`). `gh` runs as the current user with its own login; tokens never appear
in a command line.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
FORMAT = "release-check/1"
CONFIG_FORMAT = "deliverables/1"
HERE = os.path.dirname(os.path.abspath(__file__))
GH = os.environ.get("RELEASE_CHECK_GH", "gh")
STAGES = ("green", "waiting", "gap", "red", "unknown")
CI_CAP_S = 6 * 3600             # while a CI run of the tag runs: WAITING for at most this long
HEAL_WAIT_S = 45 * 60           # after a heal attempt wait this long, then red
CONFIRM_S = 20 * 60             # a gap must persist this long before healing or red (transient states heal themselves)
DEFAULT_GRACE_MIN = 90
MAX_DOWNLOAD = 900 * 1024 * 1024  # per run, at most this many bytes loaded for signature verification
SMALL_MAX = 64 * 1024           # .sig / checksum file / manifest are small
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SEMVER_IN_NAME = re.compile(r"(?<![0-9.])v?(\d+\.\d+\.\d+)(?![0-9])")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
RUN_OPEN = ("queued", "in_progress", "waiting", "requested", "pending")
HEAL_KINDS = ("command", "rerun_ci", "promote")
SIGSTORE_SIGN = "sigstore.dev/cosign/sign"
SPDX = "spdx.dev/Document"


class NetError(Exception):
    """Network / GitHub unreachable -> stage `unknown`, never red."""


def vt(v):
    m = VERSION_RE.match(str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def vs(t):
    return ".".join(map(str, t))


def clean(s, n=200):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s if s is not None else ""))[:n].strip()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def fmt(pattern, v):
    return str(pattern).replace("{v}", v)


def iso_epoch(s):
    if not s:
        return None
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return None


# ---------------------------------------------------------------- minisign (updater signatures)
def _b64(s):
    return base64.b64decode(s.strip() + "=" * (-len(s.strip()) % 4))


def minisign_pubkey(text_or_b64):
    """Public key (base64 of the minisign text file, or the text file itself) -> (key id, 32-byte key)."""
    t = text_or_b64.strip()
    if "untrusted comment" not in t:
        t = _b64(t).decode("utf-8", "replace")
    lines = [z for z in t.splitlines() if z.strip() and not z.startswith("untrusted comment")]
    raw = _b64(lines[0])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ValueError("not a minisign key")
    return raw[2:10], raw[10:]


def minisign_sig(text_or_b64):
    """.sig (base64 of the minisign signature file, or the file itself) -> dict(alg, keyid, sig, trusted, global)."""
    t = text_or_b64.strip()
    if "untrusted comment" not in t:
        t = _b64(t).decode("utf-8", "replace")
    z = [x for x in t.splitlines() if x.strip()]
    if len(z) < 4 or not z[2].startswith("trusted comment: "):
        raise ValueError("signature file incomplete")
    raw = _b64(z[1])
    if len(raw) != 74 or raw[:2] not in (b"Ed", b"ED"):
        raise ValueError("unknown signature format")
    return {"alg": raw[:2], "keyid": raw[2:10], "sig": raw[10:], "trusted": z[2][len("trusted comment: "):].encode(),
            "global": _b64(z[3])}


def minisign_verify(pub, sig, blake2b_digest, raw_message=None):
    """True = valid. `pub` = (key id, key), `sig` = minisign_sig(). ED = prehashed (BLAKE2b-512). Raises ImportError without
    `cryptography` (the caller then reports 'not verifiable', never red)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    keyid, pk = pub
    if sig["keyid"] != keyid:
        return False
    k = Ed25519PublicKey.from_public_bytes(pk)
    msg = blake2b_digest if sig["alg"] == b"ED" else raw_message
    if msg is None:
        return False
    try:
        k.verify(sig["sig"], msg)
        k.verify(sig["global"], sig["sig"] + sig["trusted"])
        return True
    except InvalidSignature:
        return False


# ---------------------------------------------------------------- network (replaceable in tests)
class Net:
    """GitHub through `gh` (current user, own login, no token in argv); images through the OCI distribution API."""

    ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])

    def __init__(self):
        self._tokens = {}

    def _gh(self, args, timeout=120):
        env = dict(os.environ, GH_PROMPT_DISABLED="1", NO_COLOR="1")
        try:
            return subprocess.run([GH] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            raise NetError("gh: %s" % type(e).__name__)

    @staticmethod
    def _not_found(err):
        return "HTTP 404" in err or "Not Found" in err

    def gh_json(self, path):
        """GET -> JSON, or None on 404. Anything else -> NetError."""
        r = self._gh(["api", path])
        if r.returncode:
            err = r.stderr.decode("utf-8", "replace")
            if self._not_found(err):
                return None
            raise NetError("GitHub %s: %s" % (path.split("?")[0], clean(err, 120)))
        try:
            return json.loads(r.stdout.decode() or "null")
        except ValueError:
            raise NetError("GitHub %s: no JSON answer" % path.split("?")[0])

    def gh_raw(self, path, accept, limit=SMALL_MAX):
        r = self._gh(["api", "-H", "Accept: " + accept, path])
        if r.returncode:
            err = r.stderr.decode("utf-8", "replace")
            if self._not_found(err):
                return None
            raise NetError("GitHub %s: %s" % (path.split("?")[0], clean(err, 120)))
        return r.stdout[:limit]

    def file(self, repo, path, ref):
        b = self.gh_raw("repos/%s/contents/%s?ref=%s" % (repo, urllib.parse.quote(path), urllib.parse.quote(ref, safe="")),
                        "application/vnd.github.raw", limit=4 * 1024 * 1024)
        return None if b is None else b.decode("utf-8", "replace")

    def asset_text(self, repo, asset_id):
        b = self.gh_raw("repos/%s/releases/assets/%s" % (repo, asset_id), "application/octet-stream")
        return None if b is None else b.decode("utf-8", "replace")

    def asset_hash(self, repo, asset_id):
        """Streams a file -> (sha256 hex, blake2b-512 bytes, size). Nothing stays on disk."""
        env = dict(os.environ, GH_PROMPT_DISABLED="1")
        s, b, n = hashlib.sha256(), hashlib.blake2b(digest_size=64), 0
        try:
            p = subprocess.Popen([GH, "api", "-H", "Accept: application/octet-stream",
                                  "repos/%s/releases/assets/%s" % (repo, asset_id)],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
            while True:
                chunk = p.stdout.read(1 << 20)
                if not chunk:
                    break
                s.update(chunk)
                b.update(chunk)
                n += len(chunk)
            if p.wait(timeout=1800):
                raise NetError("download failed")
        except (OSError, subprocess.SubprocessError) as e:
            raise NetError("download: %s" % type(e).__name__)
        return s.hexdigest(), b.digest(), n

    def gh_action(self, args, timeout=300):
        r = self._gh(args, timeout=timeout)
        return r.returncode == 0, clean(r.stderr.decode("utf-8", "replace"), 200)

    # -- images: generic OCI distribution API, anonymous bearer-token flow from WWW-Authenticate
    @staticmethod
    def _split_image(name):
        parts = name.split("/", 1)
        if len(parts) == 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
            host, path = parts
        else:
            host, path = "docker.io", (name if "/" in name else "library/" + name)
        return ("registry-1.docker.io" if host == "docker.io" else host), path

    def _token(self, host, path, challenge):
        key = (host, path)
        if key in self._tokens:
            return self._tokens[key]
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
        realm = params.get("realm")
        if not realm or not realm.startswith("https://"):
            raise NetError("registry %s: no usable token endpoint" % host)
        q = {"scope": params.get("scope") or "repository:%s:pull" % path}
        if params.get("service"):
            q["service"] = params["service"]
        try:
            with urllib.request.urlopen(realm + "?" + urllib.parse.urlencode(q), timeout=20) as r:
                d = json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NetError("registry login: %s" % type(e).__name__)
        self._tokens[key] = d.get("token") or d.get("access_token") or ""
        return self._tokens[key]

    def _registry(self, name, suffix, method="GET", accept=None, limit=1 << 20):
        host, path = self._split_image(name)
        url = "https://%s/v2/%s/%s" % (host, path, suffix)
        for attempt in (0, 1):
            headers = {"User-Agent": "release-check/1"}
            if accept:
                headers["Accept"] = accept
            if (host, path) in self._tokens and self._tokens[(host, path)]:
                headers["Authorization"] = "Bearer " + self._tokens[(host, path)]
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=30) as r:
                    return r.headers, (r.read(limit) if method == "GET" else b"")
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0 and (host, path) not in self._tokens:
                    self._token(host, path, e.headers.get("WWW-Authenticate"))
                    continue
                if e.code == 404:
                    return None, b""
                raise NetError("registry %s: HTTP %s" % (suffix, e.code))
            except (urllib.error.URLError, OSError) as e:
                raise NetError("registry: %s" % type(e).__name__)
        raise NetError("registry: login refused")

    def image_digest(self, name, tag):
        h, _ = self._registry(name, "manifests/" + tag, method="HEAD", accept=self.ACCEPT)
        return None if h is None else h.get("Docker-Content-Digest")

    def image_tags(self, name):
        """All tags of the image (for 'is a newer version already being built?')."""
        h, body = self._registry(name, "tags/list?n=10000", limit=4 << 20)
        try:
            return list(json.loads(body.decode()).get("tags") or []) if h is not None else []
        except ValueError:
            return []

    def image_attachments(self, name, digest):
        """Signature bundle at the digest (OCI referrers tag scheme sha256-<hex>) -> list of predicate types."""
        h, raw = self._registry(name, "manifests/" + digest.replace(":", "-"), accept=self.ACCEPT)
        if not raw:
            return []
        try:
            d = json.loads(raw.decode())
        except ValueError:
            return []
        out = []
        for m in d.get("manifests") or []:
            a = (m or {}).get("annotations") or {}
            out.append(str(a.get("dev.sigstore.bundle.predicateType") or (m or {}).get("artifactType") or ""))
        return out

    def page(self, url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "release-check/1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read(4 * 1024 * 1024).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NetError("page: %s" % type(e).__name__)


# ---------------------------------------------------------------- configuration
def load_config(path):
    """-> (config dict, error text or None). Never raises."""
    d = read_json(path)
    if not isinstance(d, dict) or d.get("format") != CONFIG_FORMAT:
        return {}, "no valid %s file: %s" % (CONFIG_FORMAT, path)
    for name, p in (d.get("projects") or {}).items():
        if not NAME_RE.match(str(name)):
            return {}, "bad project name: %s" % clean(name, 40)
        if not isinstance(p, dict) or not REPO_RE.match(str(p.get("repo") or "")):
            return {}, "project %s: repo must be owner/name" % name
        for h in p.get("heal") or []:
            if h.get("kind") not in HEAL_KINDS:
                return {}, "project %s: unknown heal kind %s" % (name, clean(h.get("kind"), 30))
    on_red = d.get("on_red")
    if on_red is not None and not (isinstance(on_red, list) and on_red and all(isinstance(x, str) for x in on_red)):
        return {}, "on_red must be a non-empty list of strings"
    return d, None


class Checker:
    def __init__(self, config, state_dir, status_path=None, net=None, now=None, dry_run=False, heal=True, out=None,
                 runner=None, hook=None):
        self.cfg = config
        self.state_dir = state_dir
        self.status_path = status_path or os.path.join(state_dir, "status.json")
        self.out = out or (lambda t: print(t, flush=True))
        self.net = net or Net()
        self._now = now
        self.dry_run = dry_run
        self.heal_on = heal
        self._runner = runner       # tests: (project, heal entry, result) -> (ok, info)
        self._hook = hook           # tests: (command list, report path) -> bool
        self.downloaded = 0
        self.state = read_json(os.path.join(state_dir, "state.json"), {}) or {}

    def now(self):
        return self._now() if callable(self._now) else (self._now or time.time())

    def stopped(self):
        return os.path.exists(os.path.join(self.state_dir, "STOP"))

    def projects(self):
        return {n: p for n, p in (self.cfg.get("projects") or {}).items()}

    # -- newest version
    def newest(self, decl):
        repo = decl["repo"]
        if decl.get("release", "github") == "tag":
            refs = self.net.gh_json("repos/%s/git/matching-refs/tags/v" % repo) or []
            tags = [r["ref"].rsplit("/", 1)[-1] for r in refs if isinstance(r, dict) and r.get("ref")]
            tags = [t for t in tags if vt(t)]
            return (vs(vt(max(tags, key=vt))), None) if tags else (None, None)
        rel = self.net.gh_json("repos/%s/releases?per_page=15" % repo) or []
        cand = [r for r in rel if isinstance(r, dict) and not r.get("draft") and vt(r.get("tag_name"))]
        if not cand:
            return None, None
        r = max(cand, key=lambda x: vt(x["tag_name"]))
        return vs(vt(r["tag_name"])), r

    def release_for(self, decl, v):
        r = self.net.gh_json("repos/%s/releases/tags/v%s" % (decl["repo"], v))
        return r if r is not None else self.net.gh_json("repos/%s/releases/tags/%s" % (decl["repo"], v))

    # -- one check
    def inspect(self, name, decl, version=None, newest=True, release=None):
        """-> dict(version, gaps[], checked, ci_running, failed_runs[], published_ts, hints[], newer_in_flight). Raises NetError."""
        v, rel = (version, release) if version else self.newest(decl)
        res = {"project": name, "name": decl.get("name") or name, "repo": decl["repo"], "version": v, "gaps": [],
               "checked": 0, "hints": [], "ci_running": False, "failed_runs": [], "published_ts": None,
               "newer_in_flight": None}
        if not v:
            res["hints"].append("no release or tag yet")
            return res
        gaps = res["gaps"]

        def gap(kind, what, text):
            gaps.append({"kind": kind, "what": clean(what, 120), "text": clean(text, 240)})

        def ok():
            res["checked"] += 1

        tag = "v" + v
        repo = decl["repo"]
        mode = decl.get("release", "github")
        if newest and mode == "github":
            # tag exists, release does not yet: the next release is being built - "latest" / :latest may differ then
            refs = self.net.gh_json("repos/%s/git/matching-refs/tags/v" % repo) or []
            higher = [vt(r["ref"].rsplit("/", 1)[-1]) for r in refs if isinstance(r, dict) and r.get("ref")]
            higher = [x for x in higher if x and x > vt(v)]
            if higher:
                res["newer_in_flight"] = vs(max(higher))
        if mode == "github":
            if rel is None or str(rel.get("tag_name") or "").lstrip("v") != v:
                rel = self.release_for(decl, v)
            if rel is None:
                gap("release", "release", "No GitHub release for %s" % tag)
                return res
            res["published_ts"] = iso_epoch(rel.get("published_at") or rel.get("created_at"))
            if rel.get("prerelease"):
                gap("prerelease", "prerelease", "Release %s is still a pre-release (promote missing)" % tag)
            else:
                ok()
            if decl.get("notes") and len(str(rel.get("body") or "").strip()) < 20:
                gap("notes", "notes", "Release %s has no release notes" % tag)
            elif decl.get("notes"):
                ok()
            if decl.get("latest") and newest:
                lat = self.net.gh_json("repos/%s/releases/latest" % repo)
                if res["newer_in_flight"]:
                    res["hints"].append("v%s is being built - 'latest' is checked there" % res["newer_in_flight"])
                elif not lat or str(lat.get("tag_name") or "") != str(rel.get("tag_name")):
                    gap("latest", "release-latest", "'Latest release' shows %s instead of %s" % (
                        (lat or {}).get("tag_name") or "nothing", rel.get("tag_name")))
                else:
                    ok()
            assets = {a["name"]: a for a in rel.get("assets") or [] if isinstance(a, dict) and a.get("name")}
            self._assets(decl, v, assets, gap, ok)
            self._foreign_version(v, assets, gap)
            self._checksums(decl, v, repo, assets, gap, ok)
            self._updater(decl, v, repo, assets, gap, ok)
            self._minisign(name, decl, v, repo, assets, gap, ok, res)
        else:
            commit = self.net.gh_json("repos/%s/commits/%s" % (repo, tag))
            if not commit:
                gap("release", "tag", "Tag %s is missing" % tag)
                return res
            res["published_ts"] = iso_epoch(((commit.get("commit") or {}).get("committer") or {}).get("date"))
            ok()
            if decl.get("notes"):
                cl = self.net.file(repo, decl.get("changelog") or "CHANGELOG.md", tag) or ""
                if re.search(r"^##\s*\[?v?%s\]?(\s|$)" % re.escape(v), cl, re.M):
                    ok()
                else:
                    gap("notes", "notes", "CHANGELOG at tag %s has no section %s" % (tag, v))
        self._images(decl, v, newest, gap, ok, res)
        self._version_files(decl, v, gap, ok, newest)
        if newest:
            self._pages(decl, v, gap, ok, res)
            self._runs(repo, tag, res)
        return res

    def _assets(self, decl, v, assets, gap, ok):
        for d in decl.get("assets") or []:
            fname = fmt(d.get("pattern"), v)
            a = assets.get(fname)
            title = d.get("title") or fname
            if not a or a.get("state", "uploaded") != "uploaded" or not a.get("size"):
                gap("asset", fname, "%s missing: %s" % (title, fname))
                continue
            ok()
            if d.get("signature"):
                s = assets.get(fname + d["signature"])
                if not s or not s.get("size"):
                    gap("signature", fname + d["signature"], "Signature missing: %s%s" % (fname, d["signature"]))
                else:
                    ok()

    def _foreign_version(self, v, assets, gap):
        for fname in assets:
            for other in SEMVER_IN_NAME.findall(fname):
                if other != v:
                    gap("version", fname, "File with a foreign version in the release: %s" % fname)
                    break

    def _checksums(self, decl, v, repo, assets, gap, ok):
        cs = decl.get("checksums")
        if not isinstance(cs, dict):
            return
        fname = fmt(cs.get("file"), v)
        a = assets.get(fname)
        if not a:
            return          # a missing file is reported by _assets
        text = self.net.asset_text(repo, a["id"]) or ""
        listed = {}
        for z in text.splitlines():
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", z)
            if m:
                listed[m.group(2)] = m.group(1).lower()
        if not listed:
            gap("checksum", fname, "%s is empty or unreadable" % fname)
            return
        for aname, hx in listed.items():
            b = assets.get(aname)
            if not b:
                gap("checksum", aname, "%s lists %s, the file is missing in the release" % (fname, aname))
                continue
            dg = str(b.get("digest") or "")
            if dg.startswith("sha256:"):
                if dg[7:].lower() != hx:
                    gap("checksum", aname, "Checksum mismatch: %s" % aname)
                else:
                    ok()
        if cs.get("all"):
            for aname in assets:
                if aname != fname and aname not in listed:
                    gap("checksum", aname, "%s is missing in %s" % (aname, fname))

    def _updater(self, decl, v, repo, assets, gap, ok):
        u = decl.get("updater_manifest")
        if not isinstance(u, dict):
            return
        a = assets.get(fmt(u.get("file"), v))
        if not a:
            return
        try:
            d = json.loads(self.net.asset_text(repo, a["id"]) or "")
        except ValueError:
            gap("updater", a["name"], "%s is not valid JSON" % a["name"])
            return
        if str(d.get("version") or "").lstrip("v") != v:
            gap("updater", a["name"], "%s names version %s instead of %s" % (a["name"], d.get("version"), v))
        else:
            ok()
        sigs = {}
        for plat, x in sorted((d.get("platforms") or {}).items()):
            url = str((x or {}).get("url") or "")
            fname = url.rsplit("/", 1)[-1]
            if "/v%s/" % v not in url or fname not in assets:
                gap("updater", plat, "%s: %s points to a file that is not in release %s" % (a["name"], plat, v))
                continue
            s = assets.get(fname + ".sig")
            if not s:
                gap("updater", plat, "%s: %s has no .sig" % (a["name"], fname))
                continue
            if fname not in sigs:
                sigs[fname] = (self.net.asset_text(repo, s["id"]) or "").strip()
            if str((x or {}).get("signature") or "").strip() != sigs[fname]:
                gap("updater", plat, "%s: signature for %s differs from %s.sig" % (a["name"], plat, fname))
            else:
                ok()

    def _minisign(self, name, decl, v, repo, assets, gap, ok, res):
        ms = decl.get("minisign")
        if not isinstance(ms, dict):
            return
        try:
            raw = self.net.file(repo, ms.get("repo_file"), "v" + v) or ""
            value = json.loads(raw)
            for part in str(ms.get("json_path") or "").split("."):
                value = value[part]
            pub = minisign_pubkey(str(value))
        except (ValueError, KeyError, TypeError):
            res["hints"].append("signing key not readable at the tag - signatures checked for presence only")
            return
        z = self.state.setdefault(name, {})
        confirmed = z.setdefault("minisign_ok", {})
        for d in decl.get("assets") or []:
            if d.get("signature") != ".sig":
                continue
            fname = fmt(d.get("pattern"), v)
            a, s = assets.get(fname), assets.get(fname + ".sig")
            if not a or not s:
                continue
            dg = str(a.get("digest") or a.get("updated_at") or a.get("id"))
            if confirmed.get(fname) == dg:
                ok()
                continue
            if self.downloaded + int(a.get("size") or 0) > MAX_DOWNLOAD:
                res["hints"].append("signature check of %s in the next run (download limit)" % fname)
                continue
            try:
                sig = minisign_sig(self.net.asset_text(repo, s["id"]) or "")
                sha, b2, n = self.net.asset_hash(repo, a["id"])
                self.downloaded += n
                good = minisign_verify(pub, sig, b2)
            except ImportError:
                res["hints"].append("cryptography is not installed - signatures checked for presence only")
                return
            except ValueError as e:
                gap("signature", fname + ".sig", "Signature unreadable: %s.sig (%s)" % (fname, e))
                continue
            if str(a.get("digest") or "").startswith("sha256:") and a["digest"][7:] != sha:
                gap("checksum", fname, "Downloaded file does not match the GitHub digest: %s" % fname)
                continue
            if good:
                confirmed[fname] = dg
                ok()
            else:
                gap("signature", fname + ".sig", "Signature invalid (minisign): %s" % fname)

    def _images(self, decl, v, newest, gap, ok, res):
        for ab in decl.get("images") or []:
            iname = ab.get("name")
            tags = [fmt(t, v) for t in ab.get("tags") or ["{v}"]]
            dig = {}
            for t in tags:
                if t == "latest" and not newest:
                    continue
                dig[t] = self.net.image_digest(iname, t)
                if not dig[t]:
                    gap("image", "%s:%s" % (iname, t), "Image %s:%s is missing" % (iname, t))
                else:
                    ok()
            if ab.get("latest_equal") and newest and dig.get(v) and dig.get("latest"):
                if dig[v] == dig["latest"]:
                    ok()
                else:
                    newer = res.get("newer_in_flight")
                    if not newer:
                        higher = [vt(t) for t in self.net.image_tags(iname) if vt(t) and vt(t) > vt(v)]
                        newer = vs(max(higher)) if higher else None
                        res["newer_in_flight"] = newer
                    if newer:   # never promote back: :latest already belongs to the newer version
                        res["hints"].append("%s:latest already belongs to %s (being built) - checked with the next release"
                                            % (iname, newer))
                    else:
                        gap("latest", "%s:latest" % iname, "%s:latest is not %s" % (iname, v))
            if (ab.get("signed") or ab.get("sbom")) and dig.get(v):
                att = self.net.image_attachments(iname, dig[v])
                if ab.get("signed"):
                    if any(SIGSTORE_SIGN in x for x in att):
                        ok()
                    else:
                        gap("signature", "%s:%s" % (iname, v), "Image %s:%s has no signature attached" % (iname, v))
                if ab.get("sbom"):
                    if any(SPDX in x for x in att):
                        ok()
                    else:
                        gap("image", "%s:%s-sbom" % (iname, v), "Image %s:%s has no signed SBOM" % (iname, v))

    def _version_files(self, decl, v, gap, ok, newest=True):
        for vf in decl.get("version_files") or []:
            if not newest and "{v}" not in str(vf.get("ref") or "v{v}"):
                continue        # state of a branch applies to the newest release only
            ref = fmt(vf.get("ref") or "v{v}", v)
            text = self.net.file(vf.get("repo") or decl["repo"], vf.get("path"), ref)
            pattern = str(vf.get("pattern") or "").replace("{v}", re.escape(v))
            title = vf.get("title") or vf.get("path")
            if text is None:
                gap("version", vf.get("path"), "%s is missing (%s@%s)" % (title, vf.get("path"), ref))
            elif not re.search(pattern, text, re.M):
                gap("version", vf.get("path"), "%s does not name version %s" % (title, v))
            else:
                ok()

    def _pages(self, decl, v, gap, ok, res):
        probes = read_json(os.path.join(self.state_dir, "probes.json"), {}) or {}
        for s in decl.get("pages") or []:
            url = probes.get(s.get("probe"))
            if not url or not str(url).startswith("https://"):
                res["hints"].append("%s: no probe address configured" % (s.get("title") or s.get("probe")))
                continue
            text = self.net.page(url)
            missing = [fmt(m, v) for m in s.get("patterns") or [] if fmt(m, v) not in text]
            if missing:
                for m in missing:
                    gap("page", "%s:%s" % (s.get("probe"), m), "%s does not offer %s" % (s.get("title") or "Page", m))
            else:
                ok()

    def _runs(self, repo, tag, res):
        try:
            d = self.net.gh_json("repos/%s/actions/runs?branch=%s&per_page=30" % (repo, urllib.parse.quote(tag))) or {}
        except NetError:
            return
        for r in d.get("workflow_runs") or []:
            if not isinstance(r, dict) or r.get("head_branch") != tag:
                continue
            if r.get("status") in RUN_OPEN:
                res["ci_running"] = True
            elif r.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure"):
                res["failed_runs"].append({"id": r.get("id"), "name": clean(r.get("name"), 60)})

    # -- heal
    def heal_ways(self, decl, res):
        """Applicable ways per gap set: (key, description, entry)."""
        ways = []
        kinds = {x["kind"] for x in res["gaps"]}
        names = {x["what"] for x in res["gaps"]}
        v = res["version"]
        for h in decl.get("heal") or []:
            kind = h.get("kind")
            if kind == "command":
                matching = {fmt(x, v) for x in h.get("for") or []}
                if matching & names and isinstance(h.get("command"), list):
                    ways.append(("command:" + "|".join(sorted(matching)), h.get("title") or "Repository command", h))
            elif kind == "rerun_ci":
                if kinds & {"asset", "signature", "checksum", "updater", "image"}:
                    for r in res["failed_runs"]:
                        ways.append(("ci:%s" % r["id"], "re-run failed CI run '%s'" % r["name"], dict(h, run=r["id"])))
            elif kind == "promote":
                wf = h.get("workflow") or "promote.yml"
                if kinds & {"latest", "prerelease"} and not res["ci_running"] and not res.get("newer_in_flight"):
                    ways.append(("promote:%s" % v, "promote (%s) for %s" % (wf, v), dict(h, workflow=wf)))
        return ways

    def clone(self, decl):
        """Own clone of the default branch under the state directory - never a shared working copy."""
        target = os.path.join(self.state_dir, "clones", decl["repo"].split("/")[1])
        try:
            if not os.path.isdir(os.path.join(target, ".git")):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if subprocess.run([GH, "repo", "clone", decl["repo"], target, "--", "-q"], capture_output=True,
                                  timeout=900).returncode:
                    return None
            for args in (["fetch", "-q", "origin"], ["checkout", "-q", "-f", "--detach", "origin/HEAD"]):
                if subprocess.run(["git", "-C", target] + args, capture_output=True, timeout=600).returncode:
                    return None
        except (OSError, subprocess.SubprocessError):
            return None
        return target

    def heal(self, name, decl, res, z):
        """Runs every way not yet tried, once. -> list of attempts of this run."""
        attempts = []
        tried = z.setdefault("heal", {})
        for key, text, h in self.heal_ways(decl, res):
            if key in tried:
                continue
            if self.dry_run:
                attempts.append({"way": text, "ok": None, "text": "[dry run]"})
                continue
            good, info = False, ""
            if self._runner:
                good, info = self._runner(name, h, res)
            elif h["kind"] == "rerun_ci":
                good, info = self.net.gh_action(["run", "rerun", str(h["run"]), "--failed", "-R", decl["repo"]])
            elif h["kind"] == "promote":
                good, info = self.net.gh_action(["workflow", "run", h["workflow"], "-R", decl["repo"], "-f",
                                                 "version=%s" % res["version"]])
            elif h["kind"] == "command":
                folder = self.clone(decl) if h.get("clone", True) else (h.get("cwd") or ".")
                need = h.get("requires")
                if not folder:
                    info = "own clone not available"
                elif need and not os.path.exists(os.path.join(folder, need)):
                    info = "%s is missing on the default branch" % need
                else:
                    try:
                        r = subprocess.run([fmt(x, res["version"]) for x in h["command"]], cwd=folder, capture_output=True,
                                           timeout=3600)
                        good, info = r.returncode == 0, "exit %s" % r.returncode
                    except (OSError, subprocess.SubprocessError) as e:
                        info = type(e).__name__
            tried[key] = {"ts": self.now(), "ok": bool(good), "text": clean(info, 160)}
            attempts.append({"way": text, "ok": bool(good), "text": clean(info, 160)})
            self.out("  heal %s: %s (%s)" % (name, text, "ok" if good else "failed: " + clean(info, 80)))
        return attempts

    # -- red: report + operator hook
    def report(self, name, decl, res, z):
        return {"format": FORMAT, "project": name, "name": res["name"], "repo": res["repo"], "version": res["version"],
                "gaps": res["gaps"], "heal": {k: v for k, v in sorted((z.get("heal") or {}).items())},
                "ticket": z.get("ticket"), "ts": int(self.now())}

    def on_red(self, name, decl, res, z):
        cmd = self.cfg.get("on_red")
        if not cmd or z.get("reported") or z.get("ticket"):
            return
        path = os.path.join(self.state_dir, "reports", "%s-%s.json" % (name, res["version"]))
        write_json(path, self.report(name, decl, res, z), mode=0o644)
        if self._hook:
            good = self._hook(list(cmd), path)
        else:
            try:
                good = subprocess.run(list(cmd) + [path], capture_output=True, timeout=120).returncode == 0
            except (OSError, subprocess.SubprocessError):
                good = False
        if good:
            z["reported"] = self.now()
        else:
            self.out("  on_red hook failed for %s %s (retried next run)" % (name, res["version"]))

    # -- run
    def check(self, names=None):
        projects = self.projects()
        old = read_json(self.status_path, {}) or {}
        old_items = {r.get("project"): r for r in old.get("projects") or [] if isinstance(r, dict)}
        items = []
        for name in sorted(projects):
            if names and name not in names:
                if name in old_items:
                    items.append(old_items[name])
                continue
            items.append(self.project_run(name, projects[name]))
        if not self.dry_run:    # a dry run never leaves heal / hook state behind
            write_json(os.path.join(self.state_dir, "state.json"), self.state)
        status = self.build_status(items)
        write_json(self.status_path, status, mode=0o644)
        return status

    def project_run(self, name, decl):
        now = self.now()
        try:
            res = self.inspect(name, decl)
        except NetError as e:
            return {"project": name, "name": decl.get("name") or name, "repo": decl.get("repo"), "stage": "unknown",
                    "gaps": [], "text": "not checkable: %s" % clean(e, 160)}
        z = self.state.setdefault(name, {})
        if z.get("version") != res["version"]:
            # new version: heal / ticket / report state starts over; confirmed signatures (key = file name with version) stay
            keep = dict(z.get("minisign_ok") or {})
            z.clear()
            z.update(version=res["version"], seen_ts=now, minisign_ok=keep)
        since = res.get("published_ts") or z.get("seen_ts") or now
        age = max(0, now - min(since, z.get("seen_ts") or now))
        grace = int(decl.get("grace_min") or DEFAULT_GRACE_MIN) * 60
        out = {"project": name, "name": res["name"], "repo": res["repo"], "version": res["version"],
               "age_min": int(age // 60), "gaps": res["gaps"], "checked": res["checked"], "hints": res["hints"][:10],
               "ci_running": res["ci_running"], "heal": [], "ticket": z.get("ticket"),
               "report_only": bool(decl.get("report_only"))}
        if not res["version"]:
            out.update(stage="unknown", text="no release yet")
            return out
        if not res["gaps"]:
            z.pop("gap_ts", None)
            if z.get("red_ts") or z.get("heal"):
                z["healed_ts"] = now
            out.update(stage="green", text="complete (%d checks)" % res["checked"], healed=bool(z.get("healed_ts")))
            z.pop("red_ts", None)
            return out
        z.setdefault("gap_ts", now)
        if age < grace or (res["ci_running"] and age < CI_CAP_S):
            out.update(stage="waiting", text="%d open, %s" % (
                len(res["gaps"]), "CI of the tag is still running" if res["ci_running"] else "grace period %d min" % (grace // 60)))
            return out
        if now - z["gap_ts"] < CONFIRM_S:
            out.update(stage="waiting", text="%d open, being confirmed (gap for %d min)" % (
                len(res["gaps"]), (now - z["gap_ts"]) // 60))
            return out
        if self.heal_on and not self.stopped() and not decl.get("report_only") and not self.dry_run:
            out["heal"] = self.heal(name, decl, res, z)
        last = max([float((w or {}).get("ts") or 0) for w in (z.get("heal") or {}).values()] or [0])
        if out["heal"] or (last and now - last < HEAL_WAIT_S):
            out.update(stage="gap", text="%d open, self-healing in progress" % len(res["gaps"]))
            return out
        z.setdefault("red_ts", now)
        out.update(stage="red", text="%d gap(s): %s" % (len(res["gaps"]), "; ".join(x["text"] for x in res["gaps"][:3])))
        if not self.stopped() and not self.dry_run:
            self.on_red(name, decl, res, z)
            out["ticket"] = z.get("ticket")
            out["reported"] = bool(z.get("reported"))
        return out

    def build_status(self, items):
        now = self.now()
        bad = [r for r in items if r.get("stage") in ("red", "gap")]
        waiting = [r for r in items if r.get("stage") == "waiting"]
        unknown = [r for r in items if r.get("stage") == "unknown" and str(r.get("text", "")).startswith("not checkable")]
        if bad:
            parts = ["%s %s (%s)" % (r.get("name"), r.get("version") or "", (r.get("gaps") or [{}])[0].get("text")
                                     or r.get("text")) for r in bad]
            line, overall = "Release check: gaps - " + "; ".join(parts), "gaps"
        elif waiting or unknown:
            line = "Release check: all published releases green, %s" % ", ".join(
                ["%s waiting for CI/grace" % r.get("name") for r in waiting] +
                ["%s not checkable" % r.get("name") for r in unknown])
            overall = "waiting"
        else:
            line, overall = "Release check: all green (%d projects)" % len(items), "green"
        return {"format": FORMAT, "version": VERSION, "ts": int(now), "overall": overall, "line": clean(line, 600),
                "projects": items, "excluded": sorted(self.cfg.get("excluded") or {}), "stopped": self.stopped()}

    # -- history (older releases, check only)
    def history(self, name, count=5):
        decl = self.projects().get(name)
        if not decl:
            raise SystemExit("unknown project: %s" % name)
        rel = self.net.gh_json("repos/%s/releases?per_page=%d" % (decl["repo"], min(50, count * 2))) or []
        cand = sorted([r for r in rel if not r.get("draft") and vt(r.get("tag_name"))], key=lambda x: vt(x["tag_name"]),
                      reverse=True)[:count]
        out = []
        for i, r in enumerate(cand):
            v = vs(vt(r["tag_name"]))
            e = self.inspect(name, decl, version=v, newest=(i == 0), release=r)
            out.append((v, e["gaps"], e["checked"]))
        return out


# ---------------------------------------------------------------- triggers, lock, text
def drop_trigger(state_dir, name, version=""):
    if not NAME_RE.match(name or "") or (version and not vt(version)):
        raise SystemExit("invalid trigger")
    write_json(os.path.join(state_dir, "trigger", "%s.json" % name),
               {"project": name, "version": version, "ts": int(time.time())}, mode=0o644)


def take_triggers(state_dir):
    d = os.path.join(state_dir, "trigger")
    names = set()
    try:
        files = os.listdir(d)
    except OSError:
        return names
    for f in files:
        if not f.endswith(".json"):
            continue
        x = read_json(os.path.join(d, f)) or {}
        if NAME_RE.match(str(x.get("project") or "")):
            names.add(x["project"])
        try:
            os.unlink(os.path.join(d, f))
        except OSError:
            pass
    return names


def lock(state_dir, wait=False):
    os.makedirs(state_dir, exist_ok=True)
    f = open(os.path.join(state_dir, ".lock"), "w")
    end = time.time() + (900 if wait else 0)
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() >= end:
                return None
            time.sleep(5)


def status_text(status):
    lines = [status.get("line") or "-"]
    for r in status.get("projects") or []:
        lines.append("  %-22s %-9s %-9s %s" % (r.get("name") or r.get("project"), r.get("version") or "-", r.get("stage"),
                                               r.get("text") or ""))
        for x in (r.get("gaps") or [])[:8]:
            lines.append("      - %s" % x.get("text"))
    return "\n".join(lines)


def summary_line(status):
    o = status.get("overall")
    rows = status.get("projects") or []
    if o == "gaps":
        names = ", ".join(sorted({"%s %s" % (r.get("project"), r.get("version") or "") for r in rows
                                  if r.get("stage") in ("red", "gap")}))
        return "RELEASE-CHECK: gaps (%s)" % names
    if o == "waiting":
        return "RELEASE-CHECK: waiting (%d projects, %d waiting)" % (len(rows), sum(r.get("stage") == "waiting" for r in rows))
    return "RELEASE-CHECK: green (%d projects)" % len(rows)


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config")
    common.add_argument("--state-dir")
    common.add_argument("--status")
    ap = argparse.ArgumentParser(description="Release check: does the published release contain everything advertised?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("check", parents=[common])
    a.add_argument("--project", action="append")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--no-heal", action="store_true")
    sub.add_parser("status", parents=[common])
    a = sub.add_parser("history", parents=[common])
    a.add_argument("project")
    a.add_argument("--count", type=int, default=5)
    a = sub.add_parser("assign", parents=[common])
    a.add_argument("project")
    a.add_argument("version")
    a.add_argument("ticket")
    a.add_argument("--reason", required=True)
    a = sub.add_parser("trigger", parents=[common])
    a.add_argument("project")
    a.add_argument("version", nargs="?", default="")
    sub.add_parser("run-triggers", parents=[common])
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    state_dir = args.state_dir or os.environ.get("RELEASE_CHECK_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "release-check")
    status_path = args.status or os.path.join(state_dir, "status.json")
    config_path = args.config or os.environ.get("RELEASE_CHECK_CONFIG") or os.path.join(HERE, "deliverables.json")

    if args.cmd == "trigger":
        try:
            drop_trigger(state_dir, args.project, args.version)
        except SystemExit as e:
            print(e)
            return 2
        return 0
    if args.cmd == "status":
        print(status_text(read_json(status_path, {}) or {}))
        return 0
    cfg, err = load_config(config_path)
    if err:
        print(err, file=sys.stderr)
        return 2
    try:
        if args.cmd == "history":
            for v, gaps, n in Checker(cfg, state_dir, status_path).history(args.project, args.count):
                print("%s: %s (%d checks)" % (v, "complete" if not gaps else "%d gap(s)" % len(gaps), n))
                for x in gaps[:12]:
                    print("   - %s" % x["text"])
            return 0
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    except NetError as e:
        print("not checkable: %s" % clean(e, 160), file=sys.stderr)
        return 2
    lk = lock(state_dir, wait=(args.cmd == "run-triggers"))
    if not lk:
        print("already running - nothing to do")
        return 0
    if args.cmd == "assign":
        if args.project not in (cfg.get("projects") or {}) or not vt(args.version):
            print("unknown project or version", file=sys.stderr)
            return 2
        c = Checker(cfg, state_dir, status_path)
        z = c.state.setdefault(args.project, {})
        if z.get("version") not in (None, args.version):
            z.clear()
        z.update(version=args.version, ticket=clean(args.ticket, 80), assigned_reason=clean(args.reason, 200),
                 seen_ts=z.get("seen_ts") or time.time())
        write_json(os.path.join(state_dir, "state.json"), c.state)
        print("%s v%s -> ticket %s recorded (no hook for this version)" % (args.project, args.version, clean(args.ticket, 80)))
        return 0
    if args.cmd == "run-triggers":
        names = take_triggers(state_dir)
        if not names:
            return 0
        status = Checker(cfg, state_dir, status_path).check(names=names)
    else:
        status = Checker(cfg, state_dir, status_path, dry_run=args.dry_run, heal=not args.no_heal).check(
            names=set(args.project or []) or None)
    print(status_text(status))
    print(summary_line(status))
    return 1 if status["overall"] == "gaps" else 0


if __name__ == "__main__":
    sys.exit(main())
