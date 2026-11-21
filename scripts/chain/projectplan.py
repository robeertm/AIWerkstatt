#!/usr/bin/env python3
"""Multi-project plans for AIWerkstatt (format ``projectplan/2``).

``chain.py`` runs ONE plan for ONE collecting project. This tool runs plans that span several projects: every
project has its own tracks, tasks may depend on tasks of other projects (``<project>/<ID>``), every task has a release
target (``none`` | ``train`` | ``own``) and every project can carry a ``history`` for a timeline. It files tasks through
the same HTTP API and the same ``chain`` service token as chain.py (see docs/chain.md, "Multi-project plans").
Python standard library only; works with ``python3 -I``.

    projectplan.py validate <file>                 check a plan, change nothing
    projectplan.py simulate <file>                 dry schedule on a fake clock (no API, no state)
    projectplan.py approve <name|file> [--by NAME] approve a draft: from now on the plan files tasks
    projectplan.py withdraw <name> [--reason T]    take the approval back (running tasks go on, nothing new starts)
    projectplan.py pause|resume <name>
    projectplan.py status [--json]                 all plans, drafts included; ``status.json`` is also written
    projectplan.py check                           self-healing: detect finished tasks, look at releases, fill up
    projectplan.py option <plan>:<project>/<ID>    allow an optional task
    projectplan.py done <plan>:<project>/<ID> [--reason T]    mark a task finished by hand
    projectplan.py retry <plan>:<project>/<ID>     allow a stopped / stuck / failed task again

Options (or environment): ``--root`` / AIWERKSTATT_CHAIN_ROOT (default <data>/chain; files live in ``<root>/projectplans``),
``--token-file``, ``--url``, ``--prefix`` / AIWERKSTATT_CHAIN_PLAN_PREFIX (default "Project plan · "), ``--excluded`` /
AIWERKSTATT_CHAIN_PLAN_EXCLUDED (comma separated project ids no plan may touch), ``--load-file`` /
AIWERKSTATT_PLAN_LOAD_FILE and ``--release-file`` / AIWERKSTATT_PLAN_RELEASE_FILE (see docs/chain.md). The token is only
ever read from a mode 0600 file, never from argv.

A plan runs ONLY after ``approve``. A draft is never started by itself. Hard ceilings (a plan can only lower them):
``total`` 4 open tasks (default 3), ``per_project`` 2 (default 1), 1 per track, ``per_day`` 60 (default 30). With
``priority: "free"`` (default) a task is only filed when nothing foreign waits in the queue and a slot is free.
Kill switches in ``<root>``: ``KILL`` (shared with chain.py) and ``projectplans/OFF``. The tool never tags or publishes.
Exit codes: 0 ok, 1 refused (plan or target), 2 error, 3 kill switch on.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("aiw_chain", os.path.join(_HERE, "chain.py"))
chain = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("aiw_chain", chain)
_spec.loader.exec_module(chain)

FORMAT = "projectplan/2"
STATUS_FORMAT = "projectplan-status/1"
DEFAULT_PREFIX = "Project plan · "
EFFORTS = ("low", "medium", "high", "xhigh", "max")
RELEASE_KINDS = ("none", "train", "own")
LIVE_KINDS = ("rollout", "none", "workstation")
LIVE_FOLLOWUP_MAX = 2                # automatic follow-ups per task, then a held state in the status (a person looks)
LIVE_LINE_RE = re.compile(r"(?im)^\W*LIVE:[ \t]*(.*)$")
LIVE_PAIR_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{0,60})\s*=\s*v?(\d+\.\d+\.\d+)")
LIMITS_DEFAULT = {"total": 3, "per_project": 1, "per_day": 30}
LIMITS_MAX = {"total": 4, "per_project": 2, "per_day": 60}
PRIORITIES = ("free", "queue")
STARTUP_S = 600                      # a freshly filed task counts as active even before the API shows it
VERBOSE_MAX = 60
LOG_MAX = 300
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
PLAN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TASK_RE = re.compile(r"^[A-Z][A-Z0-9]{0,11}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
QUESTION_RE = re.compile(r"^\s*QUESTION FOR THE COORDINATOR:", re.I)
STUCK_RE = re.compile(r"^\s*STUCK:", re.I)
RELEASE_LINE_RE = re.compile(r"RELEASE STATE:\s*waiting\s+([a-z0-9][a-z0-9._-]{0,60})\s+v?(\d+\.\d+\.\d+)", re.I)
TARGET_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,63}):([A-Za-z0-9][A-Za-z0-9_-]{0,63}/[A-Z][A-Z0-9]{0,11})$")
ACTIVE = ("running", "question")
DONE_STATES = ("done", "waiting_release", "published")
STATES = ("draft", "planned", "ready", "waiting", "held", "running", "question", "waiting_release", "published",
          "done", "stopped", "stuck", "failed")
TASK_KEYS = {"id", "track", "title", "model", "effort", "hours", "priority", "depends_on", "release", "release_fix",
             "optional", "prompt", "live"}


class Refused(Exception):
    """A plan or a target is not acceptable (exit 1)."""


def ver_key(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


def clean(s, n=200):
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", "" if s is None else str(s))[:n]


def excluded_default():
    return [x.strip().lower() for x in os.environ.get("AIWERKSTATT_CHAIN_PLAN_EXCLUDED", "").split(",") if x.strip()]


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def day_of(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


# ---------- plan ----------

def release_of(task, project):
    """Release target of a task: {"kind": none|train|own, "version": x.y.z|None, "slug": str|None, "how": str}.
    Kind and slug are inherited from the project; a task may set ``"release": "none"`` (or a version string)."""
    pr = project.get("release") or {}
    if isinstance(pr, str):
        pr = {"kind": pr}
    tr = task.get("release")
    if tr is None:
        tr = {}
    elif isinstance(tr, str):
        tr = {"kind": tr} if tr in RELEASE_KINDS else {"version": tr}
    ver = tr.get("version")
    return {"kind": tr.get("kind") or pr.get("kind") or "none",
            "version": ver if chain.VERSION_RE.match(str(ver or "")) else None,
            "slug": tr.get("slug") or pr.get("slug"), "how": pr.get("how") or ""}


def live_of(task, project, release=None):
    """Roll-out rule of a task: {"required", "kind", "instances", "how"}. Inherited from the project (field ``live``; a plain
    string is the kind), a task may override it. Required only with a release target (train/own) AND kind ``rollout``
    (the default). ``none`` = the project has no running instance; ``workstation`` = delivered on the main workstation only."""
    def norm(x):
        return {"kind": x} if isinstance(x, str) else (x if isinstance(x, dict) else {})
    pl, tl = norm(project.get("live")), norm(task.get("live"))
    kind = tl.get("kind") or pl.get("kind") or "rollout"
    inst = tl.get("instances") if isinstance(tl.get("instances"), list) else pl.get("instances")
    rel = release if release is not None else release_of(task, project)
    return {"required": rel["kind"] != "none" and kind == "rollout", "kind": kind,
            "instances": [str(i) for i in (inst or []) if isinstance(i, str)], "how": tl.get("how") or pl.get("how") or ""}


def live_valid(lf):
    if isinstance(lf, str):
        return lf in LIVE_KINDS
    if not isinstance(lf, dict):
        return False
    inst = lf.get("instances", [])
    return lf.get("kind", "rollout") in LIVE_KINDS and isinstance(inst, list) \
        and all(isinstance(i, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$", i) for i in inst) \
        and isinstance(lf.get("how", ""), str)


def live_check(answer, live, version=None):
    """Check the result line ``LIVE: <instance>=<version> ...`` -> (verdict, pairs, reason). verdict: ok | missing | incomplete |
    stale. The LAST line counts; ``version`` (the published version, if known) is the lower bound per instance."""
    lines = LIVE_LINE_RE.findall(answer or "")
    if not lines:
        return "missing", {}, "no LIVE: line"
    pairs = {n: v for n, v in LIVE_PAIR_RE.findall(lines[-1])}
    if not pairs:
        return "missing", {}, "LIVE: without <instance>=<version>"
    gone = [i for i in live.get("instances") or [] if i not in pairs]
    if gone:
        return "incomplete", pairs, "missing instance(s): " + ", ".join(gone)
    if version:
        old = [n for n, v in pairs.items() if ver_key(v) < ver_key(version)]
        if old:
            return "stale", pairs, "older than %s: %s" % (version, ", ".join(old))
    return "ok", pairs, ""


def repo_of(project):
    r = project.get("release")
    return r.get("repo") if isinstance(r, dict) else None


def key_of(project, tid):
    return "%s/%s" % (project, tid)


def validate_plan(plan, excluded=None):
    """Check a ``projectplan/2`` plan. Returns a list of errors (empty = fine). Never raises."""
    excl = [x.lower() for x in (excluded_default() if excluded is None else excluded)]
    err = []
    if not isinstance(plan, dict):
        return ["the plan is not an object"]
    if plan.get("format") != FORMAT:
        err.append("format must be %s" % FORMAT)
    if not PLAN_RE.match(str(plan.get("name") or "")):
        err.append("name missing or malformed ([a-z0-9-], at most 64)")
    projects = plan.get("projects")
    if not isinstance(projects, list) or not projects:
        return err + ["the plan needs a non-empty list 'projects'"]
    lim = plan.get("limits") or {}
    if not isinstance(lim, dict):
        err.append("limits is not an object")
        lim = {}
    for k, v in lim.items():
        if k == "priority":
            if v not in PRIORITIES:
                err.append("limits.priority must be %s" % "|".join(PRIORITIES))
        elif k in LIMITS_MAX:
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                err.append("limits.%s must be an integer >= 1" % k)
            elif v > LIMITS_MAX[k]:
                err.append("limits.%s = %d is above the ceiling %d" % (k, v, LIMITS_MAX[k]))
        else:
            err.append("limits.%s is unknown" % k)
    tasks, pids = {}, set()
    for p in projects:
        if not isinstance(p, dict):
            err.append("a project is not an object")
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not PROJECT_RE.match(pid):
            err.append("malformed project id %r" % (pid,))
            continue
        if pid in pids:
            err.append("project %s twice" % pid)
        pids.add(pid)
        if pid.lower() in excl:
            err.append("%s: this project is excluded from plans" % pid)
        pr = p.get("release") or {}
        if isinstance(pr, str):
            pr = {"kind": pr}
        if pr.get("kind", "none") not in RELEASE_KINDS:
            err.append("%s: release.kind must be %s" % (pid, "|".join(RELEASE_KINDS)))
        if pr.get("repo") is not None and not chain.REPO_RE.match(str(pr.get("repo"))):
            err.append("%s: release.repo is malformed" % pid)
        if p.get("live") is not None and not live_valid(p.get("live")):
            err.append("%s: live is invalid (kind %s, instances list)" % (pid, "|".join(LIVE_KINDS)))
        pl = p.get("limits") or {}
        for k, v in (pl.items() if isinstance(pl, dict) else []):
            if k in ("per_project", "per_day") and (isinstance(v, bool) or not isinstance(v, int)
                                                    or not 1 <= v <= LIMITS_MAX[k]):
                err.append("%s: limits.%s is invalid" % (pid, k))
        for v in p.get("history") or []:
            if not isinstance(v, dict) or not DATE_RE.match(str(v.get("date") or "")) or not str(v.get("title") or "").strip():
                err.append("%s: a history entry needs a date (YYYY-MM-DD) and a title" % pid)
                break
        for t in p.get("tasks") or []:
            if not isinstance(t, dict):
                err.append("%s: a task is not an object" % pid)
                continue
            tid = t.get("id")
            if not isinstance(tid, str) or not TASK_RE.match(tid):
                err.append("%s: malformed task id %r" % (pid, tid))
                continue
            k = key_of(pid, tid)
            if k in tasks:
                err.append("%s twice" % k)
            tasks[k] = (p, t)
            extra = set(t) - TASK_KEYS
            if extra:
                err.append("%s: unknown field(s) %s" % (k, ", ".join(sorted(extra))))
            for f in ("track", "title", "prompt"):
                if not isinstance(t.get(f), str) or not t[f].strip():
                    err.append("%s: field %s is missing" % (k, f))
            if isinstance(t.get("title"), str) and ("\n" in t["title"] or len(t["title"]) > 120):
                err.append("%s: title must be one line of at most 120 characters" % k)
            if isinstance(t.get("prompt"), str) and len(t["prompt"]) > 20000:
                err.append("%s: prompt is longer than 20000 characters" % k)
            if t.get("effort") not in (None, "") and t.get("effort") not in EFFORTS:
                err.append("%s: unknown effort %r" % (k, t.get("effort")))
            if t.get("model") is not None and (not isinstance(t["model"], str) or len(t["model"]) > 80):
                err.append("%s: model must be text of at most 80 characters" % k)
            try:
                if not 0 < float(t.get("hours") or 0) <= 40:
                    err.append("%s: hours missing or above 40" % k)
            except (TypeError, ValueError):
                err.append("%s: hours is invalid" % k)
            if t.get("live") is not None and not live_valid(t.get("live")):
                err.append("%s: live is invalid" % k)
            rl = t.get("release")
            if isinstance(rl, dict) and rl.get("kind", "none") not in RELEASE_KINDS:
                err.append("%s: release.kind is invalid" % k)
            if isinstance(rl, str) and rl not in RELEASE_KINDS and not chain.VERSION_RE.match(rl):
                err.append("%s: release must be %s or x.y.z" % (k, "|".join(RELEASE_KINDS)))
    deps = {}
    for k, (p, t) in tasks.items():
        out = []
        for d in t.get("depends_on") or []:
            dk = d if "/" in str(d) else key_of(p["id"], d)
            if dk not in tasks:
                err.append("%s: unknown dependency %r" % (k, d))
                continue
            if tasks[dk][1].get("optional") and not t.get("optional"):
                err.append("%s depends on the optional task %s (options must not block anything)" % (k, dk))
            out.append(dk)
        deps[k] = out
    order = plan.get("order")
    if order is not None and (not isinstance(order, list) or len(order) != len(tasks) or sorted(set(order)) != sorted(tasks)):
        err.append("order must list every task exactly once as <project>/<ID>")
    colour = {}

    def visit(n, path):
        if colour.get(n) == 1:
            err.append("cycle: %s" % " -> ".join(path + [n]))
            return
        if colour.get(n) == 2:
            return
        colour[n] = 1
        for d in deps.get(n, []):
            visit(d, path + [n])
        colour[n] = 2
    for n in deps:
        visit(n, [])
    return err


def flatten(plan):
    """({key: {project, t, release, deps}}, order) in plan order (``order`` or priority, then file order)."""
    allt = {}
    for p in plan.get("projects") or []:
        for t in p.get("tasks") or []:
            k = key_of(p["id"], t["id"])
            rel = release_of(t, p)
            allt[k] = {"project": p, "t": t, "release": rel, "live": live_of(t, p, rel),
                       "deps": [d if "/" in str(d) else key_of(p["id"], d) for d in t.get("depends_on") or []]}
    order = [k for k in (plan.get("order") or []) if k in allt]
    if not order:
        keys = list(allt)
        order = sorted(allt, key=lambda k: (int(allt[k]["t"].get("priority") or 9), keys.index(k)))
    return allt, order


def limits_of(plan):
    b = plan.get("limits") or {}
    out = {}
    for k, std in LIMITS_DEFAULT.items():
        v = b.get(k, std)
        out[k] = max(1, min(v if isinstance(v, int) and not isinstance(v, bool) else std, LIMITS_MAX[k]))
    out["priority"] = b.get("priority") if b.get("priority") in PRIORITIES else "free"
    return out


def per_project_limit(plan, project, lim):
    v = (project.get("limits") or {}).get("per_project")
    return min(lim["per_project"], v) if isinstance(v, int) and v >= 1 else lim["per_project"]


def simulate(plan, slots=5):
    """Dry schedule: in order, the first startable task (dependencies, 1 per track, per project, total). Optional tasks
    stay out. -> {end_h, tasks: {key: (start_h, end_h)}, unscheduled, sum_h, per_project, slots}"""
    allt, order = flatten(plan)
    lim = limits_of(plan)
    total = min(lim["total"], slots)
    pend = [k for k in order if not allt[k]["t"].get("optional")]
    spans, run, done, now = {}, {}, set(), 0.0
    for _ in range(10000):
        if not pend and not run:
            break
        while len(run) < total:
            pick = None
            for k in pend:
                e = allt[k]
                pid = e["project"]["id"]
                if not all(d in done for d in e["deps"]):
                    continue
                if sum(1 for j in run if allt[j]["project"]["id"] == pid) >= per_project_limit(plan, e["project"], lim):
                    continue
                if any(allt[j]["project"]["id"] == pid and allt[j]["t"]["track"] == e["t"]["track"] for j in run):
                    continue
                pick = k
                break
            if pick is None:
                break
            h = float(allt[pick]["t"]["hours"])
            run[pick] = now + h
            spans[pick] = (now, now + h)
            pend.remove(pick)
        if not run:
            break
        now = min(run.values())
        for k in [k for k, end in run.items() if end <= now]:
            done.add(k)
            del run[k]
    per = {}
    for k, e in allt.items():
        x = per.setdefault(e["project"]["id"], {"tasks": 0, "hours": 0.0})
        x["tasks"] += 1
        x["hours"] = round(x["hours"] + float(e["t"]["hours"]), 2)
    return {"end_h": round(max((s[1] for s in spans.values()), default=0.0), 2),
            "tasks": {k: (round(a, 2), round(b, 2)) for k, (a, b) in spans.items()}, "unscheduled": pend,
            "sum_h": round(sum(float(e["t"]["hours"]) for e in allt.values()), 2), "per_project": per, "slots": total}


# ---------- API ----------

class PlanApi(chain.Api):
    """The chain token's view of the API, with a project per call."""

    def create_in(self, project, title, request, model="", effort=""):
        body = {"title": title, "request": request}
        if model:
            body["model"] = model
        if effort:
            body["effort"] = effort
        return int(self._call("POST", "/api/projects/%s/threads" % project, body)["id"])


