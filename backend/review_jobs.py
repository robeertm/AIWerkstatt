"""Review jobs — the hand-off record, the job phases, the finding format, the mode switch and the green cache
of the AI-free review runner (build/review split, part 1). See docs/review-runner.md.

A *job* is one review round of one task (thread): "check commit X of this project with these commands".
It is stored as files below ``<data>/review/jobs/``:

  ``<job>.json``          the hand-off (written once, by the API)
  ``<job>.run.json``      the runner's state (steps, exit codes, durations, log paths)
  ``<job>.finding.json``  the reviewer's decision (green or rework, with findings)

Nothing here talks to Docker; ``review_runner`` executes the steps. The module is pure and can be imported
without the web app (tests, tools).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time

import config

FORMAT_JOB = "review-job/1"
FORMAT_MODE = "review-runner/1"
FORMAT_FINDING = "review-finding/1"
MODES = ("off", "probe", "on")
SEVERITIES = ("critical", "high", "medium", "low")
DECISIONS = ("green", "rework")
PHASES = ("waiting_runner", "running", "waiting_review", "in_review", "green", "rework", "escalated", "cancelled")
OPEN = ("waiting_runner", "running", "waiting_review", "in_review")
JOB_RX = re.compile(r"^j(\d{1,9})-r(\d{1,3})$")
COMMIT_RX = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
BRANCH_RX = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SLUG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TARGET_RX = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MEMORY_RX = re.compile(r"^[0-9]{1,6}[kmg]?$")
MAX_STEPS = 20
MAX_CMD = 2000
MAX_FINDINGS = 50
OVERRIDE_MIN = 15
PROBE_MAX_DEFAULT = 2
DEFAULT_TARGETS = {
    # isolated: no network at all — unit tests, linters, builds with vendored dependencies
    "local": {"limit": 1, "network": "none"},
    # for checks that must fetch dependencies (npm ci, pip install) — still no credentials, no Docker access
    "online": {"limit": 1, "network": "bridge"},
}

_LOCK = threading.Lock()


# ---------------------------------------------------------------- paths and small file helpers

def base_dir(data=None):
    return os.path.join(data or config.DATA, "review")


def jobs_dir(data=None):
    return os.path.join(base_dir(data), "jobs")


def logs_dir(data=None):
    return os.path.join(base_dir(data), "logs")


def mode_file(data=None):
    return os.path.join(data or config.DATA, "review-runner.json")


def mode_log(data=None):
    return os.path.join(base_dir(data), "mode.jsonl")


def stop_file(data=None):
    """Emergency stop: while this file exists the runner starts no new step (running steps finish)."""
    return os.path.join(base_dir(data), "OFF")


def cache_file(data=None):
    return os.path.join(base_dir(data), "green-cache.json")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---------------------------------------------------------------- mode switch (off | probe | on)

def _env_mode():
    m = os.environ.get("AIWERKSTATT_REVIEW_MODE", "off").strip().lower()
    return m if m in MODES else "off"


def load_mode(data=None):
    """Current setting. A missing or broken file means the environment default (``off`` unless set)."""
    d = read_json(mode_file(data))
    out = {"format": FORMAT_MODE, "mode": _env_mode(), "probe_max": PROBE_MAX_DEFAULT, "since": None, "by": None,
           "source": "environment"}
    if isinstance(d, dict) and d.get("mode") in MODES:
        out.update(mode=d["mode"], since=d.get("since"), by=d.get("by"), source="file")
        try:
            out["probe_max"] = max(1, min(20, int(d.get("probe_max") or PROBE_MAX_DEFAULT)))
        except (TypeError, ValueError):
            pass
    out["stopped"] = os.path.exists(stop_file(data))
    return out


def set_mode(mode, by, probe_max=None, reason="", data=None, now=None):
    """Write the switch (only the API calls this) and log one JSON line. -> (setting, error)."""
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        return None, "mode must be one of: %s" % ", ".join(MODES)
    cur = load_mode(data)
    pm = cur["probe_max"]
    if probe_max not in (None, ""):
        try:
            pm = int(probe_max)
        except (TypeError, ValueError):
            return None, "probe_max must be a number"
        if not 1 <= pm <= 20:
            return None, "probe_max must be between 1 and 20"
    now = int(now or time.time())
    rec = {"format": FORMAT_MODE, "mode": mode, "probe_max": pm, "since": now, "by": str(by or "")[:80]}
    write_json(mode_file(data), rec)
    os.makedirs(base_dir(data), exist_ok=True)
    with open(mode_log(data), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now, "by": rec["by"], "from": cur["mode"], "to": mode, "probe_max": pm,
                            "reason": str(reason or "")[:300]}, ensure_ascii=False) + "\n")
    return load_mode(data), None


# ---------------------------------------------------------------- targets (limits per target)

def _target_ok(name, t):
    if not TARGET_RX.match(str(name)) or not isinstance(t, dict):
        return False
    try:
        lim = int(t.get("limit", 1))
    except (TypeError, ValueError):
        return False
    if not 1 <= lim <= 16 or t.get("network", "none") not in ("none", "bridge"):
        return False
    if t.get("cpus") not in (None, ""):
        try:
            if not 0.1 <= float(t["cpus"]) <= 64:
                return False
        except (TypeError, ValueError):
            return False
    if t.get("memory") not in (None, "") and not MEMORY_RX.match(str(t["memory"]).lower()):
        return False
    if t.get("image") not in (None, "") and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$", str(t["image"])):
        return False
    return True


def load_targets(raw=None):
    """Targets and their limits. ``AIWERKSTATT_REVIEW_TARGETS`` (JSON object) replaces the defaults as a whole;
    an invalid value keeps the defaults and is reported. -> (targets, error or None)."""
    raw = os.environ.get("AIWERKSTATT_REVIEW_TARGETS", "") if raw is None else raw
    if not str(raw).strip():
        return {k: dict(v) for k, v in DEFAULT_TARGETS.items()}, None
    try:
        d = json.loads(raw)
    except ValueError:
        return {k: dict(v) for k, v in DEFAULT_TARGETS.items()}, "AIWERKSTATT_REVIEW_TARGETS is not valid JSON"
    if not isinstance(d, dict) or not d or len(d) > 16:
        return {k: dict(v) for k, v in DEFAULT_TARGETS.items()}, "AIWERKSTATT_REVIEW_TARGETS must be an object with 1-16 targets"
    out = {}
    for name, t in d.items():
        if not _target_ok(name, t):
            return {k: dict(v) for k, v in DEFAULT_TARGETS.items()}, "invalid target %r in AIWERKSTATT_REVIEW_TARGETS" % str(name)[:40]
        out[name] = {"limit": int(t.get("limit", 1)), "network": t.get("network", "none")}
        for k in ("cpus", "memory", "image"):
            if t.get(k) not in (None, ""):
                out[name][k] = str(t[k])
    return out, None


# ---------------------------------------------------------------- jobs

def job_id(thread_id, rnd):
    return "j%d-r%d" % (int(thread_id), int(rnd))


def job_parts(jid):
    m = JOB_RX.match(str(jid or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def phase(job):
    """waiting_runner | running | waiting_review | in_review | green | rework | escalated | cancelled."""
    run = (job or {}).get("run") or {}
    finding = (job or {}).get("finding")
    if run.get("cancelled"):
        return "cancelled"
    if isinstance(finding, dict):
        if finding.get("decision") == "green":
            return "green"
        if finding.get("decision") == "rework":
            return "escalated" if run.get("escalated") else "rework"
    st = run.get("status")
    if st == "running":
        return "running"
    if st == "ran":
        return "in_review" if run.get("reviewer") else "waiting_review"
    return "waiting_runner"


def read_job(jid, data=None):
    """The combined job (hand-off + run + finding + phase); None when it does not exist."""
    if not JOB_RX.match(str(jid or "")):
        return None
    d = jobs_dir(data)
    j = read_json(os.path.join(d, jid + ".json"))
    if not isinstance(j, dict):
        return None
    j = dict(j)
    j["run"] = read_json(os.path.join(d, jid + ".run.json"), {}) or {}
    j["finding"] = read_json(os.path.join(d, jid + ".finding.json"))
    j["phase"] = phase(j)
    return j


def list_jobs(data=None):
    try:
        names = sorted(n[:-5] for n in os.listdir(jobs_dir(data)) if n.endswith(".json") and n.count(".") == 1)
    except OSError:
        return []
    out = []
    for jid in names:
        j = read_job(jid, data)
        if j:
            out.append(j)
    return out


def write_run(jid, run, data=None):
    write_json(os.path.join(jobs_dir(data), jid + ".run.json"), run)


def runner_order(jobs):
    """Rework rounds (round > 1) before first rounds, otherwise in hand-off order."""
    return sorted(jobs, key=lambda j: (0 if int(j.get("round") or 1) > 1 else 1, int(j.get("handed_over") or 0), j.get("id")))


def _clean_checks(checks, targets):
    if not isinstance(checks, list) or not checks:
        return None, "checks: at least one check command is required"
    if len(checks) > MAX_STEPS:
        return None, "checks: at most %d steps" % MAX_STEPS
    out = []
    for i, c in enumerate(checks, 1):
        if isinstance(c, str):
            c = {"cmd": c}
        if not isinstance(c, dict):
            return None, "checks[%d]: a command string or {cmd, target}" % (i - 1)
        cmd = str(c.get("cmd") or "").strip()
        if not cmd or len(cmd) > MAX_CMD or "\x00" in cmd:
            return None, "checks[%d].cmd: 1-%d characters" % (i - 1, MAX_CMD)
        target = str(c.get("target") or "local").strip()
        if target not in targets:
            return None, "checks[%d].target: unknown target %r (known: %s)" % (i - 1, target[:32], ", ".join(sorted(targets)))
        out.append({"nr": i, "cmd": cmd, "target": target})
    return out, None


def hand_over(req, project, by, targets=None, data=None, now=None):
    """Create a review job from a hand-off request.

    ``req``: {thread_id, commit (full hash), branch?, checks: [cmd | {cmd, target}], hint?, release?}
    ``project``: the project row of the thread (dict with ``id``).
    -> (job, created, error). Same thread + same commit while a job is open or green → that job, created=False.
    A new commit (or the same one after rework/cancel) → next round; an older open job of the thread is cancelled
    as superseded so the runner never checks a stale commit.
    """
    targets = targets or load_targets()[0]
    try:
        tid = int(req.get("thread_id"))
    except (TypeError, ValueError):
        return None, False, "thread_id must be a number"
    commit = str(req.get("commit") or "").strip().lower()
    if not COMMIT_RX.match(commit):
        return None, False, "commit must be the full commit hash (git rev-parse HEAD)"
    branch = str(req.get("branch") or "").strip()
    if branch and not BRANCH_RX.match(branch):
        return None, False, "branch: letters, digits and . _ / - only"
    if not project or not SLUG_RX.match(str(project.get("id") or "")):
        return None, False, "project not found"
    checks, err = _clean_checks(req.get("checks"), targets)
    if err:
        return None, False, err
    now = int(now or time.time())
    mode = load_mode(data)
    os.makedirs(jobs_dir(data), exist_ok=True)
    with _LOCK, open(os.path.join(jobs_dir(data), ".handover.lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        all_jobs = list_jobs(data)
        mine = [j for j in all_jobs if int(j.get("thread_id") or -1) == tid]
        for j in sorted(mine, key=lambda j: -int(j.get("round") or 0)):
            if j.get("commit") == commit and j["phase"] in OPEN + ("green",):
                return j, False, None
        if mode["mode"] == "off":
            return None, False, "the review runner is off (switch it to probe or on)"
        if mode["mode"] == "probe":
            n_open = sum(1 for j in all_jobs if j["phase"] in OPEN)
            if n_open >= mode["probe_max"]:
                return None, False, "probe mode: at most %d open review jobs at a time" % mode["probe_max"]
        rnd = max([int(j.get("round") or 0) for j in mine] + [0]) + 1
        jid = job_id(tid, rnd)
        for j in mine:
            if j["phase"] in OPEN:
                run = dict(j.get("run") or {})
                run["cancelled"] = now
                run["cancel_reason"] = "superseded by %s" % jid
                write_run(j["id"], run, data)
        rec = {"format": FORMAT_JOB, "id": jid, "thread_id": tid, "round": rnd, "project": project["id"],
               "commit": commit, "branch": branch, "checks": checks, "hint": str(req.get("hint") or "")[:2000],
               "release": str(req.get("release") or "")[:200], "handed_over": now, "by": str(by or "")[:80]}
        write_json(os.path.join(jobs_dir(data), jid + ".json"), rec)
    return read_job(jid, data), True, None


def cancel(jid, by, reason="", data=None, now=None):
    """-> (job, error). Only open jobs can be cancelled; a running step is ended by the runner on its next tick."""
    j = read_job(jid, data)
    if not j:
        return None, "not found"
    if j["phase"] not in OPEN:
        return None, "job is not open (%s)" % j["phase"]
    run = dict(j.get("run") or {})
    run["cancelled"] = int(now or time.time())
    run["cancel_reason"] = ("cancelled by %s: %s" % (str(by or "")[:80], str(reason or "")[:300])).rstrip(": ")
    write_run(jid, run, data)
    return read_job(jid, data), None


# ---------------------------------------------------------------- finding format

def check_finding(data, run=None):
    """Validate a reviewer's finding. -> (clean finding, error).

    {"decision": "green" | "rework", "summary": "...", "layout": "LAYOUT: ...",
     "override_reason": "... (green although a step is red, at least 15 characters)",
     "findings": [{"file": "frontend/src/x.tsx", "line": 12, "photo": "<path or URL>",
                   "expected": "...", "measured": "...", "severity": "critical|high|medium|low"}]}
    """
    if not isinstance(data, dict):
        return None, "finding: a JSON object is expected"
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        return None, "decision must be green or rework"
    items = data.get("findings") or []
    if not isinstance(items, list):
        return None, "findings must be a list"
    if len(items) > MAX_FINDINGS:
        return None, "at most %d findings" % MAX_FINDINGS
    clean = []
    for i, b in enumerate(items):
        if not isinstance(b, dict):
            return None, "findings[%d] must be an object" % i
        sev = str(b.get("severity") or "medium").strip().lower()
        if sev not in SEVERITIES:
            return None, "findings[%d].severity must be one of: %s" % (i, ", ".join(SEVERITIES))
        if not str(b.get("expected") or "").strip():
            return None, "findings[%d].expected is missing" % i
        line = b.get("line")
        if line in (None, ""):
            line = None
        else:
            if isinstance(line, bool):
                return None, "findings[%d].line must be a number" % i
            try:
                line = int(line)
            except (TypeError, ValueError):
                return None, "findings[%d].line must be a number" % i
            if line < 1:
                return None, "findings[%d].line must be 1 or more" % i
        clean.append({"file": str(b.get("file") or "")[:300], "line": line, "photo": str(b.get("photo") or "")[:300],
                      "expected": str(b.get("expected"))[:1000], "measured": str(b.get("measured") or "")[:1000],
                      "severity": sev})
    if decision == "rework" and not clean:
        return None, "rework needs at least one finding"
    override = str(data.get("override_reason") or "").strip()[:500]
    if decision == "green" and run is not None:
        red = [s for s in (run.get("steps") or []) if s.get("status") != "green"]
        if red and len(override) < OVERRIDE_MIN:
            return None, ("green although %d step(s) are not green needs override_reason (at least %d characters)"
                          % (len(red), OVERRIDE_MIN))
    return {"format": FORMAT_FINDING, "decision": decision, "findings": clean,
            "summary": str(data.get("summary") or "")[:2000], "layout": str(data.get("layout") or "")[:400],
            "override_reason": override}, None


def record_finding(jid, data, by, store_dir=None, now=None):
    """Store the reviewer's decision for a job whose runner has finished. -> (job, error)."""
    j = read_job(jid, store_dir)
    if not j:
        return None, "not found"
    if j["phase"] not in ("waiting_review", "in_review"):
        return None, "a finding is accepted only after the runner finished (job is %s)" % j["phase"]
    f, err = check_finding(data, j.get("run") or {})
    if err:
        return None, err
    f.update(by=str(by or "")[:80], time=int(now or time.time()), job=jid, commit=j.get("commit"))
    write_json(os.path.join(jobs_dir(store_dir), jid + ".finding.json"), f)
    return read_job(jid, store_dir), None


