#!/usr/bin/env python3
"""Task chain for AIWerkstatt: after each finished task, file the next one.

Talks to the AIWerkstatt HTTP API with a service token of the ``chain`` profile (see
docs/security.md). Python standard library only; works with ``python3 -I``.

    chain.py next [--done <id> --result <file>]   record a result, then start what is ready
    chain.py status [--json]
    chain.py pause | resume                       soft pause: no new tasks are started
    chain.py manual-done <step|external-key|id>   confirm a human step or an external condition
    chain.py approve <id>                         allow an optional task
    chain.py release --id <id> --gate-log <name>  gate green = release: verify a gate log, then file a broker request
    chain.py verify [<version>]                   measure a release (tag, GitHub release, image digest, latest)
    chain.py check                                self-healing (run it from a timer)
    chain.py simulate                             dry run of the whole plan, no API, no state

Global options (or environment): ``--plan`` / AIWERKSTATT_CHAIN_PLAN, ``--root`` /
AIWERKSTATT_CHAIN_ROOT (state, status, results; default <data>/chain), ``--token-file`` /
AIWERKSTATT_CHAIN_TOKEN_FILE (default <root>/token, must be mode 0600), ``--url`` /
AIWERKSTATT_CHAIN_URL, ``--project`` / AIWERKSTATT_CHAIN_PROJECT, ``--prefix`` /
AIWERKSTATT_CHAIN_TITLE_PREFIX. The token is only ever read from the file, never from argv.

Files in the root: ``state.json`` (the chain), ``status.json`` (written atomically, for
dashboards), ``results/<id>.json`` (written by the agent), ``KILL`` (kill switch: nothing
is started or changed while it exists), ``PAUSE`` (soft pause), ``RELEASE_OFF`` (releases rest,
building goes on; the hard ``KILL`` stops releases too).

The chain never creates a tag. A release is a request to a release broker (a drop box
directory); the broker runs its own checks and creates the tag. See docs/chain.md.

Exit codes: 0 ok, 2 error, 3 kill switch on or release held.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# Hard ceilings. A plan can lower them, never raise them.
HARD_LIMITS = {"per_day": 4, "per_track": 1, "total": 2}
MAX_RED = 3                     # red results in a row before a task is blocked
DEFAULT_HOURS = 1.0
STALL_FACTOR = 2                # stalled after this many times the expected hours
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
KEY_RE = ID_RE
TASK_KEYS = {"id", "track", "title", "model", "effort", "depends_on", "prompt", "check",
             "release", "optional", "hours"}
RESULT_KEYS = {"id", "branch", "commit", "tests", "gate", "manual_steps"}
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
RESULT_RELEASES = ("published", "requested", "held", "none")
RESULT_KEYS.add("release")
# ---- gate green = release
GATE_MAX_AGE_H = 48
GATE_MAX_BYTES = 2 * 1024 * 1024
GATE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9._/-]{1,200}$")
SUMMARY_RE = re.compile(r"SUMMARY \(version (\S+), commit (\S+), image (.+)\)\s*$")
CHECK_INTERVAL_S = 10 * 60      # external releases: look at most every 10 minutes (rate limits)
REQUESTED_MAX_S = 3 * 3600      # a broker request without an answer for this long is raised as an alert
RELEASE_KEYS = {"repo", "image", "queue_dir", "broker_status", "gate_logs", "clone", "branch",
                "version_file", "identity", "slug", "max_age_hours"}
# What really needs a person at the main workstation. Everything else is the chain's or the
# coordinator's business. A plan may add its own reasons under "human_reasons".
HUMAN_REASONS = (
    (r"signature|signing key|\bcode[- ]?sign", "signing keys live only on the main workstation"),
    (r"passkey|biometric|face ?id", "passkeys and biometrics need the main workstation"),
    (r"real (desktop )?browser|desktop browser", "needs a look in the real desktop browser"),
)
RELEASE_WORDS = re.compile(r"\brelease|\bpublish|\btag(ging)?\b|\bpromote", re.I)
DOC_WORDS = re.compile(r"\bvault\b|\bmemory\b|master doc|\bnotes?\b|\binbox\b|documentation", re.I)


class ChainError(Exception):
    pass


class Killed(ChainError):
    pass


class ApiError(ChainError):
    def __init__(self, status, message):
        super().__init__("API answered %s: %s" % (status, message))
        self.status = status


# ---------- plan ----------

def _check_text(v, name, limit, required=False):
    if v is None or v == "":
        if required:
            raise ChainError("%s is required" % name)
        return ""
    if not isinstance(v, str) or len(v) > limit:
        raise ChainError("%s must be text of at most %d characters" % (name, limit))
    return v


def load_plan(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise ChainError("cannot read plan %s: %s" % (path, e))
    return validate_plan(raw)


def validate_plan(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list) or not raw["tasks"]:
        raise ChainError("plan needs a non-empty 'tasks' list")
    external = raw.get("external") or {}
    if not isinstance(external, dict) or any(not KEY_RE.match(str(k)) or not isinstance(v, str)
                                            for k, v in external.items()):
        raise ChainError("'external' must map keys to descriptions")
    tasks, ids = {}, []
    for t in raw["tasks"]:
        if not isinstance(t, dict):
            raise ChainError("every task must be an object")
        extra = set(t) - TASK_KEYS
        if extra:
            raise ChainError("task %r: unknown field(s) %s" % (t.get("id"), ", ".join(sorted(extra))))
        tid = t.get("id")
        if not isinstance(tid, str) or not ID_RE.match(tid) or tid in tasks:
            raise ChainError("task id missing, malformed or duplicate: %r" % (tid,))
        track = t.get("track")
        if not isinstance(track, str) or not ID_RE.match(track):
            raise ChainError("task %s: 'track' missing or malformed" % tid)
        hours = t.get("hours", DEFAULT_HOURS)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= 72:
            raise ChainError("task %s: 'hours' must be a number in (0, 72]" % tid)
        deps = t.get("depends_on") or []
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            raise ChainError("task %s: 'depends_on' must be a list of ids" % tid)
        tasks[tid] = {
            "id": tid, "track": track,
            "title": _check_text(t.get("title"), "title", 120, True),
            "model": _check_text(t.get("model"), "model", 80),
            "effort": _check_text(t.get("effort"), "effort", 20),
            "prompt": _check_text(t.get("prompt"), "prompt", 20000, True),
            "check": _check_text(t.get("check"), "check", 4000),
            "depends_on": deps, "hours": float(hours),
            "release": bool(t.get("release", False)), "optional": bool(t.get("optional", False))}
        if "\n" in tasks[tid]["title"]:
            raise ChainError("task %s: title must be one line" % tid)
        ids.append(tid)
    for t in tasks.values():
        for d in t["depends_on"]:
            if d.startswith("ext:"):
                if d[4:] not in external:
                    raise ChainError("task %s: unknown external %s" % (t["id"], d))
            elif d not in tasks:
                raise ChainError("task %s: unknown dependency %s" % (t["id"], d))
    seen, done = set(), set()

    def visit(i, stack):
        if i in done:
            return
        if i in stack:
            raise ChainError("dependency cycle at %s" % i)
        for d in tasks[i]["depends_on"]:
            if not d.startswith("ext:"):
                visit(d, stack | {i})
        done.add(i)
    for i in ids:
        visit(i, frozenset())
    order = raw.get("order") or ids
    if not isinstance(order, list) or sorted(order) != sorted(ids):
        raise ChainError("'order' must list every task id exactly once")
    limits = dict(HARD_LIMITS)
    plan_limits = raw.get("limits") or {}
    if not isinstance(plan_limits, dict) or set(plan_limits) - set(HARD_LIMITS):
        raise ChainError("'limits' may only contain %s" % ", ".join(HARD_LIMITS))
    for k, v in plan_limits.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ChainError("limit %s must be an integer >= 1" % k)
        limits[k] = min(v, HARD_LIMITS[k])   # lower only
    release = _validate_release(raw.get("release"))
    ext_rel = raw.get("extern_release") or {}
    if not isinstance(ext_rel, dict):
        raise ChainError("'extern_release' must map external keys to {repo, version}")
    for k, v in ext_rel.items():
        if k not in external:
            raise ChainError("extern_release %s must also be listed under 'external'" % k)
        if (not isinstance(v, dict) or set(v) != {"repo", "version"} or not isinstance(v["repo"], str)
                or not REPO_RE.match(v["repo"]) or not isinstance(v["version"], str)
                or not VERSION_RE.match(v["version"])):
            raise ChainError("extern_release %s needs exactly repo (owner/name) and version (X.Y.Z)" % k)
    reasons = []
    for r in raw.get("human_reasons") or []:
        if (not isinstance(r, dict) or set(r) != {"pattern", "reason"} or not isinstance(r["pattern"], str)
                or not isinstance(r["reason"], str) or not r["reason"].strip() or len(r["reason"]) > 200):
            raise ChainError("'human_reasons' needs objects with pattern and reason")
        try:
            re.compile(r["pattern"])
        except re.error:
            raise ChainError("human_reasons: bad pattern %r" % r["pattern"])
        reasons.append((r["pattern"], r["reason"].strip()))
    return {"tasks": tasks, "order": list(order), "limits": limits, "external": dict(external),
            "release": release, "extern_release": {k: dict(v) for k, v in ext_rel.items()},
            "human_reasons": reasons}


def _abs_path(v, name):
    if not isinstance(v, str) or not os.path.isabs(v) or "\0" in v:
        raise ChainError("release.%s must be an absolute path" % name)
    return os.path.normpath(v)


def _validate_release(raw):
    """The optional ``release`` block. Without it the chain never requests a release."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict) or set(raw) - RELEASE_KEYS:
        raise ChainError("'release' may only contain %s" % ", ".join(sorted(RELEASE_KEYS)))
    repo = raw.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise ChainError("release.repo must look like owner/name")
    image = raw.get("image") or "ghcr.io/" + repo.lower()
    if not isinstance(image, str) or not IMAGE_RE.match(image):
        raise ChainError("release.image must look like registry.example/owner/app")
    for k in ("identity", "slug"):
        v = raw.get(k) or ("chain" if k == "identity" else repo.split("/")[1].lower())
        if not isinstance(v, str) or not ID_RE.match(v):
            raise ChainError("release.%s is malformed" % k)
        raw = dict(raw, **{k: v})
    hours = raw.get("max_age_hours", GATE_MAX_AGE_H)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= GATE_MAX_AGE_H:
        raise ChainError("release.max_age_hours must be a number in (0, %d]" % GATE_MAX_AGE_H)
    branch = raw.get("branch") or "main"
    version_file = raw.get("version_file") or "VERSION"
    if not BRANCH_RE.match(branch) or ".." in branch or not isinstance(version_file, str) \
            or version_file.startswith("/") or ".." in version_file.split("/"):
        raise ChainError("release.branch or release.version_file is malformed")
    out = {"repo": repo, "image": image, "identity": raw["identity"], "slug": raw["slug"],
           "branch": branch, "version_file": version_file, "max_age_hours": float(hours)}
    for k in ("queue_dir", "broker_status", "gate_logs", "clone"):
        out[k] = _abs_path(raw[k], k) if raw.get(k) else ""
    if not out["queue_dir"] or not out["clone"]:
        raise ChainError("release.queue_dir and release.clone are required")
    return out


