#!/usr/bin/env python3
"""Remaining-time estimate per long run and per task: "done in ~12 min / ~2 h / ~3 days" (0.25.0).

One file, two roles:

1. Writer (test scripts such as scripts/test-runner/remote_test.py) - ``Run``:
       run = eta.Run("test-runner", "remote_test", ["queue", "test"], title="my-repo/image").start()
       run.phase("test", total=120)  ·  run.tests(done=30)  ·  run.end("ok")
   writes <dir>/<run-id>.json (format aiwerkstatt-progress/1) and, at the end, one line into <dir>/history-<user>.jsonl.
   A heartbeat thread rewrites the file every minute (with a load sample) so readers can tell a dead run. Every error is
   swallowed - a script never fails because of the progress display. Without write permission the writer is silent.

2. Reader (GET /api/pool):
       estimate(run, history, load)        -> eta_s, range, text, phase, progress, reliability
       task_eta(...)                       -> remaining time of a whole task (run + remaining steps)
       start_times(free_in, durations)     -> start time of waiting tasks (FIFO onto the slots that free up first)
       duration_text(s)                    -> "~12 min", "~2 h 10 min", "~1 day 4 h", from 48 h "~3 days"

Remaining time = rest of the current phase (test progress x measured rate, else median - elapsed) + median of the remaining
phases (repeat phases weighted by how often they ran) x load factor. Range from the 20/80 % quantiles. Without any history of
this kind: "unknown (first run of this kind)" - unless the writer passes default durations (then reliability is "rough").

The folder comes from AIWERKSTATT_PROGRESS_DIR (default ~/.local/state/aiwerkstatt/progress).
"""
import json
import os
import re
import statistics
import sys
import threading
import time

FORMAT = "aiwerkstatt-progress/1"
HEARTBEAT_S = 60
VANISHED_S = 15 * 60            # no heartbeat for 15 min -> the run counts as gone
SHOW_DONE_S = 30 * 60           # finished runs are still listed for 30 min
KEEP_S = 2 * 86400              # run files older than 2 days are removed by the reader (own write permission only)
HISTORY_LAST = 10               # moving median over the last 10 runs per phase
FACTOR_MIN, FACTOR_MAX = 0.75, 1.6
UNKNOWN = "unknown (first run of this kind)"

PHASE_TITLES = {"queue": "waiting for a test slot", "test": "test run", "build": "build", "prepare": "preparation",
                "fetch": "fetch", "checks": "checks", "ci": "CI", "release": "release", "verify": "verification"}


def progress_dir():
    return os.environ.get("AIWERKSTATT_PROGRESS_DIR") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "aiwerkstatt", "progress")


def title(name):
    return PHASE_TITLES.get(name, str(name or "").replace("_", " "))


# ------------------------------------------------------------------------------------------------ format
def _round_min(m):
    return int(round(m)) if m < 20 else int(5 * round(m / 5.0))


def duration_text(s, tilde=True):
    """Seconds -> "~12 min", "~2 h 10 min", "~1 day 4 h", from 48 h "~3 days"; None -> "unknown"."""
    if s is None:
        return "unknown"
    s = max(0.0, float(s))
    t = "~" if tilde else ""
    if s < 60:
        return "under 1 min"
    m = s / 60.0
    if m < 59.5:
        return "%s%d min" % (t, max(1, _round_min(m)))
    if s < 24 * 3600:
        gm = int(5 * round(m / 5.0))
        h, rest = divmod(gm, 60)
        if h >= 24:
            return "%s1 day" % t
        return "%s%d h%s" % (t, h, (" %d min" % rest) if rest else "")
    if s < 48 * 3600:
        h = int(round((s - 24 * 3600) / 3600.0))
        if h >= 24:
            return "%s2 days" % t
        return "%s1 day%s" % (t, (" %d h" % h) if h else "")
    return "%s%d days" % (t, int(round(s / 86400.0)))