def rework_text(job, finding):
    """Plain text of a finding for the builder (part 2 sends it back to the same build session)."""
    lines = ["Rework from review job %s (round %s)" % (job.get("id"), job.get("round")), ""]
    if finding.get("summary"):
        lines += [finding["summary"], ""]
    if finding.get("layout"):
        lines += [finding["layout"], ""]
    lines.append("Findings:")
    for i, b in enumerate(finding.get("findings") or [], 1):
        where = b.get("file") or "(no file)"
        if b.get("line"):
            where += ":%d" % b["line"]
        lines.append("%d. [%s] %s — expected: %s%s%s" % (
            i, b.get("severity"), where, b.get("expected"),
            (" · measured: " + b["measured"]) if b.get("measured") else "",
            (" · photo: " + b["photo"]) if b.get("photo") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- green cache (per project, commit, command)

def cache_days():
    try:
        return max(0, int(os.environ.get("AIWERKSTATT_REVIEW_CACHE_DAYS", "7")))
    except ValueError:
        return 7


def cache_key(job, step, target_cfg=None):
    image = (target_cfg or {}).get("image") or ""
    raw = "|".join([str(job.get("project")), str(job.get("commit")), str(step.get("cmd")), str(step.get("target")), image])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cache_load(data=None, now=None):
    c = read_json(cache_file(data), {}) or {}
    days = cache_days()
    if not days:
        return {}
    limit = (now or time.time()) - days * 86400
    return {k: v for k, v in c.items() if isinstance(v, dict) and float(v.get("time") or 0) >= limit}


def cache_store(key, jid, log, data=None, now=None):
    if not cache_days():
        return
    c = cache_load(data, now)
    c[key] = {"time": int(now or time.time()), "job": jid, "log": log}
    write_json(cache_file(data), c)


# ---------------------------------------------------------------- views

def public_job(j, tail=None):
    """The job as the API shows it (never more than the hand-off, the run and the finding)."""
    run = j.get("run") or {}
    steps = []
    for s in run.get("steps") or []:
        row = {k: s.get(k) for k in ("nr", "cmd", "target", "status", "rc", "duration_s", "from_cache", "reason", "start")}
        if tail:
            row["tail"] = (s.get("tail") or "")[-tail:]
        steps.append(row)
    return {"id": j.get("id"), "thread_id": j.get("thread_id"), "round": j.get("round"), "project": j.get("project"),
            "commit": j.get("commit"), "branch": j.get("branch"), "phase": j.get("phase"), "hint": j.get("hint"),
            "release": j.get("release"), "handed_over": j.get("handed_over"), "by": j.get("by"),
            "checks": j.get("checks") or [],
            "run": {"status": run.get("status"), "start": run.get("start"), "end": run.get("end"),
                    "green": run.get("green"), "prepared": run.get("prepared"), "steps": steps,
                    "cancel_reason": run.get("cancel_reason"), "source_error": run.get("source_error")},
            "finding": j.get("finding")}


def counts(jobs):
    out = {p: 0 for p in PHASES}
    for j in jobs:
        out[j["phase"]] = out.get(j["phase"], 0) + 1
    return out