def validate_result(obj, expect_id):
    """Check a result file: only known fields, every value of the expected shape."""
    if not isinstance(obj, dict):
        raise ChainError("result must be a JSON object")
    extra = set(obj) - RESULT_KEYS
    if extra:
        raise ChainError("result: unknown field(s) %s" % ", ".join(sorted(extra)))
    if obj.get("id") != expect_id:
        raise ChainError("result id %r does not match %r" % (obj.get("id"), expect_id))
    if obj.get("tests") not in ("green", "red"):
        raise ChainError("result: 'tests' must be green or red")
    if obj.get("gate") not in ("green", "open", "red"):
        raise ChainError("result: 'gate' must be green, open or red")
    if obj.get("release", "none") not in RESULT_RELEASES:
        raise ChainError("result: 'release' must be one of %s" % ", ".join(RESULT_RELEASES))
    branch, commit = obj.get("branch", ""), obj.get("commit", "")
    if branch and (not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch):
        raise ChainError("result: malformed 'branch'")
    if commit and (not isinstance(commit, str) or not COMMIT_RE.match(commit)):
        raise ChainError("result: malformed 'commit'")
    steps = obj.get("manual_steps") or []
    if (not isinstance(steps, list) or len(steps) > 20 or
            any(not isinstance(s, str) or not s.strip() or len(s) > 300 for s in steps)):
        raise ChainError("result: 'manual_steps' must be up to 20 short texts")
    return {"id": expect_id, "branch": branch, "commit": commit, "tests": obj["tests"],
            "gate": obj["gate"], "release": obj.get("release", "none"),
            "manual_steps": [s.strip() for s in steps]}