def range_text(lo, hi):
    """"25-60 min" resp. "1 h 10 min - 2 h 30 min" (no tilde)."""
    if lo is None or hi is None:
        return ""
    if hi <= 60 * 60 + 30:
        return "%d-%d min" % (max(1, _round_min(lo / 60.0)) if lo >= 60 else 0, max(1, _round_min(hi / 60.0)))
    return "%s - %s" % (duration_text(lo, False), duration_text(hi, False))


def eta_text(eta, lo=None, hi=None, prefix="done in", unknown=UNKNOWN):
    if eta is None:
        return unknown
    if eta < 60:
        return "done in under 1 min" if prefix == "done in" else "%s under 1 min" % prefix
    sp = range_text(lo, hi) if (lo is not None and hi is not None and hi - lo >= 120) else ""
    return "%s %s%s" % (prefix, duration_text(eta), (" (%s)" % sp) if sp else "")


# ------------------------------------------------------------------------------------------------ load
def load_sample():
    """{"host": load1 per core} - optional."""
    d = {}
    try:
        d["host"] = round(os.getloadavg()[0] / float(os.cpu_count() or 4), 3)
    except (OSError, AttributeError):
        pass
    return d


def _pressure(load):
    if not isinstance(load, dict):
        return None
    w = [float(v) for v in load.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return sum(w) / len(w) if w else None


def load_factor(current, hist_loads):
    """Slower/faster than in the measured runs? (1 + pressure now) / (1 + median pressure then), clamped."""
    a = _pressure(current)
    h = [x for x in (_pressure(l) for l in hist_loads or []) if x is not None]
    if a is None or not h:
        return 1.0
    f = (1.0 + a) / (1.0 + statistics.median(h))
    return round(min(FACTOR_MAX, max(FACTOR_MIN, f)), 3)


# ------------------------------------------------------------------------------------------------ writer
def _atomic(path, d):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _origin():
    v = os.environ.get("AIWERKSTATT_PROGRESS_THREAD", "")
    return int(v) if v.isdigit() and len(v) < 9 else None


def _user():
    try:
        import pwd
        return re.sub(r"[^A-Za-z0-9_-]", "", pwd.getpwuid(os.geteuid()).pw_name) or "x"
    except Exception:  # noqa: BLE001
        return str(os.geteuid())


class Run:
    """Progress of a long run. All methods are failure-proof (exceptions are swallowed)."""

    def __init__(self, profile, kind, phases, title="", folder=None, run_id=None, thread=None, defaults=None,
                 heartbeat=True, clock=time.time):
        self.clock = clock
        self.folder = folder or progress_dir()
        now = clock()
        self.id = run_id or "%s-%s-%d" % (re.sub(r"[^a-z0-9_-]", "", str(kind).lower()) or "run",
                                          time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), os.getpid())
        self.path = os.path.join(self.folder, "%s.json" % self.id)
        self.d = {"format": FORMAT, "run": self.id, "profile": profile, "kind": kind, "title": title or kind,
                  "thread": thread if thread is not None else _origin(), "pid": os.getpid(), "start": now,
                  "stamp": now, "end": None, "result": None, "current": None,
                  "defaults": {k: int(v) for k, v in (defaults or {}).items()},
                  "phases": [{"name": n, "status": "open"} for n in phases], "load": {}, "load_n": 0}
        self.on = False
        self.heartbeat = heartbeat
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _write(self):
        if not self.on:
            return
        try:
            with self._lock:
                self.d["stamp"] = self.clock()
                _atomic(self.path, self.d)
        except Exception:  # noqa: BLE001
            pass

    def _probe(self):
        try:
            n = int(self.d.get("load_n") or 0)
            for k, v in load_sample().items():
                old = self.d["load"].get(k)
                self.d["load"][k] = round(v if old is None else (old * n + v) / (n + 1), 3)
            self.d["load_n"] = n + 1
        except Exception:  # noqa: BLE001
            pass

    def _pulse(self):
        while not self._stop.wait(HEARTBEAT_S):
            self._probe()
            self._write()

    def _ph(self, name):
        for i, ph in enumerate(self.d["phases"]):
            if ph["name"] == name:
                return i, ph
        self.d["phases"].append({"name": name, "status": "open"})
        return len(self.d["phases"]) - 1, self.d["phases"][-1]

    def start(self):
        try:
            os.makedirs(self.folder, exist_ok=True)
            self.on = os.access(self.folder, os.W_OK)
            self._probe()
            self._write()
            if self.on and self.heartbeat:
                threading.Thread(target=self._pulse, name="eta-heartbeat", daemon=True).start()
        except Exception:  # noqa: BLE001
            self.on = False
        return self

    def phase(self, name, total=None):
        """A new phase begins; the running one is done, phases skipped in between are marked as such."""
        try:
            now = self.clock()
            with self._lock:
                i, ph = self._ph(name)
                cur = self.d.get("current")
                if cur is not None and cur != i and self.d["phases"][cur].get("status") == "running":
                    self.d["phases"][cur].update(status="done", end=now)
                for j in range((cur + 1) if cur is not None else 0, i):
                    if self.d["phases"][j].get("status") == "open":
                        self.d["phases"][j]["status"] = "skipped"
                ph.update(status="running", start=now, end=None, done=None, total=total)
                self.d["current"] = i
            self._write()
        except Exception:  # noqa: BLE001
            pass
        return self

    def tests(self, done=None, total=None):
        try:
            with self._lock:
                cur = self.d.get("current")
                if cur is None:
                    return self
                ph = self.d["phases"][cur]
                if total is not None:
                    ph["total"] = int(total)
                if done is not None:
                    ph["done"] = int(done)
            self._write()
        except Exception:  # noqa: BLE001
            pass
        return self

    def expected(self, seconds):
        """The writer knows the duration of the current phase itself (e.g. queue: free in ~X s) - takes precedence."""
        try:
            with self._lock:
                cur = self.d.get("current")
                if cur is None:
                    return self
                self.d["phases"][cur]["expected_s"] = int(max(0, seconds))
                self.d["phases"][cur]["expected_at"] = self.clock()
            self._write()
        except Exception:  # noqa: BLE001
            pass
        return self

    def end(self, result="ok"):
        """Finish (ok | failed | aborted) and write the measured phase durations into the history."""
        try:
            self._stop.set()
            now = self.clock()
            with self._lock:
                cur = self.d.get("current")
                if cur is not None and self.d["phases"][cur].get("status") == "running":
                    # an aborted run's current phase is no valid measurement
                    self.d["phases"][cur].update(status="done" if result != "aborted" else "aborted", end=now)
                if result != "aborted":
                    for ph in self.d["phases"]:
                        if ph.get("status") == "open":
                            ph["status"] = "skipped"
                self.d.update(end=now, result=result, current=None)
                entry = history_entry(self.d)
            self._write()
            if self.on and entry["phases"]:
                hp = os.path.join(self.folder, "history-%s.jsonl" % _user())
                fd = os.open(hp, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                with os.fdopen(fd, "a") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                try:
                    os.chmod(hp, 0o644)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        return self


def history_entry(d):
    phases, tests, skipped = {}, {}, []
    for ph in d.get("phases") or []:
        if ph.get("status") == "done" and ph.get("start") is not None and ph.get("end") is not None:
            phases[ph["name"]] = int(max(0, ph["end"] - ph["start"]))
            if ph.get("total"):
                tests[ph["name"]] = int(ph["total"])
        elif ph.get("status") == "skipped":
            skipped.append(ph["name"])
    return {"profile": d.get("profile"), "kind": d.get("kind"), "run": d.get("run"), "end": d.get("end"),
            "result": d.get("result"), "phases": phases, "tests": tests, "skipped": skipped,
            "load": d.get("load") or {}, "total_s": int((d.get("end") or 0) - (d.get("start") or 0))}


# ------------------------------------------------------------------------------------------------ reader
def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_history(folder=None, per_file=600):
    o = folder or progress_dir()
    out = []
    try:
        names = sorted(n for n in os.listdir(o) if n.startswith("history") and n.endswith(".jsonl"))
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(o, n), encoding="utf-8") as fh:
                lines = fh.readlines()[-per_file:]
        except OSError:
            continue
        for z in lines:
            try:
                e = json.loads(z)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("phases"), dict):
                out.append(e)
    out.sort(key=lambda e: e.get("end") or 0)
    return out


