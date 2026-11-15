#!/usr/bin/env python3
"""Task chain for AIWerkstatt: after each finished task, file the next one.

Talks to the AIWerkstatt HTTP API with a service token of the ``chain`` profile (see
docs/security.md). Python standard library only; works with ``python3 -I``.

    chain.py next [--done <id> --result <file>]   record a result, then start what is ready
    chain.py status [--json]
    chain.py pause | resume                       soft pause: no new tasks are started
    chain.py manual-done <step|external-key|id>   confirm a human step or an external condition
    chain.py approve <id>                         allow an optional task
    chain.py check                                self-healing (run it from a timer)
    chain.py simulate                             dry run of the whole plan, no API, no state

Global options (or environment): ``--plan`` / AIWERKSTATT_CHAIN_PLAN, ``--root`` /
AIWERKSTATT_CHAIN_ROOT (state, status, results; default <data>/chain), ``--token-file`` /
AIWERKSTATT_CHAIN_TOKEN_FILE (default <root>/token, must be mode 0600), ``--url`` /
AIWERKSTATT_CHAIN_URL, ``--project`` / AIWERKSTATT_CHAIN_PROJECT, ``--prefix`` /
AIWERKSTATT_CHAIN_TITLE_PREFIX. The token is only ever read from the file, never from argv.

Files in the root: ``state.json`` (the chain), ``status.json`` (written atomically, for
dashboards), ``results/<id>.json`` (written by the agent), ``KILL`` (kill switch: nothing
is started or changed while it exists), ``PAUSE`` (soft pause).

Exit codes: 0 ok, 2 error, 3 kill switch on.
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
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
    return {"tasks": tasks, "order": list(order), "limits": limits, "external": dict(external)}


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
            "gate": obj["gate"], "manual_steps": [s.strip() for s in steps]}


# ---------- infrastructure ----------

class Clock:
    def now(self):
        return time.time()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


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
        for tid in self.plan["tasks"]:
            s["tasks"].setdefault(tid, {"state": "pending", "red": 0})
        return s

    def save(self, s):
        write_atomic(self.state_path, json.dumps(s, indent=1, sort_keys=True) + "\n")
        self.write_status(s)

    def killed(self):
        return os.path.exists(os.path.join(self.root, "KILL"))

    def paused(self):
        return os.path.exists(os.path.join(self.root, "PAUSE"))

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
            steps = list(res["manual_steps"])
            if self.plan["tasks"][tid]["release"]:
                steps.append("Release %s by hand (the chain never releases)" % tid)
            for n, text in enumerate(steps, 1):
                key = ("release:" + tid) if (n == len(steps) and self.plan["tasks"][tid]["release"]) \
                    else "%s:%d" % (tid, n)
                s["manual"][key] = {"text": text, "task": tid, "done": False}
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
        return ingested

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
            "manual_pending": [{"key": k, "text": m["text"], "task": m["task"]}
                               for k, m in sorted(s["manual"].items()) if not m["done"]],
            "external_pending": [{"key": k, "text": v} for k, v in sorted(self.plan["external"].items())
                                 if k not in s["external_done"]],
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
                    for m in st["manual_pending"]:
                        out("  manual %s: %s" % (m["key"], m["text"]))
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