def clean(v, n):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v if v is not None else ""))[:n]


def classify_step(text, plan, release_task=False):
    """Sort a step from a result: (kind, reason).
    human = really needs a person at the main workstation (always with a reason),
    chain = the chain does it itself (the release), inbox = notes and docs travel by the inbox,
    coordinator = a decision or an acceptance."""
    t = str(text or "")
    for pat, reason in list(HUMAN_REASONS) + [tuple(r) for r in plan.get("human_reasons", [])]:
        if re.search(pat, t, re.I):
            return "human", reason
    if RELEASE_WORDS.search(t):
        if release_task and plan.get("release"):
            return "chain", "the chain releases: gate green = release (chain.py release)"
        return "coordinator", "tag or release without a release path of the chain: coordinator signs off (agents never tag)"
    if DOC_WORDS.search(t):
        return "inbox", "notes and documentation travel through the inbox"
    return "coordinator", "decision or acceptance by the coordinator"


def check_gate_log(folder, name, now, max_hours, image):
    """Check a gate log like the broker does, fail closed. -> (ok, reason, info)
    info: version, commit, image, age_h. The log must be a regular file (no symlink) of at most
    2 MB inside ``folder``, fresh, with the line ``GATE: GREEN``, without ``GATE: RED``, and with
    exactly one distinct ``SUMMARY (version V, commit C, image I)`` line whose image is the
    published ``<image>:V``."""
    info = {}
    if not isinstance(name, str) or not GATE_NAME_RE.match(name):
        return False, "gate log name is invalid (a bare file name from the gate log folder)", info
    path = os.path.join(folder, name)
    try:
        st = os.lstat(path)
    except OSError:
        return False, "gate log %s not found" % name, info
    if stat.S_ISLNK(st.st_mode):
        return False, "gate log is a symlink", info
    if not stat.S_ISREG(st.st_mode):
        return False, "gate log is not a regular file", info
    if st.st_size > GATE_MAX_BYTES:
        return False, "gate log is larger than 2 MB", info
    info["age_h"] = round((now - st.st_mtime) / 3600.0, 1)
    if now - st.st_mtime > max_hours * 3600:
        return False, "gate log is older than %g h (run the gate again)" % max_hours, info
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
            lines = [z.strip() for z in fh.read().splitlines()]
    except OSError:
        return False, "gate log not readable", info
    if any(z.startswith("GATE: RED") for z in lines):  # also "GATE: RED (3)"
        return False, "the gate is red (GATE: RED): never release", info
    if "GATE: GREEN" not in lines:
        return False, "line GATE: GREEN is missing", info
    sums = [z for z in lines if "SUMMARY (" in z]
    if not sums:
        return False, "SUMMARY line is missing", info
    seen = set()
    for z in sums:
        m = SUMMARY_RE.search(z)
        if not m:
            return False, "SUMMARY line is not readable", info
        seen.add(m.groups())
    if len(seen) != 1:
        return False, "several different SUMMARY lines", info
    version, commit, img = seen.pop()
    info.update({"version": version, "commit": commit, "image": img})
    if not VERSION_RE.match(version):
        return False, "version %r in the gate log is invalid" % clean(version, 20), info
    if not re.match(r"^[0-9a-f]{7,40}$", commit):
        return False, "commit %r in the gate log is invalid" % clean(commit, 20), info
    if img != "%s:%s" % (image, version):
        return False, "the gate checked image %s, not the published %s:%s" % (clean(img, 80), image, version), info
    return True, "OK", info


def _http(url, headers=None, method="GET"):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "aiwerkstatt-chain")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read() if method != "HEAD" else b""
            return r.status, (json.loads(raw) if raw else {}), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, {}, {}
    except (OSError, ValueError):
        return 0, {}, {}


def image_digest(image, ref):
    """Digest of <image>:<ref> from a registry with anonymous pull tokens (public package), or ''."""
    host, _, path = image.partition("/")
    code, body, _ = _http("https://%s/token?scope=repository:%s:pull" % (host, path))
    token = body.get("token") if code == 200 else None
    if not token:
        return ""
    accept = ", ".join(("application/vnd.oci.image.index.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"))
    code, _, head = _http("https://%s/v2/%s/manifests/%s" % (host, path, ref),
                          {"Authorization": "Bearer " + token, "Accept": accept}, "HEAD")
    if code != 200:
        return ""
    for k, v in head.items():
        if k.lower() == "docker-content-digest" and str(v).startswith("sha256:"):
            return str(v)
    return ""


def release_evidence(repo, image, version):
    """Is <repo> v<version> really out? Tag + GitHub release (no draft) + image :<version>,
    read-only and anonymous. -> {ok, reason, tag, tag_commit, digest, latest_digest, latest_same}"""
    if not REPO_RE.match(repo or "") or not VERSION_RE.match(version or ""):
        return {"ok": False, "reason": "repo or version invalid"}
    tag = "v" + version
    out = {"ok": False, "tag": tag, "reason": ""}
    api = "https://api.github.com/repos/%s" % repo
    accept = {"Accept": "application/vnd.github+json"}
    code, body, _ = _http("%s/git/ref/tags/%s" % (api, tag), accept)
    if code != 200:
        out["reason"] = "tag %s missing (HTTP %s)" % (tag, code)
        return out
    out["tag_commit"] = str((body.get("object") or {}).get("sha", ""))[:12]
    code, body, _ = _http("%s/releases/tags/%s" % (api, tag), accept)
    if code != 200 or body.get("draft"):
        out["reason"] = "GitHub release %s missing%s (HTTP %s)" % (tag, " (draft)" if body.get("draft") else "", code)
        return out
    out["digest"] = image_digest(image, version)
    if not out["digest"]:
        out["reason"] = "image %s:%s missing" % (image, version)
        return out
    out["latest_digest"] = image_digest(image, "latest")
    out["latest_same"] = bool(out["latest_digest"]) and out["latest_digest"] == out["digest"]
    out["ok"], out["reason"] = True, "OK"
    return out