def load_runs(folder=None, now=None, cleanup=False):
    """Active (and recently finished) runs. Vanished ones (no heartbeat) get state=vanished."""
    o = folder or progress_dir()
    now = now or time.time()
    out = []
    try:
        names = [n for n in os.listdir(o) if n.endswith(".json")]
    except OSError:
        return out

    def drop(path):
        try:
            os.remove(path)
        except OSError:
            pass

    for n in names:
        path = os.path.join(o, n)
        d = _read(path)
        if not isinstance(d, dict) or d.get("format") != FORMAT:
            continue
        stamp = float(d.get("stamp") or 0)
        if d.get("end"):
            if now - float(d["end"]) > SHOW_DONE_S:
                if cleanup and now - float(d["end"]) > KEEP_S:
                    drop(path)
                continue
            d["state"] = "done"
        elif now - stamp > VANISHED_S:
            if cleanup and now - stamp > KEEP_S:
                drop(path)
            if now - stamp > 6 * 3600:
                continue
            d["state"] = "vanished"
        else:
            d["state"] = "running"
        out.append(d)
    out.sort(key=lambda d: d.get("start") or 0)
    return out


def _quantile(values, q):
    w = sorted(values)
    if not w:
        return None
    if len(w) == 1:
        return float(w[0])
    pos = (len(w) - 1) * q
    a = int(pos)
    b = min(a + 1, len(w) - 1)
    return w[a] + (w[b] - w[a]) * (pos - a)