class FakeApi:
    def __init__(self):
        self.n = 0
        self.created = []

    def create_in(self, project, title, request, model="", effort=""):
        self.n += 1
        self.created.append((project, title))
        return self.n

    def get(self, thread):
        return {"status": "working", "timeline": []}

    def comment(self, thread, text):
        self.comments = getattr(self, "comments", [])
        self.comments.append((thread, text))


# ---------- the runner ----------

class ProjectPlans:
    """Everything outside is injectable (tests): api, clock, load, release_state."""

    def __init__(self, root, api, prefix=DEFAULT_PREFIX, excluded=None, clock=None, load=None, release_state=None,
                 out=print):
        self.root, self.api, self.prefix = root, api, prefix
        self.base = os.path.join(root, "projectplans")
        self.excluded = [x.lower() for x in (excluded_default() if excluded is None else excluded)]
        self.clock = clock or chain.Clock()
        self._load, self._release_state = load, release_state
        self.out = out
        self.s = None
        self.halt = {}

    # state
    def path(self, *parts):
        return os.path.join(self.base, *parts)

    def now(self):
        return self.clock.now()

    def load_state(self):
        s = read_json(self.path("state.json"))
        s = s if isinstance(s, dict) else {}
        for k in ("plans", "tasks"):
            if not isinstance(s.get(k), dict):
                s[k] = {}
        for k in ("created", "log"):
            if not isinstance(s.get(k), list):
                s[k] = []
        self.s = s
        return s

    def save_state(self):
        self.s["log"] = self.s["log"][-LOG_MAX:]
        self.s["created"] = [c for c in self.s["created"] if self.now() - c.get("ts", 0) < 14 * 86400]
        chain.write_atomic(self.path("state.json"), json.dumps(self.s, indent=1, sort_keys=True) + "\n")

    def note(self, event, **fields):
        e = {"time": chain.iso(self.now()), "event": event}
        e.update(fields)
        self.s["log"].append(e)

    def lock(self):
        os.makedirs(self.base, exist_ok=True)
        f = open(self.path("lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def killed(self):
        for p in (os.path.join(self.root, "KILL"), self.path("OFF")):
            if os.path.exists(p):
                return "kill switch %s is on" % os.path.basename(p)
        return None

    def guard(self):
        why = self.killed()
        if why:
            raise chain.Killed(why + ": nothing is changed")

    def tstate(self, name, k):
        return self.s["tasks"].setdefault("%s:%s" % (name, k), {})

    # plans on disk
    def load_plans(self, folder):
        out = {}
        try:
            names = sorted(n for n in os.listdir(self.path(folder)) if n.endswith(".json"))
        except OSError:
            return out
        for n in names:
            try:
                with open(self.path(folder, n), "r", encoding="utf-8") as f:
                    raw = f.read()
                plan = json.loads(raw)
            except (OSError, ValueError):
                continue
            if validate_plan(plan, self.excluded) or plan.get("name", "") + ".json" != n:
                self.note("plan_invalid", folder=folder, file=n)
                continue
            out[plan["name"]] = (plan, hashlib.sha256(raw.encode()).hexdigest()[:16])
        return out

    def find_draft(self, name_or_file):
        x = name_or_file.strip()
        if x.endswith(".json") and os.path.isabs(x):
            path = x
        elif PLAN_RE.match(x):
            path = self.path("drafts", x + ".json")
        else:
            raise Refused("malformed plan name: %s" % clean(x, 80))
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(), path
        except OSError:
            raise Refused("draft %s not found" % clean(x, 80))

    def approve(self, name_or_file, by="operator"):
        raw, src = self.find_draft(name_or_file)
        try:
            plan = json.loads(raw)
        except ValueError as e:
            raise Refused("the plan is not JSON: %s" % e)
        err = validate_plan(plan, self.excluded)
        if err:
            raise Refused("plan rejected: " + "; ".join(err[:8]))
        name = plan["name"]
        chain.write_atomic(self.path("active", name + ".json"), json.dumps(plan, indent=1, ensure_ascii=False) + "\n")
        with open(self.path("active", name + ".json"), "r", encoding="utf-8") as f:
            sha = hashlib.sha256(f.read().encode()).hexdigest()[:16]
        ps = self.s["plans"].setdefault(name, {})
        ps.update({"approved": chain.iso(self.now()), "approved_ts": self.now(), "by": clean(by, 40), "sha": sha,
                   "source": clean(os.path.basename(src), 120), "withdrawn": None})
        ps.setdefault("paused", False)
        self.note("approved", plan=name, by=clean(by, 40), sha=sha)
        sim = simulate(plan)
        self.out("plan %s approved (%d tasks, %.0f h, dry run %.1f h with %d slots)"
                 % (name, len(flatten(plan)[0]), sim["sum_h"], sim["end_h"], sim["slots"]))
        return name

    def withdraw(self, name, reason=""):
        ps = self.s["plans"].get(name)
        if not ps or not ps.get("approved"):
            raise Refused("plan %s is not approved" % clean(name, 64))
        ps["withdrawn"] = chain.iso(self.now())
        ps["withdrawn_reason"] = clean(reason, 200) or None
        try:
            os.replace(self.path("active", name + ".json"), self.path("active", name + ".json.withdrawn"))
        except OSError:
            pass
        self.note("withdrawn", plan=name, reason=ps["withdrawn_reason"])
        self.out("approval of %s withdrawn: running tasks go on, nothing new starts" % name)

    # outside world
    def load_info(self):
        """{"waiting": [{"thread": n, "urgent": bool}], "slots_free": n} or None when unknown."""
        d = self._load() if self._load else None
        return d if isinstance(d, dict) else None

    def repo_state(self, repo, slug):
        d = self._release_state() if callable(self._release_state) else self._release_state
        for r in (d or {}).get("repos") or []:
            if (slug and r.get("slug") == slug) or (repo and str(r.get("repo") or "").lower() == str(repo).lower()):
                return r
        return None

    def thread_info(self, nr):
        """{"active", "finished", "question", "stopped", "failed", "answer"} from the API view of one task."""
        out = {"active": False, "finished": False, "question": False, "stopped": False, "failed": False, "answer": ""}
        try:
            th = self.api.get(int(nr))
        except chain.ChainError:
            out["active"] = True        # cannot tell: wait rather than file twice
            return out
        st = th.get("status")
        replies = [e for e in th.get("timeline", []) if e.get("type") == "reply"]
        out["answer"] = (replies[-1].get("text") if replies else "") or ""
        if st in ("working", "queued", "waiting", "retrying"):
            out["active"] = True
        elif st == "stopped":
            out["stopped"] = True
        elif st == "failed":
            out["failed"] = True
        elif st in ("done", "review"):
            out["finished"] = True
        else:
            out["active"] = True
        return out

    # task states
    def state_of(self, name, k, e, approved):
        ts = self.s["tasks"].get("%s:%s" % (name, k)) or {}
        if ts.get("state"):
            return ts["state"]
        if not approved:
            return "draft"
        if e["t"].get("optional") and not ts.get("option_on"):
            return "held"
        deps = [(self.s["tasks"].get("%s:%s" % (name, d)) or {}).get("state") for d in e["deps"]]
        return "ready" if all(x in DONE_STATES for x in deps) else "waiting"

    def open_tasks(self):
        return [(key.split(":", 1)[0], key.split(":", 1)[1], ts) for key, ts in self.s["tasks"].items()
                if ts.get("state") in ACTIVE]

    def created_today(self, project=None):
        today = day_of(self.now())
        return sum(1 for c in self.s["created"] if day_of(c.get("ts", 0)) == today
                   and (project is None or c.get("project") == project))

    def inspect(self, name, k, e):
        """Look at a filed task and advance its state."""
        ts = self.tstate(name, k)
        st = ts.get("state")
        if st in ("waiting_release", "done", "published"):
            if st == "waiting_release":
                self.release_look(ts, e)
            self.live_follow(name, k, e, ts)
            return
        if st not in ACTIVE:
            return
        info = self.thread_info(ts["thread"])
        if info["stopped"]:
            ts.update(state="stopped", end_ts=self.now())
            self.note("stopped", task=k, thread=ts["thread"])
            return
        if info["failed"]:
            ts.update(state="failed", end_ts=self.now())
            self.note("failed", task=k, thread=ts["thread"])
            return
        recent = self.now() - float(ts.get("since_ts") or 0) < STARTUP_S
        if info["active"] or (recent and not info["answer"]):
            if st == "question" and info["active"]:
                ts["state"] = "running"         # a follow-up answered the question
            return
        answer = info["answer"]
        if not answer.strip():
            return
        if QUESTION_RE.match(answer):
            if st != "question":
                ts.update(state="question", question_ts=self.now())
                self.note("question", task=k, thread=ts["thread"])
            return
        if STUCK_RE.match(answer):
            ts.update(state="stuck", end_ts=self.now())
            self.note("stuck", task=k, thread=ts["thread"])
            return
        ts.update(state="done", end_ts=self.now())
        m = RELEASE_LINE_RE.search(answer)
        if m:
            ts["release_slug"], ts["release_version"] = m.group(1).lower(), m.group(2)
        if m:
            ts["release_marker"] = True          # built, not published: LIVE only after publication
        if e["release"]["kind"] != "none" or m:
            ts["state"] = "waiting_release"
            self.release_look(ts, e)
        self.note("finished", task=k, thread=ts["thread"], state=ts["state"], version=ts.get("release_version"))
        self.live_follow(name, k, e, ts)

    def live_follow(self, name, k, e, ts):
        """Roll out + measure: check the ``LIVE:`` result line of a task. Only for tasks filed with the requirement
        (``live_required``), older state is left alone. If the line is missing, an expected instance is missing or a version is
        older than the published one, a follow-up goes to the task (at most LIVE_FOLLOWUP_MAX, every new answer is checked
        again), then the task stays ``live: held`` in the status."""
        if not ts.get("live_required") or not ts.get("thread"):
            return
        lz = ts.setdefault("live", {"state": "open", "followups": 0})
        if lz.get("state") in ("ok", "held"):
            return
        if ts.get("state") == "waiting_release" and ts.get("release_marker"):
            return                               # the release is still pending: check after publication
        ver = ts.get("published") or ts.get("release_version") or e["release"].get("version")
        info = self.thread_info(ts["thread"])
        if info["active"]:
            return
        answer = info["answer"]
        if not answer.strip():
            return
        h = hashlib.sha1(answer.encode("utf-8", "replace")).hexdigest()[:12]
        if lz.get("state") == "followup" and lz.get("answer_hash") == h:
            return                               # no new answer to the follow-up yet
        verdict, pairs, why = live_check(answer, e["live"], ver)
        lz.update({"checked": chain.iso(self.now()), "pairs": pairs, "reason": why or None, "answer_hash": h, "version": ver})
        if verdict == "ok":
            lz["state"] = "ok" if (ver or ts.get("state") == "published") else "ok_provisional"
            self.note("live_ok", task=k, thread=ts["thread"], pairs=pairs)
            return
        n = int(lz.get("followups") or 0)
        if n >= LIVE_FOLLOWUP_MAX:
            lz["state"] = "held"
            self.note("live_held", task=k, thread=ts["thread"], reason=why)
            return
        text = ("Project plan (watcher): your result has no valid roll-out measurement (%s). Done means published AND live. Roll%s "
                "out to ALL running instances (how: %s; never wait for an update timer), measure version and health per "
                "instance and put a line `LIVE: <instance>=<version> ...` into your result.%s If the project really has no "
                "running instance, start the answer with `QUESTION FOR THE COORDINATOR:` and say why."
                % (why, (" version %s" % ver) if ver else "", e["live"]["how"] or "the project's notes",
                   (" Expected instances: %s." % ", ".join(e["live"]["instances"])) if e["live"]["instances"] else ""))
        try:
            self.api.comment(int(ts["thread"]), text)
        except chain.ChainError as ex:
            self.note("live_followup_error", task=k, error=clean(str(ex), 200))
            return
        self.note("live_followup", task=k, thread=ts["thread"], reason=why)
        lz.update(followups=n + 1, state="followup")

    def release_look(self, ts, e):
        """waiting_release -> published once the release state shows the version (or any release after the task end)."""
        r = self.repo_state(repo_of(e["project"]),
                            ts.get("release_slug") or e["release"].get("slug"))
        if not r:
            return
        ver = ts.get("release_version") or e["release"].get("version")
        pub = r.get("published")
        if ver:
            if pub and ver_key(pub) >= ver_key(ver):
                ts.update(state="published", published=pub)
            return
        if not r.get("unpublished") or float(r.get("published_ts") or 0) >= float(ts.get("end_ts") or 0):
            ts.update(state="published", published=pub)

    # brakes
    def release_free(self, name, k, e):
        if e["t"].get("release_fix"):
            return True, ""
        slug = e["release"].get("slug")
        repo = repo_of(e["project"])
        r = self.repo_state(repo, slug)
        if r:
            if r.get("level") in ("stale", "critical"):
                return False, "release backlog %s (%s)" % (r.get("slug") or slug, r.get("level"))
            if (r.get("train") or {}).get("status") == "red":
                return False, "release train %s is red" % (r.get("slug") or slug)
        if e["release"]["kind"] == "own":
            if r and (r.get("train") or {}).get("status") in ("running", "green", "requested"):
                return False, "release train %s is running" % (r.get("slug") or slug)
            for pn, kk, ts in self.open_tasks():
                if (pn, kk) != (name, k) and repo and ts.get("repo") == repo and ts.get("release_kind") == "own":
                    return False, "another own release for %s is open" % repo
        return True, ""

    def slot_free(self, own_threads, new_now):
        info = self.load_info()
        if info is None:
            return False, "queue state unknown"
        waiting = info.get("waiting") or []
        foreign = [w for w in waiting if w.get("thread") not in own_threads]
        if any(w.get("urgent") for w in foreign):
            return False, "an urgent task is waiting"
        if foreign:
            return False, "%d foreign tasks are waiting (priority)" % len(foreign)
        own_waiting = sum(1 for w in waiting if w.get("thread") in own_threads)
        if int(info.get("slots_free") or 0) - own_waiting - new_now <= 0:
            return False, "no active slot is free"
        return True, ""

    # filing
    def request_for(self, name, plan, sha, k, e):
        p, t, rel = e["project"], e["t"], e["release"]
        prior = []
        for d in e["deps"]:
            ds = self.s["tasks"].get("%s:%s" % (name, d)) or {}
            prior.append("- %s: task #%s, state %s" % (d, ds.get("thread", "?"), ds.get("state", "?")))
        if rel["kind"] == "train":
            rt = ("Release target **train**: test the work, bring it to main by fast-forward, write the changelog "
                  "entry and version as the repository requires. If a release train or gate already runs for this "
                  "repository, start no second gate. If the result is built but not published yet, write the line "
                  "`RELEASE STATE: waiting %s <version>`.%s" % (rel.get("slug") or "<slug>",
                                                                (" Way: " + rel["how"]) if rel.get("how") else ""))
        elif rel["kind"] == "own":
            rt = ("Release target **own**: after a green gate publish through the project's own way (%s). Never tag "
                  "yourself, never publish an unchecked version. If it hangs, write `RELEASE STATE: waiting %s <version>`."
                  % (rel.get("how") or "the project's release process", rel.get("slug") or "<slug>"))
        else:
            rt = "Release target **none**: no release in this task (analysis, check, host file)."
        if rel["kind"] != "none":
            rt += "\n- " + self.live_text(e)
        return (
            "%s\n\n## Project plan context (automatic)\n"
            "- Plan **%s** (date %s, SHA %s), task **%s** · project %s · track %s · priority %s · about %s h.\n"
            "- Depends on:\n%s\n- %s\n\n"
            "## Plan rules\n"
            "1. Read the project's notes first, then act; check against the main branch and the latest releases whether "
            "the point is already done, and if so only prove it and report.\n"
            "2. Do not tag, release or publish unless the release target above says so.\n"
            "3. Never write to real devices unless this task says so explicitly.\n"
            "4. Result: first line one sentence, then the evidence; name branch and commit and push the branch. If you have "
            "to ask, make the first line `QUESTION FOR THE COORDINATOR:` (the plan then waits). If you hang on a background "
            "job, make it `STUCK:`. The plan notices the end by itself, no result file is needed.\n"
            % (t["prompt"].strip(), name, plan.get("date") or "?", sha, k, p.get("name") or p["id"], t["track"],
               t.get("priority", "-"), t.get("hours"), "\n".join(prior) or "- nothing", rt))

    @staticmethod
    def live_text(e):
        """Mandatory steps "roll out + measure" for the task text (rule ``live`` of the project/task)."""
        lv = e["live"]
        if lv["kind"] == "none":
            return "**Roll-out: not needed** - the project has no running instance (publishing is delivery). No LIVE line needed."
        if lv["kind"] == "workstation":
            return ("**Roll-out: main workstation only** (never through the chain). No LIVE line needed; report workstation steps "
                    "as a question for the coordinator.")
        insts = (" Instances: %s." % ", ".join(lv["instances"])) if lv["instances"] else ""
        return ("**Roll out + measure (mandatory; done means published AND live):** after the tag/release (published, not just "
                "built) roll out to ALL running instances - how: %s - and measure version and health per instance (never "
                "just trust 'built').%s Result: one line of its own `LIVE: <instance>=<measured version> <instance>=<version> "
                "...` (measured values only). If the release is still pending (`RELEASE STATE: waiting ...`), write the line after "
                "publication - the plan asks for it in a follow-up; a missing line or an older version also gets a follow-up."
                % (lv["how"] or "from the project's notes (the project's own roll-out command)", insts))

    def file_task(self, name, plan, sha, k, e):
        p, t = e["project"], e["t"]
        title = ("%s%s %s: %s" % (self.prefix, p["id"], t["id"], t["title"]))[:200]
        try:
            thread = self.api.create_in(p["id"], title, self.request_for(name, plan, sha, k, e),
                                        t.get("model") or "", t.get("effort") or "")
        except chain.ChainError as ex:
            self.note("file_error", task=k, error=clean(str(ex), 200))
            return False
        ts = self.tstate(name, k)
        rel = e["release"]
        repo = repo_of(p)
        ts.update({"state": "running", "thread": thread, "since": chain.iso(self.now()), "since_ts": self.now(),
                   "hours": float(t["hours"]), "project": p["id"], "repo": repo, "release_kind": rel["kind"],
                   "live_required": bool(e["live"]["required"])})
        self.s["created"].append({"plan": name, "task": k, "project": p["id"], "thread": thread, "ts": self.now()})
        self.note("filed", plan=name, task=k, thread=thread)
        self.out("filed %s as task #%s" % (k, thread))
        return True

    def fill(self, active=None):
        """File every ready task of every approved plan, as far as switches, priority and brakes allow.
        Returns the filed keys; ``self.halt`` holds the reason per task or plan."""
        self.halt = {}
        active = active if active is not None else self.load_plans("active")
        why = self.killed()
        if why:
            self.halt["*"] = why
            return []
        new = []
        own = {ts.get("thread") for _p, _k, ts in self.open_tasks() if ts.get("thread")}
        for name, (plan, sha) in active.items():
            ps = self.s["plans"].get(name) or {}
            if not ps.get("approved") or ps.get("withdrawn"):
                continue
            if ps.get("paused"):
                self.halt[name] = "paused"
                continue
            lim = limits_of(plan)
            allt, order = flatten(plan)
            for k in order:
                e = allt[k]
                if self.state_of(name, k, e, True) != "ready":
                    continue
                pid = e["project"]["id"]
                if pid.lower() in self.excluded:
                    self.halt[k] = "project is excluded"
                    continue
                opn = self.open_tasks()
                if len(opn) >= lim["total"]:
                    self.halt[name] = "total brake %d reached" % lim["total"]
                    break
                pp = per_project_limit(plan, e["project"], lim)
                if sum(1 for _n, _k, ts in opn if ts.get("project") == pid) >= pp:
                    self.halt[k] = "project brake (%d)" % pp
                    continue
                if any(ts.get("project") == pid and allt.get(kk, {}).get("t", {}).get("track") == e["t"]["track"]
                       for pn, kk, ts in opn if pn == name):
                    self.halt[k] = "previous task of track %s is running" % e["t"]["track"]
                    continue
                if self.created_today() >= lim["per_day"]:
                    self.halt[name] = "daily brake %d reached" % lim["per_day"]
                    break
                pd = (e["project"].get("limits") or {}).get("per_day")
                if pd and self.created_today(pid) >= int(pd):
                    self.halt[k] = "daily brake of the project (%d)" % int(pd)
                    continue
                ok, why = self.release_free(name, k, e)
                if not ok:
                    self.halt[k] = why
                    continue
                if lim["priority"] == "free":
                    ok, why = self.slot_free(own, len(new))
                    if not ok:
                        self.halt[name] = "priority: " + why
                        break
                if self.file_task(name, plan, sha, k, e):
                    new.append(k)
                else:
                    self.halt[name] = "the API refused or is unreachable"
                    break
        return new

    def check(self):
        self.guard()
        active = self.load_plans("active")
        for name, (plan, _sha) in active.items():
            allt, _order = flatten(plan)
            for k, e in allt.items():
                self.inspect(name, k, e)
        return self.fill(active)

    # hand controls
    @staticmethod
    def target(text):
        m = TARGET_RE.match(text.strip())
        if not m:
            raise Refused("give the target as <plan>:<project>/<ID>")
        return m.group(1), m.group(2)

    def mark_done(self, target, reason=""):
        name, k = self.target(target)
        self.tstate(name, k).update(state="done", end_ts=self.now(), by_hand=clean(reason, 200) or "by hand")
        self.note("done_by_hand", plan=name, task=k, reason=clean(reason, 200))

    def retry(self, target):
        name, k = self.target(target)
        key = "%s:%s" % (name, k)
        ts = self.s["tasks"].get(key) or {}
        if ts.get("state") not in ("stopped", "stuck", "failed", "question"):
            raise Refused("%s is not stopped, stuck or failed (state %s)" % (key, ts.get("state")))
        self.s["tasks"][key] = {"before": {"thread": ts.get("thread"), "state": ts.get("state")}}
        self.note("retry", plan=name, task=k)

    def option(self, target):
        name, k = self.target(target)
        self.tstate(name, k)["option_on"] = chain.iso(self.now())
        self.note("option_on", plan=name, task=k)

    # status
    def status(self):
        active = self.load_plans("active")
        drafts = self.load_plans("drafts")
        plans = []
        for name in sorted(set(active) | set(drafts)):
            ps = self.s["plans"].get(name) or {}
            approved = name in active and bool(ps.get("approved")) and not ps.get("withdrawn")
            plan, sha = active[name] if name in active else drafts[name]
            allt, order = flatten(plan)
            rows = []
            for k in order:
                e = allt[k]
                ts = self.s["tasks"].get("%s:%s" % (name, k)) or {}
                st = self.state_of(name, k, e, approved)
                rows.append({
                    "key": k, "project": e["project"]["id"], "id": e["t"]["id"], "track": e["t"]["track"],
                    "title": clean(e["t"]["title"], 160), "hours": float(e["t"]["hours"]), "priority": e["t"].get("priority"),
                    "depends_on": e["deps"], "release": {"kind": e["release"]["kind"], "version": e["release"]["version"],
                                                          "slug": e["release"]["slug"]},
                    "optional": bool(e["t"].get("optional")), "state": st, "thread": ts.get("thread"),
                    "start": ts.get("since_ts"), "end": ts.get("end_ts"),
                    "release_version": ts.get("release_version") or ts.get("published"),
                    "live": self.live_row(e, ts, st),
                    "halt": clean(self.halt.get(k) or (self.halt.get(name) if st == "ready" else None) or "", 120) or None})
            projects = [{"id": p["id"], "name": clean(p.get("name") or p["id"], 80),
                         "history": [{"date": str(v.get("date"))[:10], "title": clean(v.get("title"), 160),
                                      "result": clean(v.get("result"), 200) or None,
                                      "version": v.get("version") if chain.VERSION_RE.match(str(v.get("version") or "")) else None}
                                     for v in (p.get("history") or []) if isinstance(v, dict)][:VERBOSE_MAX]}
                        for p in plan.get("projects") or []]
            not_live = [r["key"] for r in rows if (r.get("live") or {}).get("not_live")]
            sim = simulate(plan)
            plans.append({"name": name, "title": clean(plan.get("title") or name, 160), "date": plan.get("date"),
                          "sha": ps.get("sha") or sha, "approved": approved, "by": ps.get("by"),
                          "paused": bool(ps.get("paused")), "withdrawn": ps.get("withdrawn"),
                          "limits": limits_of(plan), "sum_h": sim["sum_h"], "dry_run_end_h": sim["end_h"],
                          "projects": projects, "tasks": rows, "not_live": not_live,
                          "counts": {x: sum(1 for r in rows if r["state"] == x) for x in STATES
                                     if any(r["state"] == x for r in rows)}})
        all_nl = ["%s:%s" % (pl["name"], k) for pl in plans for k in pl["not_live"]]
        return {"format": STATUS_FORMAT, "time": int(self.now()), "killed": self.killed(),
                "live": {"not_live": len(all_nl), "tasks": all_nl[:20]},
                "halt": clean(self.halt.get("*") or "", 160) or None, "open": len(self.open_tasks()), "plans": plans}

    @staticmethod
    def live_row(e, ts, st):
        """Field ``live`` per task in the status (additive): required, state (n/a|open|followup|ok|ok_provisional|held), followups,
        not_live (= finished with a release but without a confirmed live version)."""
        if not ts.get("live_required"):
            return {"required": bool(e["live"]["required"]), "state": "n/a", "not_live": False}
        lz = ts.get("live") or {}
        s_ = lz.get("state") or "open"
        published = st in ("published", "done") or (st == "waiting_release" and not ts.get("release_marker"))
        return {"required": True, "state": s_, "followups": int(lz.get("followups") or 0),
                "not_live": bool(published and s_ not in ("ok", "ok_provisional")), "reason": clean(lz.get("reason") or "", 120) or None}

    def write_status(self):
        st = self.status()
        chain.write_atomic(self.path("status.json"), json.dumps(st, indent=1, sort_keys=True) + "\n")
        return st


def status_text(st):
    lines = ["project plans · open %d%s%s" % (st["open"], " · KILLED: " + st["killed"] if st.get("killed") else "",
                                               " · held: " + st["halt"] if st.get("halt") else "")]
    for p in st["plans"]:
        lines.append("")
        lines.append("%s — %s · %d tasks · %.0f h · dry run %.1f h%s" % (
            p["name"], "approved" if p["approved"] else "DRAFT (not approved)", len(p["tasks"]), p["sum_h"],
            p["dry_run_end_h"], " · paused" if p["paused"] else ""))
        lines.append("   " + ", ".join("%s %d" % kv for kv in sorted(p["counts"].items())))
        for r in p["tasks"]:
            if r["state"] in ("running", "question", "waiting_release", "stuck", "stopped", "failed", "ready"):
                lines.append("   %-28s %-16s %s%s" % (r["key"], r["state"], "#%s " % r["thread"] if r.get("thread") else "",
                                                      "(%s)" % r["halt"] if r.get("halt") else ""))
    if not st["plans"]:
        lines.append("no plans (drafts go to <root>/projectplans/drafts/, approve with: projectplan.py approve <name>)")
    return "\n".join(lines)


# ---------- command line ----------

def _default_root():
    data = os.environ.get("AIWERKSTATT_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(data, "chain")


def build_parser():
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="projectplan.py", description="AIWerkstatt multi-project plans.")
    ap.add_argument("--root", default=env("AIWERKSTATT_CHAIN_ROOT") or _default_root())
    ap.add_argument("--token-file", default=env("AIWERKSTATT_CHAIN_TOKEN_FILE"))
    ap.add_argument("--url", default=env("AIWERKSTATT_CHAIN_URL") or "http://127.0.0.1:%s" % env("AIWERKSTATT_PORT", "8095"))
    ap.add_argument("--prefix", default=env("AIWERKSTATT_CHAIN_PLAN_PREFIX", DEFAULT_PREFIX))
    ap.add_argument("--excluded", default=env("AIWERKSTATT_CHAIN_PLAN_EXCLUDED", ""))
    ap.add_argument("--load-file", default=env("AIWERKSTATT_PLAN_LOAD_FILE"))
    ap.add_argument("--release-file", default=env("AIWERKSTATT_PLAN_RELEASE_FILE"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").add_argument("file")
    sub.add_parser("simulate").add_argument("file")
    a = sub.add_parser("approve")
    a.add_argument("plan")
    a.add_argument("--by", default=env("USER") or "operator")
    w = sub.add_parser("withdraw")
    w.add_argument("plan")
    w.add_argument("--reason", default="")
    for c in ("pause", "resume"):
        sub.add_parser(c).add_argument("plan")
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    sub.add_parser("check")
    sub.add_parser("option").add_argument("target")
    d = sub.add_parser("done")
    d.add_argument("target")
    d.add_argument("--reason", default="")
    sub.add_parser("retry").add_argument("target")
    return ap


def main(argv=None, out=print, api=None, clock=None, load=None, release_state=None):
    a = build_parser().parse_args(argv)
    excluded = [x.strip().lower() for x in a.excluded.split(",") if x.strip()]
    try:
        if a.cmd in ("validate", "simulate"):
            with open(a.file, "r", encoding="utf-8") as f:
                plan = json.load(f)
            err = validate_plan(plan, excluded)
            if err:
                out("%d error(s):\n  - %s" % (len(err), "\n  - ".join(err)))
                return 1
            sim = simulate(plan)
            if a.cmd == "simulate":
                out(json.dumps(sim, indent=1, sort_keys=True))
            else:
                out("plan %s is valid: %d projects, %d tasks, %.1f h, dry run %.1f h with %d slots" % (
                    plan["name"], len(plan["projects"]), len(flatten(plan)[0]), sim["sum_h"], sim["end_h"], sim["slots"]))
            return 0
        if api is None:
            api = PlanApi(a.url, a.token_file or os.path.join(a.root, "token"), "", a.prefix)
        if load is None and a.load_file:
            load = lambda: read_json(a.load_file)                                   # noqa: E731
        if release_state is None and a.release_file:
            release_state = lambda: read_json(a.release_file)                       # noqa: E731
        pp = ProjectPlans(a.root, api, a.prefix, excluded, clock, load, release_state, out)
        lock = pp.lock()
        try:
            pp.load_state()
            if a.cmd != "status":
                pp.guard()
            if a.cmd == "approve":
                pp.approve(a.plan, a.by)
                pp.fill()
            elif a.cmd == "withdraw":
                pp.withdraw(a.plan, a.reason)
            elif a.cmd in ("pause", "resume"):
                ps = pp.s["plans"].get(a.plan)
                if not ps:
                    raise Refused("plan %s is not approved" % clean(a.plan, 64))
                ps["paused"] = a.cmd == "pause"
                pp.note(a.cmd, plan=a.plan)
                out(a.cmd + "d " + a.plan if a.cmd == "pause" else "resumed " + a.plan)
                if a.cmd == "resume":
                    pp.fill()
            elif a.cmd == "check":
                for k in pp.check():
                    out("filed %s" % k)
            elif a.cmd == "done":
                pp.mark_done(a.target, a.reason)
            elif a.cmd == "retry":
                pp.retry(a.target)
                pp.fill()
            elif a.cmd == "option":
                pp.option(a.target)
                pp.fill()
            st = pp.status()
            if a.cmd == "status":
                out(json.dumps(st, indent=1, sort_keys=True) if a.json else status_text(st))
            else:
                pp.save_state()
            chain.write_atomic(pp.path("status.json"), json.dumps(st, indent=1, sort_keys=True) + "\n")
        finally:
            lock.close()
        return 0
    except chain.Killed as e:
        print("projectplan: %s" % e, file=sys.stderr)
        return 3
    except Refused as e:
        print("projectplan: %s" % e, file=sys.stderr)
        return 1
    except chain.ChainError as e:
        print("projectplan: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
