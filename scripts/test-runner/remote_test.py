#!/usr/bin/env python3
"""remote_test.py - run a test suite on a second test machine and show its load (AIWerkstatt 0.22.0, queue instead of
wait lock since 0.24.0).

    remote_test.py <repo-path> --profile NAME --mode NAME [--commit REF] [-n N] [--extra <repo-path>[@REF]]
                   [--extra-path sub/dir] [--env K=V] [--pins a==1,b==2] [--junit FILE]
                   [--quiet-build] [-- test-runner-arguments ...]
    remote_test.py --status                  # state of the runner (load, CPU, running tests) as JSON
    remote_test.py --available               # exit 0 = runner reachable and set up
    remote_test.py --collect [--once]        # collector: status.json + one jsonl per day (14 days)

What happens:
  1. The commit is packed with `git archive` (tracked files only: no secrets from untracked files) and sent over a
     dedicated SSH key with a forced command to the receiver (remote_test_receiver.py on the test machine).
  2. Free slot -> start at once ("no wait locks, use a queue"). Throttling happens only on the test machine: every run gets
     a low cgroup weight (--cpu-shares) and a CPU limit (--cpus or a cpuset) from profiles.json, so production services keep
     priority; the test gets slower but never waits because of CPU, load or pressure.
     Both slots taken (the receiver never accepts more than 2) -> FIFO queue on this machine (<STATUS_DIR>/queue/, one file
     per waiting run, name = arrival time): message "queue position N, ahead of you: <run>, expected free in ~X min"
     (estimate from the own protocols). First come, first served; no deadline, no abort, no overtaking. Entries whose
     process is gone (PID + start time) clean themselves up. If the queue is not writable, the run waits for a free slot
     without a queue position (note on stderr). --wait-max-min is still accepted but has no effect.
  3. The output of the tests goes to stdout unchanged, build and info lines to stderr. The exit code is the exit code of
     the test run. If the connection drops, the run keeps going over there and the result is fetched afterwards.
  4. A protocol per run (duration, build/test time, load and CPU of the runner, unhealthy containers before/during/after)
     is written to the log folder.

Exit 100 = runner really gone or broken (not set up, unreachable at start, 3 status requests in a row unanswered while
waiting), request refused (for example an argument outside the allow list), a fault of the runner instead of the code, or
result not obtainable: the caller runs the tests locally. Never because of waiting time. Exit 2 only for wrong use of this tool.
Anything else is the exit code of the test run (pytest: 0 green, 1 red, 2 collection error, ...).

Settings (no defaults with hosts): AIWERKSTATT_TEST_RUNNER_CONFIG = JSON file {"target": "user@host", "key": "/path"}
or AIWERKSTATT_TEST_RUNNER_TARGET / AIWERKSTATT_TEST_RUNNER_KEY. Optional: AIWERKSTATT_TEST_RUNNER_LOGS,
AIWERKSTATT_TEST_RUNNER_STATUS_DIR (collector output), AIWERKSTATT_TEST_RUNNER_QUEUE (queue folder, default
<STATUS_DIR>/queue; share it between users with a group-writable folder without sticky bit), AIWERKSTATT_TEST_RUNNER_OFF=1 (switch off, exit 100).
"""
import argparse
import base64
import datetime
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

PREFIX = "AIWERKSTATT_TEST_RUNNER_"
STATE = os.path.join(os.path.expanduser("~"), ".local", "state", "aiwerkstatt", "test-runner")
CTL = "\x1eRTR "
UNAVAILABLE = 100
CONFIG = os.environ.get(PREFIX + "CONFIG", "")
LOGS = os.environ.get(PREFIX + "LOGS") or os.path.join(STATE, "logs")
WORK = os.environ.get(PREFIX + "WORK") or os.path.join(STATE, "work")
STATUS_DIR = os.environ.get(PREFIX + "STATUS_DIR") or os.path.join(STATE, "status")
WAIT_TICK_S = int(os.environ.get(PREFIX + "TICK_S") or 30)
THRESHOLD_CPU = 70.0
THRESHOLD_LOAD_SHARE = 0.75
THRESHOLD_PSI = 40.0
COLLECT_TICK_S = int(os.environ.get(PREFIX + "COLLECT_TICK_S") or 30)
COLLECT_DAYS = 14
QUEUE = os.environ.get(PREFIX + "QUEUE") or os.path.join(STATUS_DIR, "queue")
WAIT_NOTE_S = 300               # waiting message at most every 5 min (or at once when the position changes)
STATUS_FAILS_MAX = 3            # while waiting: this many status requests in a row without answer = runner gone
DEFAULT_DURATION_S = 20 * 60    # estimate without own protocols