def phase_stats(hist, profile, kind, last=HISTORY_LAST):
    """({phase: {median, lo, hi, n, rate, tests}}, [load of the last runs]) from the last runs of the same kind."""
    same = [e for e in hist if e.get("profile") == profile and e.get("kind") == kind]
    names = []
    for e in same:
        for k in e["phases"]:
            if k not in names:
                names.append(k)
    st = {}
    for k in names:
        vals = [e["phases"][k] for e in same if isinstance(e["phases"].get(k), (int, float))][-last:]
        if not vals:
            continue
        # frequency only from green runs where the phase was planned (ran or skipped)
        basis = [e for e in same if e.get("result") == "ok" and (k in e["phases"] or k in (e.get("skipped") or []))][-last:]
        rate = (sum(1 for e in basis if k in e["phases"]) / float(len(basis))) if basis else 1.0
        tests = [e.get("tests", {}).get(k) for e in same if (e.get("tests") or {}).get(k)]
        st[k] = {"median": float(statistics.median(vals)), "lo": _quantile(vals, 0.2), "hi": _quantile(vals, 0.8),
                 "n": len(vals), "rate": round(max(rate, 0.0), 3), "tests": tests[-1] if tests else None}
    return st, [e.get("load") for e in same[-last:]]


def estimate(run, hist, load=None, now=None):
    """Remaining time of a run. Fields: eta_s, lo_s, hi_s, text, short, phase, phase_name, phase_index, phase_count,
    progress{done,total,share}, after[], reliability (good|rough|unknown), basis (runs), factor."""
    now = now or time.time()
    all_ph = run.get("phases") or []
    visible = [p["name"] for p in all_ph if p.get("status") not in ("skipped", "omitted")]
    cur = run.get("current")
    act = all_ph[cur] if isinstance(cur, int) and 0 <= cur < len(all_ph) else None
    out = {"run": run.get("run"), "profile": run.get("profile"), "kind": run.get("kind"), "title": run.get("title"),
           "thread": run.get("thread"), "start": run.get("start"),
           "state": run.get("state") or ("done" if run.get("end") else "running"),
           "phase": title(act["name"]) if act else None, "phase_name": act["name"] if act else None,
           "phase_index": (visible.index(act["name"]) + 1) if act and act["name"] in visible else None,
           "phase_count": len(visible), "progress": None, "after": []}
    if run.get("end"):
        out.update(eta_s=0, lo_s=0, hi_s=0, reliability="good", basis=0, factor=1.0,
                   text="done (%s)" % (run.get("result") or "?"))
        out["short"] = out["text"]
        return out
    if out["state"] == "vanished":
        out.update(eta_s=None, lo_s=None, hi_s=None, reliability="unknown", basis=0, factor=1.0,
                   text="unknown (run has not reported for over 15 min)", short="unknown")
        return out
    st, hist_load = phase_stats(hist or [], run.get("profile"), run.get("kind"))
    defaults = run.get("defaults") or {}
    f = load_factor(load if load is not None else run.get("load"), hist_load)
    eta = lo = hi = 0.0
    rough, missing, ns = False, False, []

    def stat(name):
        if name in st:
            return st[name], False
        if defaults.get(name):
            s = float(defaults[name])
            return {"median": s, "lo": 0.7 * s, "hi": 1.6 * s, "n": 0, "rate": 1.0, "tests": None}, True
        return None, False

    if act:
        el = max(0.0, now - float(act.get("start") or now))
        s, std = stat(act["name"])
        done, total = act.get("done"), act.get("total") or (s or {}).get("tests")
        share = None
        if isinstance(done, (int, float)) and total:
            share = max(0.0, min(1.0, float(done) / float(total)))
            out["progress"] = {"done": int(done), "total": int(total), "share": round(share, 3)}
        elif total:
            out["progress"] = {"done": None, "total": int(total), "share": None}
        if isinstance(act.get("expected_s"), (int, float)):
            r = max(60.0, float(act["expected_s"]) - max(0.0, now - float(act.get("expected_at") or now)))
            eta += r
            lo += 0.5 * r
            hi += 2.0 * r
            rough = True
        elif share is not None and share >= 0.05 and el > 30:
            r = el * (1.0 - share) / share               # measured rate of this run (load is already in it)
            eta += r
            lo += 0.8 * r
            hi += 1.4 * r + 60
            rough = rough or share < 0.2
        elif s:
            m, a, b = s["median"] * f, (s["lo"] or s["median"]) * f, (s["hi"] or s["median"]) * f
            eta += max(m - el, 0.1 * m)
            lo += max(a - el, 0.0)
            hi += max(b - el, 0.25 * m)
            rough = rough or std or s["n"] < 3
            ns.append(s["n"])
            if out["progress"] is None or out["progress"].get("share") is None:
                tshare = min(0.99, el / m) if m > 0 else None
                if tshare is not None:
                    out["progress"] = dict(out["progress"] or {"done": None, "total": None},
                                           share=round(tshare, 3), estimated=True)
        else:
            missing = True
    rest = []
    for ph in all_ph[(cur + 1) if isinstance(cur, int) else 0:]:
        if ph.get("status") != "open":
            continue
        s, std = stat(ph["name"])
        if not s:
            rough = True
            missing = missing or not st
            continue
        q = s.get("rate", 1.0)
        if q <= 0:
            continue
        rest.append(ph["name"])
        eta += q * s["median"] * f
        lo += (s["lo"] or s["median"]) * f if q >= 0.999 else 0.0
        hi += (s["hi"] or s["median"]) * f
        rough = rough or std or s["n"] < 3 or q < 0.999
        if not std:
            ns.append(s["n"])
    out["after"] = [title(n) for n in rest]
    if not st and not defaults:
        out.update(eta_s=None, lo_s=None, hi_s=None, reliability="unknown", basis=0, factor=f, text=UNKNOWN, short="unknown")
        return out
    out.update(eta_s=int(round(eta)), lo_s=int(round(min(lo, eta))), hi_s=int(round(max(hi, eta))),
               reliability="rough" if (rough or missing) else "good", basis=min(ns) if ns else 0, factor=f)
    out["text"] = eta_text(out["eta_s"], out["lo_s"], out["hi_s"])
    out["short"] = duration_text(out["eta_s"])
    return out