# ---------- infrastructure ----------

class Clock:
    def now(self):
        return time.time()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def dt_parse(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def write_atomic(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Api:
    """The AIWerkstatt API as the chain token sees it: create a task, read one, follow up."""

    def __init__(self, url, token_file, project, prefix, timeout=30):
        self.url, self.token_file, self.project = url.rstrip("/"), token_file, project
        self.prefix, self.timeout = prefix, timeout

    def _token(self):
        try:
            st = os.stat(self.token_file)
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise ChainError("token file %s must not be readable by group or others (chmod 600)"
                                 % self.token_file)
            with open(self.token_file, "r", encoding="utf-8") as f:
                tok = f.read().strip()
        except OSError as e:
            raise ChainError("cannot read token file: %s" % e.strerror)
        if not tok:
            raise ChainError("token file is empty")
        return tok

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                msg = ""
            raise ApiError(e.code, msg or e.reason)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ChainError("API not reachable: %s" % getattr(e, "reason", e))

    def create(self, title, request, model="", effort=""):
        body = {"title": title, "request": request}
        if model:
            body["model"] = model
        if effort:
            body["effort"] = effort
        return int(self._call("POST", "/api/projects/%s/threads" % self.project, body)["id"])

    def get(self, thread):
        return self._call("GET", "/api/threads/%d" % thread)

    def comment(self, thread, text):
        self._call("POST", "/api/threads/%d/comment" % thread, {"text": text})


# ---------- the chain ----------

class Chain:
    def __init__(self, plan, root, api, clock=None, prefix=""):
        self.plan, self.root, self.api = plan, root, api
        self.clock, self.prefix = clock or Clock(), prefix
        self.state_path = os.path.join(root, "state.json")
        self.status_path = os.path.join(root, "status.json")
        self.hints = []
        # seams for tests and for other hosts: each takes plain values and returns plain values
        self.evidence = lambda repo, image, version: release_evidence(repo, image, version)
        self.main_state = self._main_state
        self.broker = self._broker

    # state
    def load(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                s = json.load(f)
        except FileNotFoundError:
            s = {}
        except (OSError, ValueError) as e:
            raise ChainError("state file unreadable (%s); fix or remove it by hand" % e)
        s.setdefault("tasks", {})
        s.setdefault("created", [])
        s.setdefault("approved", [])
        s.setdefault("external_done", [])
        s.setdefault("manual", {})
        s.setdefault("releases", {})
        s.setdefault("external_auto", {})
        s.setdefault("external_checked", {})
        s.setdefault("alerts", {})
        s.setdefault("taken_over", [])
        for tid in self.plan["tasks"]:
            s["tasks"].setdefault(tid, {"state": "pending", "red": 0})
        for k, m in s["manual"].items():     # old entries get sorted once, on loading
            if "kind" not in m:
                rel = k.startswith("release:") and self.plan["tasks"].get(m.get("task"), {}).get("release")
                m["kind"], m["reason"] = classify_step(m.get("text", ""), self.plan, bool(rel))
                if m["kind"] == "inbox":
                    m["kind"] = "coordinator"
        return s

    def save(self, s):
        write_atomic(self.state_path, json.dumps(s, indent=1, sort_keys=True) + "\n")
        self.write_status(s)

    def killed(self):
        return os.path.exists(os.path.join(self.root, "KILL"))

    def paused(self):
        return os.path.exists(os.path.join(self.root, "PAUSE"))

    def release_off(self):
        """Release kill switch: building goes on, nothing is published. The hard KILL stops releases too."""
        if os.path.exists(os.path.join(self.root, "RELEASE_OFF")):
            return "release kill switch RELEASE_OFF is on"
        if self.killed():
            return "kill switch KILL is on"
        return None

    def guard(self):
        if self.killed():
            raise Killed("kill switch is on (%s): nothing is changed" % os.path.join(self.root, "KILL"))

    def lock(self):
        os.makedirs(self.root, exist_ok=True)
        f = open(os.path.join(self.root, "lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    # rules
    def _deps_met(self, s, t):
        for d in t["depends_on"]:
            if d.startswith("ext:"):
                if d[4:] not in s["external_done"]:
                    return False
            else:
                if s["tasks"][d]["state"] != "done":
                    return False
                if any(not m["done"] for m in s["manual"].values() if m["task"] == d):
                    return False
        return True

    def _today(self):
        return iso(self.clock.now())[:10]

    def _eligible(self, s):
        out = []
        for tid in self.plan["order"]:
            t = self.plan["tasks"][tid]
            if s["tasks"][tid]["state"] != "pending":
                continue
            if t["optional"] and tid not in s["approved"]:
                continue
            if self._deps_met(s, t):
                out.append(t)
        return out

    def fill(self):
        """Start every task that is ready, within the limits. Returns the started ids."""
        self.guard()
        started = []
        if self.paused():
            self.hints.append("paused: no new tasks are started")
            return started
        lim = self.plan["limits"]
        while True:
            s = self.load()
            running = [i for i, v in s["tasks"].items() if v["state"] in ("running", "starting")]
            today = sum(1 for c in s["created"] if c[:10] == self._today())
            pick = None
            for t in self._eligible(s):
                if len(running) >= lim["total"] or today >= lim["per_day"]:
                    break
                if sum(1 for i in running if self.plan["tasks"][i]["track"] == t["track"]) >= lim["per_track"]:
                    continue
                pick = t
                break
            if not pick:
                return started
            now = iso(self.clock.now())
            s["tasks"][pick["id"]] = {"state": "starting", "red": 0, "started_at": now}
            s["created"].append(now)
            self.save(s)    # written before the call: a crash never starts a task twice
            try:
                thread = self.api.create(self.prefix + "%s: %s" % (pick["id"], pick["title"]),
                                         self._request(pick), pick["model"], pick["effort"])
            except ChainError as e:
                s["tasks"][pick["id"]] = {"state": "pending", "red": 0}
                s["created"].remove(now)
                self.save(s)
                self.hints.append("could not start %s: %s" % (pick["id"], e))
                return started
            s["tasks"][pick["id"]].update(state="running", thread=thread)
            self.save(s)
            started.append(pick["id"])

    def _request(self, t):
        parts = [t["prompt"].strip()]
        if t["check"]:
            parts.append("Check before you finish:\n" + t["check"].strip())
        parts.append(
            "When you are done, write a result file as JSON to %s with exactly these fields: "
            "id (\"%s\"), branch, commit, tests (\"green\" or \"red\"), gate (\"green\", \"open\" "
            "or \"red\"), manual_steps (list of things a human still has to do). Do not tag, "
            "release or publish anything." % (self.result_path(t["id"]), t["id"]))
        return "\n\n".join(parts)

    def result_path(self, tid):
        return os.path.join(self.root, "results", tid + ".json")

    # events
    def ingest(self, tid, result_file):
        """Record the result of a running task. Does not start anything."""
        self.guard()
        if tid not in self.plan["tasks"]:
            raise ChainError("unknown task %s" % tid)
        try:
            with open(result_file, "r", encoding="utf-8") as f:
                res = validate_result(json.load(f), tid)
        except (OSError, ValueError) as e:
            raise ChainError("cannot read result: %s" % e)
        s = self.load()
        st = s["tasks"][tid]
        if st["state"] == "done":
            return "already done"
        if st["state"] != "running":
            raise ChainError("task %s is %s, not running" % (tid, st["state"]))
        st.update(branch=res["branch"], commit=res["commit"], gate=res["gate"], tests=res["tests"])
        if res["tests"] == "red" or res["gate"] == "red":
            st["red"] = st.get("red", 0) + 1
            if st["red"] >= MAX_RED:
                st["state"] = "blocked"
                st["note"] = "red %d times in a row: needs a human" % MAX_RED
                outcome = "blocked"
            else:
                outcome = "red %d/%d" % (st["red"], MAX_RED)
                self._nudge(st, "Your result is red (attempt %d of %d). Fix the cause, run the "
                            "checks again and write a new result file." % (st["red"], MAX_RED))
        elif res["gate"] == "open":
            st["note"] = "gate still open"
            outcome = "gate open"
        else:
            st["state"] = "done"
            st["finished_at"] = iso(self.clock.now())
            st.pop("note", None)
            is_rel = self.plan["tasks"][tid]["release"]
            steps = list(res["manual_steps"])
            if is_rel:
                if self.plan["release"]:
                    steps.append("Release %s: the chain requests it once the gate is green" % tid)
                else:
                    steps.append("Release %s (no release path is configured; the chain never tags)" % tid)
            for n, text in enumerate(steps, 1):
                last_release = is_rel and n == len(steps)
                key = ("release:" + tid) if last_release else "%s:%d" % (tid, n)
                if last_release:
                    kind, reason = ("chain", "the chain releases: gate green = release (chain.py release)") \
                        if self.plan["release"] else ("coordinator", "no release path configured: coordinator releases")
                else:
                    kind, reason = classify_step(text, self.plan, is_rel)
                if kind == "chain" and not last_release and res["release"] in ("published", "requested"):
                    kind, reason = "inbox", "release already %s" % res["release"]
                if kind == "inbox":
                    s["taken_over"] = (s["taken_over"] + [{"task": tid, "text": clean(text, 200), "how": reason,
                                                           "at": iso(self.clock.now())}])[-50:]
                    continue
                s["manual"][key] = {"text": text, "task": tid, "done": False, "kind": kind, "reason": reason}
            outcome = "done"
        self.save(s)
        return outcome

    def _nudge(self, st, text):
        if st.get("thread"):
            try:
                self.api.comment(st["thread"], text)
            except ChainError as e:
                self.hints.append("could not send follow-up: %s" % e)

    def manual_done(self, key):
        self.guard()
        s = self.load()
        hit = False
        if key in s["manual"]:
            s["manual"][key]["done"] = hit = True
        elif key in self.plan["external"]:
            if key not in s["external_done"]:
                s["external_done"].append(key)
            hit = True
        elif key in self.plan["tasks"]:
            for m in s["manual"].values():
                if m["task"] == key:
                    m["done"] = hit = True
        if not hit:
            raise ChainError("no open step, external key or task named %s" % key)
        self.save(s)

    def approve(self, tid):
        self.guard()
        t = self.plan["tasks"].get(tid)
        if not t or not t["optional"]:
            raise ChainError("%s is not an optional task" % tid)
        s = self.load()
        if tid not in s["approved"]:
            s["approved"].append(tid)
        self.save(s)

    def check(self):
        """Self-healing. Ingest result files that appeared; a task that finished without a
        result, or has not moved for a long time, gets exactly one follow-up."""
        self.guard()
        ingested = []
        for tid, st in self.load()["tasks"].items():
            rp = self.result_path(tid)
            if st["state"] == "running" and os.path.exists(rp):
                try:
                    ingested.append((tid, self.ingest(tid, rp)))
                except ChainError as e:
                    self.hints.append("result of %s ignored: %s" % (tid, e))
        s = self.load()
        for tid, st in s["tasks"].items():
            if st["state"] == "starting":
                self.hints.append("%s: start was interrupted, check the project by hand" % tid)
            if st["state"] != "running" or not st.get("thread"):
                continue
            try:
                th = self.api.get(st["thread"])
            except ChainError as e:
                self.hints.append("%s: cannot read task: %s" % (tid, e))
                continue
            status = th.get("status")
            if status in ("done", "review"):
                if not st.get("nudged_missing"):
                    st["nudged_missing"] = True
                    self._nudge(st, "The task is finished but no result file was found at %s. "
                                "Write it now (id, branch, commit, tests, gate, manual_steps)."
                                % self.result_path(tid))
            elif status in ("failed", "stopped"):
                self.hints.append("%s: task %s, needs a human" % (tid, status))
            else:
                events = [e.get("created_at") for e in th.get("timeline", []) if e.get("created_at")]
                last = max(map(parse_iso, events)) if events else parse_iso(st["started_at"])
                limit = STALL_FACTOR * 3600 * self.plan["tasks"][tid]["hours"]
                if self.clock.now() - last > limit and not st.get("nudged_stalled"):
                    st["nudged_stalled"] = True
                    self._nudge(st, "No progress for a long time. Report where you stand, then "
                                "continue or write the result file.")
        self.save(s)
        if self.plan["release"] and not self.killed():
            if any(r.get("state") == "requested" for r in self.load()["releases"].values()):
                self.verify()
        if self.plan["extern_release"] and not self.killed():
            for key in self.check_external_releases():
                self.hints.append("external condition %s resolved: release is out" % key)
        return ingested

    # gate green = release
    def _main_state(self):
        """(version, commit) of origin/<branch> in the release clone, freshly fetched."""
        r = self.plan["release"]
        git = ["git", "-C", r["clone"]]
        try:
            subprocess.run(git + ["fetch", "-q", "origin", r["branch"]], capture_output=True, timeout=90)
            v = subprocess.run(git + ["show", "origin/%s:%s" % (r["branch"], r["version_file"])],
                               capture_output=True, text=True, timeout=30)
            c = subprocess.run(git + ["rev-parse", "origin/" + r["branch"]],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None, None
        if v.returncode or c.returncode:
            return None, None
        return v.stdout.strip(), c.stdout.strip()

    def _broker(self, body):
        """File the request in the broker's drop box, complete before visible. -> (ok, reason)"""
        r = self.plan["release"]
        target = os.path.join(r["queue_dir"], r["slug"] + ".req")
        if os.path.exists(target):
            return False, "an unprocessed request is already waiting (%s.req)" % r["slug"]
        try:
            fd, tmp = tempfile.mkstemp(prefix=".%s." % r["slug"], dir=r["queue_dir"])
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except OSError as e:
            return False, "request not writable (%s)" % type(e).__name__
        return True, "OK"

    def _record_release(self, s, ver, tid, log, commit, state, proof=None):
        r = s["releases"].get(ver) or {}
        now = self.clock.now()
        r.update({"version": ver, "task": tid or r.get("task"), "gate_log": log or r.get("gate_log"),
                  "commit": commit or r.get("commit"), "state": state, "at": iso(now), "ts": now})
        if state == "requested":
            r["requested_at"], r["requested_ts"] = iso(now), now
        if proof:
            r["proof"] = {k: proof.get(k) for k in ("tag", "tag_commit", "digest", "latest_digest", "latest_same")}
        r.pop("reason", None)
        s["releases"][ver] = r
        if state == "published":
            s["alerts"].pop("release:" + ver, None)
            for m in s["manual"].values():       # releases are cumulative: open "chain releases" steps are done
                if m.get("kind") == "chain" and not m["done"]:
                    m["done"], m["done_by"] = True, "release v%s" % ver

    def release(self, tid, log_name):
        """``release --id <task> --gate-log <name>``: verify, then file the broker request.
        -> (rc, text): 0 requested or already out, 3 held, 1 refused."""
        self.guard()
        rel = self.plan["release"]
        t = self.plan["tasks"].get(tid)
        if not rel:
            return 1, "no release block in the plan: the chain does not release"
        if not t:
            return 1, "unknown task %s" % clean(tid, 40)
        if not t["release"]:
            return 1, "task %s is not a release task (release: true)" % tid
        ok, why, info = check_gate_log(rel["gate_logs"] or os.path.join(self.root, "gate-logs"), log_name,
                                       self.clock.now(), rel["max_age_hours"], rel["image"])
        if not ok:
            return 1, "no release: %s" % why
        ver, commit = info["version"], info["commit"]
        main_ver, main_commit = self.main_state()
        if not main_ver or not main_commit:
            return 1, "no release: origin/%s of %s not readable (fail closed)" % (rel["branch"], rel["repo"])
        if main_ver != ver:
            return 1, "no release: the gate ran for %s, origin/%s is %s: run the gate again" % (ver, rel["branch"], main_ver)
        if not main_commit.startswith(commit):
            return 1, "no release: the gate ran for commit %s, origin/%s is %s: run the gate again" % (
                commit, rel["branch"], main_commit[:8])
        s = self.load()
        before = s["releases"].get(ver) or {}
        if before.get("state") == "published":
            return 0, "v%s is already published" % ver
        if before.get("state") == "requested" and self.clock.now() - float(before.get("ts") or 0) < REQUESTED_MAX_S:
            return 0, "v%s is already requested (%s): chain.py verify %s" % (ver, before.get("at"), ver)
        proof = self.evidence(rel["repo"], rel["image"], ver)
        if proof.get("ok"):
            self._record_release(s, ver, tid, log_name, commit, "published", proof)
            self.save(s)
            return 0, "v%s is already out (tag, release, image): nothing requested" % ver
        halt = self.release_off()
        if halt:
            s["releases"][ver] = dict(before, version=ver, task=tid, gate_log=log_name, commit=commit,
                                      state="held", reason=halt, at=iso(self.clock.now()), ts=self.clock.now())
            self.save(s)
            return 3, "gate green, but %s: not published, keep building (result: \"release\": \"held\")" % halt
        ok, why = self.broker({"mode": "release", "by": rel["identity"], "gate": "green", "gate_log": log_name})
        if not ok:
            return 1, "broker request failed: %s" % why
        self._record_release(s, ver, tid, log_name, commit, "requested")
        self.save(s)
        return 0, "v%s requested (gate log %s, %.1f h old): chain.py verify %s" % (ver, log_name, info.get("age_h", 0), ver)

    def verify(self, version=None):
        """Measure releases: broker answer, tag, GitHub release, image digest, :latest. -> list of dicts"""
        self.guard()
        rel = self.plan["release"]
        if not rel:
            raise ChainError("no release block in the plan")
        s = self.load()
        if version:
            if not VERSION_RE.match(version):
                raise ChainError("version %r is invalid" % clean(version, 20))
            targets = [version]
        else:
            targets = [v for v, r in s["releases"].items() if r.get("state") == "requested"]
        answer = {}
        if rel["broker_status"]:
            try:
                with open(rel["broker_status"], "r", encoding="utf-8") as f:
                    answer = json.load(f)
            except (OSError, ValueError):
                answer = {}
        out = []
        for ver in targets:
            r = s["releases"].get(ver) or {}
            proof = self.evidence(rel["repo"], rel["image"], ver)
            if proof.get("ok"):
                self._record_release(s, ver, r.get("task"), r.get("gate_log"), r.get("commit"), "published", proof)
                if not proof.get("latest_same"):
                    s["alerts"]["latest:" + ver] = "v%s is out but :latest points to another image" % ver
                else:
                    s["alerts"].pop("latest:" + ver, None)
                out.append({"version": ver, "state": "published", "proof": s["releases"][ver]["proof"]})
                continue
            b_state = str(answer.get("state", ""))
            since = float(r.get("requested_ts") or r.get("ts") or 0)
            try:
                b_ts = dt_parse(str(answer.get("ts", ""))) if answer.get("ts") else 0
            except ValueError:
                b_ts = 0
            if r.get("state") == "requested" and str(answer.get("version", "")) == ver and b_ts >= since - 60 \
                    and b_state in ("failed", "rejected", "error"):
                why = clean(answer.get("error") or answer.get("step") or b_state, 200)
                r.update(state="failed", reason=why, at=iso(self.clock.now()), ts=self.clock.now())
                s["releases"][ver] = r
                s["alerts"]["release:" + ver] = "the broker refused release v%s (%s); nothing is half published, " \
                                                "the next green gate requests it again" % (ver, why)
            elif r.get("state") == "requested" and self.clock.now() - since > REQUESTED_MAX_S:
                s["alerts"]["release:" + ver] = "request for v%s has had no result for over %d h (%s)" % (
                    ver, REQUESTED_MAX_S // 3600, proof.get("reason"))
            out.append({"version": ver, "state": (s["releases"].get(ver) or {}).get("state", "unknown"),
                        "reason": proof.get("reason")})
        self.save(s)
        return out

    def check_external_releases(self, force=False):
        """Resolve external conditions that are releases (plan ``extern_release``) once tag, release
        and image are provably out. At most every 10 minutes per condition."""
        s = self.load()
        resolved = []
        for key, v in self.plan["extern_release"].items():
            if key in s["external_done"]:
                continue
            last = float((s["external_checked"].get(key) or {}).get("ts") or 0)
            if not force and self.clock.now() - last < CHECK_INTERVAL_S:
                continue
            image = (self.plan["release"] or {}).get("image") if (self.plan["release"] or {}).get("repo") == v["repo"] \
                else "ghcr.io/" + v["repo"].lower()
            proof = self.evidence(v["repo"], image, v["version"])
            s["external_checked"][key] = {"ts": self.clock.now(), "at": iso(self.clock.now()),
                                          "ok": bool(proof.get("ok")), "reason": clean(proof.get("reason"), 200)}
            if proof.get("ok"):
                s["external_done"].append(key)
                s["external_auto"][key] = {"at": iso(self.clock.now()), "repo": v["repo"], "version": v["version"],
                                           "tag": proof.get("tag"), "digest": proof.get("digest")}
                resolved.append(key)
        self.save(s)
        return resolved

    # reporting
    def status(self, s=None):
        s = s or self.load()
        tasks = {}
        for tid in self.plan["order"]:
            t, v = self.plan["tasks"][tid], s["tasks"][tid]
            shown = v["state"]
            if shown == "pending" and t["optional"] and tid not in s["approved"]:
                shown = "optional"
            elif shown == "pending" and not self._deps_met(s, t):
                shown = "waiting"
            tasks[tid] = {"track": t["track"], "state": shown, "thread": v.get("thread"),
                          "red": v.get("red", 0), "note": v.get("note", ""),
                          "branch": v.get("branch", ""), "commit": v.get("commit", ""),
                          "started_at": v.get("started_at", ""), "finished_at": v.get("finished_at", "")}
        return {
            "updated": iso(self.clock.now()), "killed": self.killed(), "paused": self.paused(),
            "limits": self.plan["limits"],
            "started_today": sum(1 for c in s["created"] if c[:10] == self._today()),
            "tasks": tasks,
            "manual_pending": [{"key": k, "text": m["text"], "task": m["task"], "kind": m.get("kind", ""),
                                "reason": m.get("reason", "")}
                               for k, m in sorted(s["manual"].items()) if not m["done"]],
            "waiting_for_human": [{"key": k, "text": m["text"], "task": m["task"], "reason": m.get("reason", "")}
                                  for k, m in sorted(s["manual"].items())
                                  if not m["done"] and m.get("kind") == "human"],
            "waiting_for_coordinator": [{"key": k, "text": m["text"], "task": m["task"],
                                         "reason": m.get("reason", "")}
                                        for k, m in sorted(s["manual"].items())
                                        if not m["done"] and m.get("kind") == "coordinator"],
            "chain_open": [{"key": k, "text": m["text"], "task": m["task"]}
                           for k, m in sorted(s["manual"].items()) if not m["done"] and m.get("kind") == "chain"],
            "external_pending": [{"key": k, "text": v} for k, v in sorted(self.plan["external"].items())
                                 if k not in s["external_done"]],
            "external_auto": s["external_auto"],
            "releases": {v: {k: r.get(k) for k in ("state", "task", "commit", "at", "reason", "proof")}
                         for v, r in sorted(s["releases"].items(), key=lambda kv: kv[1].get("ts", 0))[-10:]},
            "release_off": bool(self.plan["release"]) and bool(self.release_off()),
            "alerts": dict(sorted(s["alerts"].items())),
            "taken_over": s["taken_over"][-10:],
            "hints": [("blocked: %s (%s)" % (i, v["note"])) for i, v in tasks.items()
                      if v["state"] == "blocked"] + self.hints}

    def write_status(self, s):
        write_atomic(self.status_path, json.dumps(self.status(s), indent=1, sort_keys=True) + "\n")


# ---------- simulation ----------

class FakeClock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def now(self):
        return self.t


class FakeApi:
    def __init__(self):
        self.n = 0
        self.titles = {}

    def create(self, title, request, model="", effort=""):
        self.n += 1
        self.titles[self.n] = title
        return self.n

    def get(self, thread):
        return {"status": "working", "timeline": []}

    def comment(self, thread, text):
        pass


def simulate(plan, out=print):
    """Walk the whole plan on a fake clock: every started task ends green, every human step
    and external condition is confirmed at once, optional tasks stay off. Writes nothing real."""
    root = tempfile.mkdtemp(prefix="chain-sim-")
    try:
        clock, api = FakeClock(), FakeApi()
        ch = Chain(plan, root, api, clock)
        for k in plan["external"]:
            ch.manual_done(k)
        day, rounds = 1, 0
        out("limits: %s" % json.dumps(plan["limits"], sort_keys=True))
        while rounds < 10000:
            rounds += 1
            started = ch.fill()
            for tid in started:
                out("day %d: start %s (track %s)" % (day, tid, plan["tasks"][tid]["track"]))
            s = ch.load()
            running = [i for i, v in s["tasks"].items() if v["state"] == "running"]
            if running:
                for tid in running:
                    rf = os.path.join(root, "r-%s.json" % tid)
                    write_atomic(rf, json.dumps({"id": tid, "branch": "work/" + tid, "commit": "abc1234",
                                                 "tests": "green", "gate": "green", "manual_steps": []}))
                    ch.ingest(tid, rf)
                    out("day %d: done  %s" % (day, tid))
                    if any(m["task"] == tid for m in ch.load()["manual"].values()):
                        ch.manual_done(tid)   # confirm the human steps of this task at once
                continue
            if any(s["tasks"][t["id"]]["state"] == "pending" and (not t["optional"])
                   and ch._deps_met(s, t) for t in plan["tasks"].values()):
                day += 1
                clock.t += 86400
                continue
            break
        s = ch.load()
        left = [i for i, v in s["tasks"].items() if v["state"] != "done"]
        out("finished after %d day(s): %d task(s) done, not run: %s"
            % (day, len(plan["tasks"]) - len(left), ", ".join(left) or "none"))
        return left
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------- command line ----------

def _default_root():
    data = os.environ.get("AIWERKSTATT_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(data, "chain")


def build_parser():
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="chain.py", description="AIWerkstatt task chain.")
    ap.add_argument("--root", default=env("AIWERKSTATT_CHAIN_ROOT") or _default_root())
    ap.add_argument("--plan", default=env("AIWERKSTATT_CHAIN_PLAN"))
    ap.add_argument("--token-file", default=env("AIWERKSTATT_CHAIN_TOKEN_FILE"))
    ap.add_argument("--url", default=env("AIWERKSTATT_CHAIN_URL")
                    or "http://127.0.0.1:%s" % env("AIWERKSTATT_PORT", "8095"))
    ap.add_argument("--project", default=env("AIWERKSTATT_CHAIN_PROJECT", ""))
    ap.add_argument("--prefix", default=env("AIWERKSTATT_CHAIN_TITLE_PREFIX", ""))
    ap.add_argument("--dry-run", action="store_true", help="never touch the API or the state")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("next")
    n.add_argument("--done")
    n.add_argument("--result")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    for c in ("pause", "resume", "check", "simulate"):
        sub.add_parser(c)
    sub.add_parser("manual-done").add_argument("key")
    sub.add_parser("approve").add_argument("id")
    rl = sub.add_parser("release")
    rl.add_argument("--id", required=True)
    rl.add_argument("--gate-log", required=True)
    sub.add_parser("verify").add_argument("version", nargs="?")
    return ap


def main(argv=None, out=print, api=None, clock=None):
    a = build_parser().parse_args(argv)
    try:
        plan = load_plan(a.plan or os.path.join(a.root, "plan.json"))
        if a.cmd == "simulate" or a.dry_run:
            if a.cmd not in ("simulate", "next"):
                raise ChainError("--dry-run only goes with simulate or next")
            simulate(plan, out)
            return 0
        if api is None:
            api = Api(a.url, a.token_file or os.path.join(a.root, "token"), a.project, a.prefix)
        ch = Chain(plan, a.root, api, clock, a.prefix)
        pause_file = os.path.join(a.root, "PAUSE")
        if a.cmd == "pause":
            write_atomic(pause_file, iso(ch.clock.now()) + "\n")
            out("paused")
            return 0
        if a.cmd == "resume":
            try:
                os.remove(pause_file)
            except FileNotFoundError:
                pass
            out("resumed")
            return 0
        lock = ch.lock()
        try:
            if a.cmd == "status":
                st = ch.status()
                if a.json:
                    out(json.dumps(st, indent=1, sort_keys=True))
                else:
                    out("chain: %s%s" % ("KILLED " if st["killed"] else "", "PAUSED" if st["paused"] else "running"))
                    for tid, v in st["tasks"].items():
                        out("  %-14s %-8s %-9s %s" % (tid, v["track"], v["state"], v["note"]))
                    for label, name in (("human", "waiting_for_human"), ("coordinator", "waiting_for_coordinator"),
                                        ("chain", "chain_open")):
                        for m in st[name]:
                            out("  %s %s: %s%s" % (label, m["key"], m["text"],
                                                  " (%s)" % m["reason"] if m.get("reason") else ""))
                    if not st["waiting_for_human"]:
                        out("  waiting for the main workstation: nothing")
                    for ver, r in st["releases"].items():
                        out("  release v%s: %s%s" % (ver, r["state"], " (%s)" % r["reason"] if r.get("reason") else ""))
                    if st["release_off"]:
                        out("  releases are resting (RELEASE_OFF or KILL)")
                    for k, v in st["alerts"].items():
                        out("  alert %s: %s" % (k, v))
                    for m in st["external_pending"]:
                        out("  external %s: %s" % (m["key"], m["text"]))
                    for h in st["hints"]:
                        out("  hint: %s" % h)
                return 0
            if a.cmd == "next":
                if bool(a.done) != bool(a.result):
                    raise ChainError("--done and --result belong together")
                if a.done:
                    out("%s: %s" % (a.done, ch.ingest(a.done, a.result)))
                for tid in ch.fill():
                    out("started %s" % tid)
            elif a.cmd == "manual-done":
                ch.manual_done(a.key)
                for tid in ch.fill():
                    out("started %s" % tid)
            elif a.cmd == "approve":
                ch.approve(a.id)
                for tid in ch.fill():
                    out("started %s" % tid)
            elif a.cmd == "release":
                rc, text = ch.release(a.id, a.gate_log)
                out(text)
                for tid in ch.fill():
                    out("started %s" % tid)
                return rc
            elif a.cmd == "verify":
                for r in ch.verify(a.version):
                    out("v%s: %s%s" % (r["version"], r["state"], " (%s)" % r["reason"] if r.get("reason") else ""))
            elif a.cmd == "check":
                for tid, outcome in ch.check():
                    out("%s: %s" % (tid, outcome))
                for tid in ch.fill():
                    out("started %s" % tid)
            for h in ch.hints:
                out("hint: %s" % h)
        finally:
            lock.close()
        return 0
    except Killed as e:
        print("chain: %s" % e, file=sys.stderr)
        return 3
    except ChainError as e:
        print("chain: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