def info(text):
    sys.stderr.write("[remote-test] %s\n" % text)
    sys.stderr.flush()


def settings():
    cfg = {}
    if CONFIG:
        try:
            with open(CONFIG) as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            pass
    target = os.environ.get(PREFIX + "TARGET") or cfg.get("target")
    key = os.environ.get(PREFIX + "KEY") or cfg.get("key")
    return target, os.path.expanduser(key) if key else None


def ssh_command(command):
    target, key = settings()
    if os.environ.get(PREFIX + "SSH"):                        # tests: a stand-in instead of ssh
        return os.environ[PREFIX + "SSH"].split() + [command]
    return ["ssh", "-T", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=10", "-o", "StrictHostKeyChecking=yes", target, command]


def unavailable_reason():
    if os.environ.get(PREFIX + "OFF") == "1":
        return "switched off (%sOFF=1)" % PREFIX
    if os.environ.get(PREFIX + "SSH"):
        return ""
    target, key = settings()
    if not target:
        return "no target configured (%sCONFIG or %sTARGET)" % (PREFIX, PREFIX)
    if not key or not os.path.isfile(key):
        return "key missing (%s)" % (key or "not configured")
    return ""


def runner_status(tries=2, timeout=60):
    for i in range(tries):
        try:
            p = subprocess.run(ssh_command("status"), capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            if p.returncode == 0:
                return json.loads(p.stdout.strip().splitlines()[-1])
        except (subprocess.SubprocessError, ValueError, IndexError, OSError):
            pass
        if i + 1 < tries:
            time.sleep(5)
    return None


def busy_reason(st):
    """Why the runner is busy right now, or ''. Since 0.24.0 only a note (the run starts at once anyway, throttled by the
    cgroup weight on the test machine) - never a reason to wait."""
    cores = st.get("cores") or 1
    load1 = (st.get("load") or [0])[0] or 0
    if st.get("test_runs"):
        return "%d test run(s) active" % st["test_runs"]
    if st.get("psi_cpu") is not None and st["psi_cpu"] >= THRESHOLD_PSI:
        return "CPU pressure %.0f %%" % st["psi_cpu"]
    if (st.get("cpu_percent") or 0) >= THRESHOLD_CPU:
        return "CPU %.0f %%" % st["cpu_percent"]
    if load1 >= THRESHOLD_LOAD_SHARE * cores:
        return "load %.1f on %d cores" % (load1, cores)
    return ""


# ---------------------------------------------------------------- queue (FIFO, 0.24.0)
def _pid_start(pid):
    """Start time of the process (field 22 of /proc/<pid>/stat, ticks since boot) or None if it is not running."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            raw = fh.read()
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _entry_alive(e):
    pid = e.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    start = _pid_start(pid)
    return start is not None and (e.get("pid_start") is None or start == e.get("pid_start"))


def queue_read(clean=True):
    """Live queue entries in arrival order. Orphans (process gone or PID reused) are removed on the way (if allowed)."""
    try:
        names = sorted(n for n in os.listdir(QUEUE) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for n in names:
        pf = os.path.join(QUEUE, n)
        try:
            with open(pf) as fh:
                e = json.load(fh)
        except (OSError, ValueError):
            e = None
        if not isinstance(e, dict) or not _entry_alive(e):
            if clean:
                try:
                    if e is not None or time.time() - os.path.getmtime(pf) > 60:     # half written file: not at once
                        os.remove(pf)
                except OSError:
                    pass
            continue
        e["file"] = n
        out.append(e)
    return out


def queue_join(request):
    """Create our own entry -> file name or None (queue not writable)."""
    try:
        os.makedirs(QUEUE, exist_ok=True)
    except OSError:
        pass
    pid = os.getpid()
    o = request.get("origin") or {}
    e = {"id": request["id"], "pid": pid, "pid_start": _pid_start(pid), "since": int(time.time()), "repo": request["repo"],
         "profile": request["profile"], "mode": request["mode"], "slot": o.get("slot"), "ref": o.get("ref")}
    name = "%020d-%s.json" % (time.time_ns(), request["id"])
    tmp = os.path.join(QUEUE, ".%s.tmp" % name)
    try:
        with open(tmp, "w") as fh:
            json.dump(e, fh)
        os.chmod(tmp, 0o664)                     # the group may clean up orphans of other users
        os.replace(tmp, os.path.join(QUEUE, name))
    except OSError as ex:
        info("queue not writable (%s) - waiting without a queue position" % ex)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return name


def queue_leave(name):
    if name:
        try:
            os.remove(os.path.join(QUEUE, name))
        except OSError:
            pass


def typical_duration_s(repo, profile, mode):
    """Median run time (without waiting) of equal runs from the own protocols, else DEFAULT_DURATION_S."""
    values = []
    try:
        names = sorted(n for n in os.listdir(LOGS) if n.endswith(".json"))[-200:]
    except OSError:
        names = []
    for n in names:
        try:
            with open(os.path.join(LOGS, n)) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if (m.get("outcome") == "done" and m.get("repo") == repo and m.get("profile") == profile
                and m.get("mode") == mode and isinstance(m.get("total_s"), int)):
            values.append(max(60, m["total_s"] - int(m.get("waited_s") or 0)))
    if not values:
        return DEFAULT_DURATION_S
    values.sort()
    return values[len(values) // 2]


def run_text(e):
    who = " / ".join(x for x in ("slot %s" % e["slot"] if e.get("slot") else "",
                                  "#%s" % e["ref"] if e.get("ref") else "") if x)
    return "%s/%s" % (e.get("repo") or "?", e.get("mode") or "?") + (" (%s)" % who if who else "")


def free_in_s(st, ahead):
    """Estimated seconds until a slot is free for us: running runs end after their typical duration, the waiting runs
    ahead of us take the slot that frees up first, one after the other."""
    slots = max(1, int(st.get("max_runs") or 2))
    ends = sorted(max(60, typical_duration_s(r.get("repo"), r.get("profile"), r.get("mode")) - int(r.get("runtime_s") or 0))
                  for r in (st.get("runs") or []))[:slots]
    ends += [0] * (slots - len(ends))
    for e in ahead:
        ends.sort()
        ends[0] += typical_duration_s(e.get("repo"), e.get("profile"), e.get("mode"))
    return min(ends)


def queue_state(st, own, own_id):
    """-> (may_start, position 1-based, ahead[entries], running[runs]). Waiting runs that already hold a slot on the test
    machine do not count as waiting any more (no double counting)."""
    running = st.get("runs") or []
    running_ids = {r.get("id") for r in running}
    waiting = [e for e in queue_read() if e.get("id") not in running_ids]
    if own and not any(e["file"] == own for e in waiting):
        waiting.append({"file": own, "id": own_id})
        waiting.sort(key=lambda e: e["file"])
    pos = next(i for i, e in enumerate(waiting) if e["file"] == own) if own else len(waiting)
    free = max(0, int(st.get("max_runs") or 2) - int(st.get("test_runs") or 0))
    return pos < free, pos + 1, waiting[:pos], running


def queue_summary(running_ids=()):
    """For the collector: {length, waiting: [{position, repo, profile, mode, slot, ref, since_s}]}."""
    now = time.time()
    w = [e for e in queue_read() if e.get("id") not in set(running_ids)]
    return {"length": len(w), "waiting": [{"position": i + 1, "id": e.get("id"), "repo": e.get("repo"),
                                           "profile": e.get("profile"), "mode": e.get("mode"), "slot": e.get("slot"),
                                           "ref": e.get("ref"), "since_s": max(0, int(now - (e.get("since") or now)))}
                                          for i, e in enumerate(w)]}


def git(repo, *args, binary=False):
    p = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=not binary)
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace") if binary else p.stderr
        raise SystemExit("git %s failed: %s" % (args[0], err.strip()[:200]))
    return p.stdout


def repo_name(repo):
    try:
        url = git(repo, "remote", "get-url", "origin").strip()
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    except SystemExit:
        name = os.path.basename(os.path.abspath(repo))
    name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40]
    return name or "repo"


def commit_of(repo, ref):
    return git(repo, "rev-parse", "--verify", "%s^{commit}" % ref).strip()


def build_package(dest, repo, commit, extra=None, extra_commit=None, extra_path=None):
    """One tar: source/ = git archive of the commit, extra/ = git archive of the extra repo (optionally one subpath)."""
    with tarfile.open(dest, "w") as out:
        parts = [("source/", repo, commit, [])]
        if extra:
            parts.append(("extra/", extra, extra_commit, [extra_path] if extra_path else []))
        for prefix, r, c, paths in parts:
            raw = git(r, "archive", "--format=tar", "--prefix=" + prefix, c, *paths, binary=True)
            with tarfile.open(fileobj=io.BytesIO(raw)) as src:
                for m in src.getmembers():
                    if m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                        continue
                    out.addfile(m, src.extractfile(m) if m.isfile() else None)
    return os.path.getsize(dest)


def version_of(repo, commit):
    p = subprocess.run(["git", "-C", repo, "show", "%s:VERSION" % commit], capture_output=True, text=True)
    v = p.stdout.strip() if p.returncode == 0 else ""
    return v if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?", v) else ""


class Run:
    def __init__(self, args, protocol):
        self.args = args
        self.protocol = protocol
        self.junit = []
        self.end = None
        self.busy = False
        self.error = None
        self.started = None              # time of the first line after the slot was granted
        self.on_start = None

    def _started(self):
        if self.started is None:
            self.started = time.time()
            if self.on_start:
                self.on_start()

    def line(self, z, repeat=False):
        if z.startswith(CTL):
            try:
                d = json.loads(z[len(CTL):])
            except ValueError:
                return
            kind = d.get("kind")
            if kind == "junit":
                if not repeat or not self.end:
                    self.junit.append(d.get("b64", ""))
            elif kind == "end":
                self.end = d
            elif kind == "busy":
                self.busy = True
                return
            elif kind == "error":
                self.error = d.get("text", "")
                info("receiver: %s" % self.error)
                return
            elif kind == "build":
                self.protocol.write("[build] %s\n" % d.get("text", ""))
                if not self.args.quiet_build:
                    info("build: %s" % d.get("text", ""))
            elif kind == "info":
                self.protocol.write("[info] %s\n" % d.get("text", ""))
                info(d.get("text", ""))
            if kind in ("build", "info", "junit", "end"):
                self._started()
            return
        self._started()
        self.protocol.write(z + "\n")
        sys.stdout.write(z + "\n")
        sys.stdout.flush()


def read_stream(run, cmd, stdin=None, skip=0):
    """Run a command and hand its lines to `run`; plain lines only after `skip` (re-fetching)."""
    p = subprocess.Popen(cmd, stdin=stdin or subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", bufsize=1)
    plain = 0
    for z in p.stdout:
        z = z.rstrip("\n")
        if z.startswith(CTL):
            run.line(z, repeat=skip > 0)
            continue
        plain += 1
        if plain > skip:
            run.line(z)
    err = p.stderr.read()
    rc = p.wait()
    return rc, plain, err


def fetch_result(run, rid, shown, max_s):
    """After a dropped connection: fetch the result until it is there (the run over there keeps going)."""
    deadline = time.time() + max_s
    away_since = None
    while time.time() < deadline:
        before = run.end
        try:
            p = subprocess.run(ssh_command("result %s" % rid), capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL)
        except subprocess.SubprocessError:
            p = None
        if p is not None and p.returncode == 0 and '"kind": "running"' not in p.stdout and CTL in p.stdout:
            plain = 0
            for z in p.stdout.splitlines():
                if z.startswith(CTL):
                    if '"kind": "junit"' in z:
                        if before is None:
                            run.line(z)
                    else:
                        run.line(z)
                    continue
                plain += 1
                if plain > shown:
                    run.line(z)
            if run.end:
                return True
        if p is None or p.returncode != 0:
            away_since = away_since or time.time()
            if time.time() - away_since > 20 * 60:
                info("runner unreachable for 20 min - result lost")
                return False
        else:
            away_since = None
        time.sleep(60)
    return False


def origin():
    """Which slot/task started the run, from the optional variables AIWERKSTATT_TEST_RUNNER_SLOT / _REF (numbers)."""
    def number(name):
        v = os.environ.get(PREFIX + name, "")
        return int(v) if v.isdigit() and len(v) < 9 else None
    return {"slot": number("SLOT"), "ref": number("REF")}


def write_atomic(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def collect(once=False):
    """Collector: every COLLECT_TICK_S the read-only command `status`; result to <STATUS_DIR>/status.json and one line
    per minute to <day>.jsonl (14 days). An error means unreachable: the last values stay with an old `stand`.
    Never blocking: every request has a time limit."""
    os.makedirs(STATUS_DIR, exist_ok=True)
    last = {}
    try:
        with open(os.path.join(STATUS_DIR, "status.json")) as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        pass
    last_minute = 0
    while True:
        t0 = time.time()
        reason = unavailable_reason()
        st = None if reason else runner_status(tries=1, timeout=25)
        now = int(time.time())
        if st:
            fields = ("load", "cores", "psi_cpu", "psi_io", "ram_free_gb", "cpu_percent", "test_runs", "runs", "max_runs")
            last = {k: st.get(k) for k in fields}
            last["queue"] = queue_summary(r.get("id") for r in st.get("runs") or [])
            last.update(t=now, ok=True, error=None, stand=now)
            if now // 60 != last_minute:
                last_minute = now // 60
                row = {"t": now, "load": st.get("load"), "cores": st.get("cores"), "cpu_pct": st.get("cpu_percent"),
                       "psi_cpu": st.get("psi_cpu"), "ram_free_gb": st.get("ram_free_gb"), "test_runs": st.get("test_runs")}
                day = os.path.join(STATUS_DIR, time.strftime("%Y-%m-%d") + ".jsonl")
                with open(day, "a") as fh:
                    fh.write(json.dumps(row) + "\n")
                os.chmod(day, 0o640)
                limit = time.time() - COLLECT_DAYS * 86400
                for n in os.listdir(STATUS_DIR):
                    pf = os.path.join(STATUS_DIR, n)
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", n) and os.path.getmtime(pf) < limit:
                        os.remove(pf)
        else:
            last = dict(last, t=now, ok=False, error=reason or "unreachable", queue=queue_summary())
            last.setdefault("stand", None)
        write_atomic(os.path.join(STATUS_DIR, "status.json"), json.dumps(last, ensure_ascii=False))
        if once:
            return 0 if st else UNAVAILABLE
        time.sleep(max(1.0, COLLECT_TICK_S - (time.time() - t0)))


def protocol_base(repo, rid):
    os.makedirs(LOGS, exist_ok=True)
    return os.path.join(LOGS, "%s-%s-%s" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), repo, rid))


def execute(a):
    reason = unavailable_reason()
    if reason:
        info("runner unavailable: %s -> run locally" % reason)
        return UNAVAILABLE
    repo = os.path.abspath(a.repo)
    commit = commit_of(repo, a.commit)
    name = repo_name(repo)
    extra_path, extra_commit = None, None
    if a.extra:
        extra_path, _, ref = a.extra.partition("@")
        extra_path = os.path.abspath(extra_path)
        extra_commit = commit_of(extra_path, ref or "HEAD")
    env = {}
    for e in a.env or []:
        k, _, v = e.partition("=")
        env[k] = v
    rid = "%s-%s" % (datetime.datetime.now().strftime("%m%d%H%M%S"), secrets.token_hex(4))
    request = {"id": rid, "profile": a.profile, "mode": a.mode, "repo": name, "commit": commit,
               "version": version_of(repo, commit), "n": a.n, "args": a.runner_args, "env": env, "origin": origin()}
    if a.pins:
        request["pins"] = [p for p in a.pins.split(",") if p]
    os.makedirs(WORK, exist_ok=True)
    work = tempfile.mkdtemp(prefix="run-", dir=WORK)
    base = protocol_base(name, rid)
    meta = {"id": rid, "repo": name, "profile": a.profile, "mode": a.mode, "commit": commit, "n": a.n,
            "extra": extra_commit, "start": datetime.datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    own = None
    try:
        package = os.path.join(work, "package.tar")
        size = build_package(package, repo, commit, extra_path, extra_commit, a.extra_path)
        meta["package_kb"] = size // 1024
        info("%s@%s -> runner (%s/%s, package %d KB, run %s)" % (name, commit[:12], a.profile, a.mode, size // 1024, rid))
        # 0.24.0: free slot -> at once (throttled on the test machine); both taken -> FIFO queue, no deadline
        with open(base + ".log", "w") as prot:
            run = Run(a, prot)
            st = runner_status()
            if st is None:
                info("runner not reachable -> run locally")
                meta.update(outcome="unreachable")
                return UNAVAILABLE
            meta["runner_before"] = st
            own = queue_join(request)
            meta["queue"] = bool(own)
            fails = 0
            last_note, last_pos = 0.0, None
            t_wait = time.time()

            def got_slot():
                queue_leave(own)
                meta["started"] = round(run.started, 3)
                w = int(run.started - t_wait)
                if w >= WAIT_TICK_S:
                    info("slot free after %d min in the queue - run starts" % max(1, round(w / 60)))
            run.on_start = got_slot
            while True:
                may, pos, ahead, running = queue_state(st, own, rid)
                note = None
                if may:
                    busy = busy_reason(dict(st, test_runs=0))
                    if busy:
                        info("runner loaded (%s) - starting at once anyway, throttled by the cgroup weight there" % busy)
                    run.busy = False
                    with open(package, "rb") as stdin:
                        rc, shown, err = read_stream(run, ssh_command("run %s" % base64.b64encode(
                            json.dumps(request).encode()).decode()), stdin=stdin)
                    if not run.busy:
                        break
                    note = "runner fully booked after all"      # someone without the queue came first; keep our position
                if pos != last_pos or time.time() - last_note >= WAIT_NOTE_S or note:
                    if ahead:
                        before = ", ".join(run_text(e) for e in ahead[:3]) + (" ... (+%d)" % (len(ahead) - 3) if len(ahead) > 3 else "")
                    else:
                        before = "running " + (", ".join(run_text(r) for r in running) or "%s runs" % st.get("test_runs"))
                    info("%squeue position %d, ahead of you: %s, expected free in ~%d min (no deadline, no abort)"
                         % ((note + " - ") if note else "", pos, before, max(1, round(free_in_s(st, ahead) / 60))))
                    last_note, last_pos = time.time(), pos
                time.sleep(WAIT_TICK_S)
                new = runner_status(tries=1, timeout=60)
                if new is None:
                    fails += 1
                    if fails >= STATUS_FAILS_MAX:
                        info("runner not reachable %d times in a row -> run locally" % fails)
                        meta.update(outcome="unreachable", waited_s=int(time.time() - t_wait))
                        return UNAVAILABLE
                    continue
                fails = 0
                st = new
            meta["waited_s"] = int((run.started or time.time()) - t_wait)
            if run.error and not run.end and rc == 2:
                meta.update(outcome="refused", reason=run.error)
                info("request refused by the runner: %s -> run locally" % run.error)
                return UNAVAILABLE
            if not run.end:
                info("connection dropped (ssh rc %d%s) - the run goes on over there, fetching the result afterwards"
                     % (rc, (": " + err.strip()[:200]) if err.strip() else ""))
                if not fetch_result(run, rid, shown, 5 * 3600):
                    meta.update(outcome="lost")
                    return UNAVAILABLE
        e = run.end
        meta.update(outcome="done", rc=e.get("rc"), reason=e.get("reason"), runner=e, total_s=int(time.time() - t0))
        if run.junit and a.junit:
            try:
                with open(a.junit, "wb") as fh:
                    fh.write(base64.b64decode("".join(run.junit)))
            except (ValueError, OSError) as ex:
                info("JUnit not written: %s" % ex)
        info("done: rc %s - total %d s (build %s s, test %s s, waited %d s) - runner load max %s, CPU max %s %% - "
             "unhealthy during: %s" % (e.get("rc"), int(time.time() - t0), e.get("build_s"), e.get("test_s"),
                                       meta.get("waited_s") or 0, e.get("load_max"), e.get("cpu_max"),
                                       ", ".join(e.get("unhealthy_during") or []) or "none"))
        if e.get("reason"):
            info("reason: %s" % e["reason"])
        if e.get("infra"):
            info("the fault was the runner's, not the code's -> run locally")
            return UNAVAILABLE
        rc = e.get("rc")
        return rc if isinstance(rc, int) else 3
    finally:
        meta["end"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(base + ".json", "w") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        queue_leave(own)
        shutil.rmtree(work, ignore_errors=True)        # only our own run folder


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    runner_args = []
    if "--" in argv:
        i = argv.index("--")
        argv, runner_args = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(description="Run a test suite on a second test machine")
    p.add_argument("repo", nargs="?")
    p.add_argument("--profile")
    p.add_argument("--mode")
    p.add_argument("--commit", default="HEAD")
    p.add_argument("-n", type=int, default=2)
    p.add_argument("--extra", help="second repo (for example test tooling) as extra/, form path[@ref]")
    p.add_argument("--extra-path", help="only this subpath of the extra repo")
    p.add_argument("--env", action="append", help="K=V (only variables on the profile's allow list)")
    p.add_argument("--pins", help="test tools for base profiles, comma separated package==version")
    p.add_argument("--junit", help="write the JUnit report here")
    p.add_argument("--wait-max-min", type=int, default=None,
                   help="no effect since 0.24.0 (queue without deadline), only accepted for old callers")
    p.add_argument("--quiet-build", action="store_true", help="build lines only into the protocol")
    p.add_argument("--status", action="store_true")
    p.add_argument("--available", action="store_true")
    p.add_argument("--collect", action="store_true", help="collector for the load (run it as a service)")
    p.add_argument("--once", action="store_true", help="with --collect: only one request")
    a = p.parse_args(argv)
    a.runner_args = runner_args
    a.n = max(1, min(a.n, 8))
    if a.collect:
        return collect(a.once)
    if a.status or a.available:
        reason = unavailable_reason()
        st = None if reason else runner_status()
        if a.status:
            print(json.dumps(st if st else {"available": False, "reason": reason or "unreachable"},
                             ensure_ascii=False, indent=1))
        return 0 if st else UNAVAILABLE
    if not a.repo or not a.profile or not a.mode:
        p.error("repo, --profile and --mode are required")
    if a.wait_max_min is not None:
        info("--wait-max-min has no effect (since 0.24.0: queue without deadline, never local because of waiting)")
    return execute(a)


if __name__ == "__main__":
    sys.exit(main())