def line(est, with_title=False):
    """"done in ~25 min (test run 9/18, then release)"."""
    if not est:
        return ""
    parts = []
    if est.get("phase"):
        fs = est.get("progress") or {}
        p = est["phase"]
        if fs.get("done") is not None and fs.get("total"):
            p += " %d/%d" % (fs["done"], fs["total"])
        elif est.get("phase_index") and est.get("phase_count"):
            p += " · phase %d/%d" % (est["phase_index"], est["phase_count"])
        parts.append(p)
    if est.get("after"):
        parts.append("then " + " + ".join(est["after"]))
    head = ("%s: " % est.get("title")) if with_title and est.get("title") else ""
    return "%s%s%s" % (head, est.get("text") or "unknown", (" (%s)" % ", ".join(parts)) if parts else "")


# ------------------------------------------------------------------------------------------------ task + queue
def task_eta(runtime_s, done, total, run_est=None, experience_s=None):
    """Remaining time of the whole task: running test run + remaining steps (pace of this task) or an experience value.
    Returns {eta_s, lo_s, hi_s, source} - eta_s None = unknown."""
    rt = max(0.0, float(runtime_s or 0))
    d = int(done) if isinstance(done, (int, float)) and not isinstance(done, bool) else None
    t = int(total) if isinstance(total, (int, float)) and not isinstance(total, bool) and total else None
    rate = (rt / d) if (d and d > 0 and rt > 0) else None
    open_ = max(0, t - d) if (t is not None and d is not None) else None
    if run_est and run_est.get("eta_s") is not None:
        if open_ is not None and open_ > 1:
            after = (open_ - 1) * (min(rate, 20 * 60) if rate else 10 * 60)
        else:
            after = 5 * 60                  # reply, notes, wrap-up
        return {"eta_s": int(run_est["eta_s"] + after),
                "lo_s": int((run_est.get("lo_s") or run_est["eta_s"]) + 0.5 * after),
                "hi_s": int((run_est.get("hi_s") or run_est["eta_s"]) + 2.0 * after), "source": "run"}
    if rate and open_ is not None and open_ > 0:
        eta = rate * (open_ - 0.5)
        return {"eta_s": int(eta), "lo_s": int(0.6 * eta), "hi_s": int(1.8 * eta + 300), "source": "steps"}
    if open_ == 0 and t:
        return {"eta_s": 300, "lo_s": 60, "hi_s": 900, "source": "steps"}
    if experience_s:
        m = float(experience_s)
        eta = max(m - rt, 0.15 * m)
        return {"eta_s": int(eta), "lo_s": int(max(0.0, 0.5 * m - rt)), "hi_s": int(max(2.0 * m - rt, 0.4 * m)),
                "source": "experience"}
    return {"eta_s": None, "lo_s": None, "hi_s": None, "source": None}


def start_times(free_in, durations, fallback_s=30 * 60):
    """FIFO: free_in = seconds until free per slot (0 = free, None = unknown -> does not count), durations = expected
    duration per waiting task (None -> fallback). Returns seconds until start per waiting task (None = no slot in sight)."""
    slots = sorted(float(x) for x in free_in if x is not None)
    out = []
    for d in durations:
        if not slots:
            out.append(None)
            continue
        t = slots.pop(0)
        out.append(int(t))
        slots.append(t + float(d if d is not None else fallback_s))
        slots.sort()
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Remaining time of the active runs")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    o = a.dir or progress_dir()
    hist = load_history(o)
    res = [estimate(r, hist, load=load_sample()) for r in load_runs(o)]
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    print("\n".join(line(e, True) for e in res) or "no active runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
