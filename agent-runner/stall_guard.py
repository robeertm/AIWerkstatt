#!/usr/bin/env python3
"""Stall guard (0.44.0): detect a hung agent on several axes and heal it without a human.

Why: a run can sit in a polling loop (for example `ps | grep <gate>; sleep 30`) waiting for a process that belongs to
another run. There is no output, no CPU and no growing log, yet the output-silence watchdog never fires because the loop
keeps starting new tool calls. The slot stays busy for hours although the work is long done.

This file is a building block for the session driver (`session.py`): pure measuring functions over /proc and files plus a
small state machine. It never ends anything by itself; the driver decides what to do.

Activity = at least ONE axis moves (everything else is silence):
  output  - the agent (or a subagent) streams output (API replies, tool results); the driver reports it
  cpu     - tool processes under the agent (all descendants except the provider CLI itself) use together at least
            CPU_SHARE of one core over the interval (default 2 %; a `ps; sleep 30` loop is about 0.3 %)
  io      - a long-lived tool process reads/writes at least IO_CHARS characters (rchar+wchar; ssh to a remote test
            runner, tail -f) or IO_BLOCK bytes of real disk I/O
  files   - a file named on a tool command line (gate log, `until grep ...` file) or an output file of a background
            task of this session grows or changes
  git     - git activity in the working directory (HEAD, index, reflog, ORIG_HEAD of the work tree)
  gate    - a progress file of this run (format aiwerkstatt-progress/1, written by the gates and the remote test runner,
            see backend/eta.py) is alive: not ended and its heartbeat is fresh. A long gate run with progress is never a
            stall.

Stages (thresholds `WK_STALL_GUARD_STAGES`, default 15/25/40 minutes of silence on ALL axes):
  1 - nudge (a message into the session)
  2 - end the hung tool processes, then a message "check state and continue"
  3 - end the run cleanly and report "hung"; the engine re-queues the task at the front with context

Never ended: the provider CLI itself, release/gate/deploy scripts (PROTECTED_SCRIPTS) and everything in their subtree.
Whether something is a protected script is decided by the program or script name, never by a word in a shell line -
otherwise `ps aux | grep release_check` would make the waiting loop itself untouchable.
"""
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import time

VERSION = "1.0.0"

STAGES_DEFAULT = (15 * 60, 25 * 60, 40 * 60)
TICK_DEFAULT = 60
CPU_SHARE = float(os.environ.get("WK_STALL_GUARD_CPU_SHARE", "0.02"))        # share of one core over the interval
IO_CHARS = int(os.environ.get("WK_STALL_GUARD_IO_CHARS", str(16 * 1024)))   # rchar+wchar per process and interval
IO_BLOCK = int(os.environ.get("WK_STALL_GUARD_IO_BLOCK", str(256 * 1024)))  # read_bytes+write_bytes per process and interval
GATE_FRESH_S = 180          # a progress file counts as alive while its heartbeat is younger than this
MAX_FILES = 40
PROGRESS_FORMAT = "aiwerkstatt-progress/1"

OWN = {"claude", "node", "codex", "gemini", "runuser", "env"}   # the provider CLI itself: never measured, never ended
WRAPPERS = {"timeout", "nice", "ionice", "env", "sudo", "runuser", "stdbuf", "setsid", "nohup", "flock", "chrt",
            "taskset", "time", "systemd-run", "unbuffer"}
INTERPRETER_RX = re.compile(r"^(python[0-9.]*|bash|sh|dash|zsh|node|perl|ruby)$")
# Release, gate and deploy steps: never ended, and stage 3 waits while one is alive in the tool tree.
PROTECTED_SCRIPTS = {"release_check.py", "security_gate.py", "branch_check.py", "chain.py", "projectplan.py",
                     "remote_test.py", "remote_test_receiver.py", "check_commit_identity.py", "self-update.sh",
                     "install.sh", "release.sh", "deploy.sh"}
NO_PATHS = ("/proc/", "/dev/", "/sys/", "/bin/", "/usr/", "/etc/", "/lib", "/sbin/", "/opt/", "/run/")
_PATH_RX = re.compile(r"(?:(?<=[\s'\"=<>|;(])|^)((?:~|/)[\w.@+\-/~]+)")

AXIS_TEXT = {"output": "output", "cpu": "CPU", "io": "I/O", "files": "growing logs", "git": "git", "gate": "gate progress"}


# ------------------------------------------------------------------------------------------------ reading /proc
def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def process(pid, proc_dir="/proc"):
    """One process -> {pid, ppid, comm, argv, ticks, io_c, io_b, start, cwd} or None."""
    p = os.path.join(proc_dir, str(pid))
    stat = _read(os.path.join(p, "stat"))
    if not stat:
        return None
    try:
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        f = stat.rsplit(")", 1)[1].split()
        ppid = int(f[1])
        ticks = int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])   # utime stime cutime cstime
        start = int(f[19])
    except (ValueError, IndexError):
        return None
    raw = _read(os.path.join(p, "cmdline"), binary=True) or b""
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    io_c = io_b = 0
    io = _read(os.path.join(p, "io"))
    if io:
        vals = {}
        for line in io.splitlines():
            k, _, v = line.partition(":")
            if v.strip().isdigit():
                vals[k.strip()] = int(v)
        io_c = vals.get("rchar", 0) + vals.get("wchar", 0)
        io_b = vals.get("read_bytes", 0) + vals.get("write_bytes", 0)
    try:
        cwd = os.readlink(os.path.join(p, "cwd"))
    except OSError:
        cwd = ""
    return {"pid": int(pid), "ppid": ppid, "comm": comm, "argv": argv, "ticks": ticks, "io_c": io_c, "io_b": io_b,
            "start": start, "cwd": cwd}


def all_processes(proc_dir="/proc"):
    out = {}
    try:
        names = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError:
        return out
    for n in names:
        p = process(n, proc_dir)
        if p:
            out[p["pid"]] = p
    return out


def descendants(procs, root):
    """All descendants of `root` (without root) as a set of PIDs."""
    kids = {}
    for p in procs.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])
    out, stack = set(), list(kids.get(int(root), []))
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(kids.get(x, []))
    return out


def program(argv):
    """Effective program or script of a command line (wrappers like timeout/nice/env skipped, interpreter -> script)."""
    a = list(argv or [])
    i = 0
    while i < len(a):
        b = os.path.basename(a[i])
        if b in WRAPPERS:
            i += 1
            while i < len(a) and (a[i].startswith("-") or re.fullmatch(r"[0-9.]+[smhd]?", a[i]) or "=" in a[i]
                                  or a[i] in ("-u", "--")):
                if a[i] == "-u" and i + 1 < len(a):     # runuser -u <name>
                    i += 1
                i += 1
            continue
        if INTERPRETER_RX.match(b):
            j = i + 1
            while j < len(a) and a[j].startswith("-"):
                if a[j] == "-c":
                    return b                            # shell line: the program is the shell, not a script
                if a[j] == "-m" and j + 1 < len(a):
                    return a[j + 1]
                j += 1
            return os.path.basename(a[j]) if j < len(a) else b
        return b
    return ""


def is_protected(argv):
    return program(argv) in PROTECTED_SCRIPTS


def paths_from(argv, cwd="", home=""):
    """Existing regular files named on a command line (absolute, ~/..., or relative to the process directory)."""
    out, words = [], []
    for a in argv or []:
        if " " in a or ";" in a or "|" in a:            # shell line (bash -c "...") -> split up
            try:
                words.extend(shlex.split(a, comments=False, posix=True))
            except ValueError:
                words.extend(a.split())
            words.extend(m.group(1) for m in _PATH_RX.finditer(a))
        else:
            words.append(a)
    for w in words:
        w = w.strip("'\"();|&<>")
        if not w or len(w) > 400 or any(c in w for c in "*?$`"):
            continue
        if w.startswith("~/") and home:
            w = os.path.join(home, w[2:])
        elif not w.startswith("/"):
            if "/" not in w and "." not in w:
                continue
            if not cwd:
                continue
            w = os.path.join(cwd, w)
        if w.startswith(NO_PATHS) or "/shell-snapshots/" in w or "/.ssh/" in w:
            continue
        try:
            if os.path.isfile(w) and w not in out:
                out.append(w)
        except (OSError, ValueError):
            continue
        if len(out) >= MAX_FILES:
            break
    return out


def git_files(wt):
    """State files of the working directory (own work tree: its gitdir)."""
    if not wt:
        return []
    dot = os.path.join(wt, ".git")
    gitdir = None
    if os.path.isdir(dot):
        gitdir = dot
    elif os.path.isfile(dot):
        m = re.match(r"gitdir:\s*(.+)", (_read(dot) or "").strip())
        if m:
            g = m.group(1).strip()
            gitdir = g if os.path.isabs(g) else os.path.normpath(os.path.join(wt, g))
    if not gitdir:
        return []
    return [os.path.join(gitdir, n) for n in ("HEAD", "index", "ORIG_HEAD", os.path.join("logs", "HEAD"))
            if os.path.exists(os.path.join(gitdir, n))]


def background_files(sid, tmp="/tmp"):
    """Output files of the background tasks of THIS session (/tmp/claude-<uid>/<cwd>/<sid>/tasks/*.output)."""
    if not sid or not re.fullmatch(r"[0-9a-f-]{8,64}", str(sid)):
        return []
    return sorted(glob.glob(os.path.join(tmp, "claude-*", "*", str(sid), "tasks", "*.output")))[:MAX_FILES]


def _sig(path):
    try:
        st = os.stat(path)
        return [int(st.st_mtime_ns), int(st.st_size)]
    except OSError:
        return None


def _json(path):
    t = _read(path)
    if not t:
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ gates
def gate_state(thread, pids=(), progress_dir="", now=None):
    """-> {run: stamp} for the progress files of THIS run that are not ended (thread id or writer pid match).
    Without a progress folder the axis is simply absent."""
    now = time.time() if now is None else now
    out = {}
    if not progress_dir:
        return out
    thread = int(thread) if str(thread or "").isdigit() else None
    pids = set(pids or ())
    try:
        names = [n for n in os.listdir(progress_dir) if n.endswith(".json") and not n.startswith("history")]
    except OSError:
        return out
    for n in names:
        d = _json(os.path.join(progress_dir, n))
        if not isinstance(d, dict) or d.get("format") != PROGRESS_FORMAT or d.get("end"):
            continue
        if not ((thread is not None and d.get("thread") == thread) or (d.get("pid") in pids)):
            continue
        stamp = float(d.get("stamp") or 0)
        if now - stamp <= 6 * 3600:
            out[str(d.get("run") or n)] = stamp
    return out


# ------------------------------------------------------------------------------------------------ measuring
def measure(root, thread=None, sid="", wt="", home="", proc_dir="/proc", tmp="/tmp", progress_dir="", now=None):
    """One measuring point of all axes (except `output`, which the driver reports). Never raises."""
    now = time.time() if now is None else now
    try:
        procs_all = all_processes(proc_dir)
        tree = descendants(procs_all, root) if root else set()
        procs, files = {}, set()
        for pid in sorted(tree):
            p = procs_all.get(pid)
            if not p or p["comm"] in OWN:
                continue
            procs[pid] = {"ticks": p["ticks"], "io_c": p["io_c"], "io_b": p["io_b"], "start": p["start"],
                          "prog": program(p["argv"]) or p["comm"], "protected": is_protected(p["argv"]),
                          "ppid": p["ppid"]}
            files.update(paths_from(p["argv"], p["cwd"], home))
        files.update(background_files(sid, tmp))
        return {"time": now, "procs": procs,
                "files": {f: _sig(f) for f in sorted(files)[:MAX_FILES * 2]},
                "git": {f: _sig(f) for f in git_files(wt)},
                "gate": gate_state(thread, set(procs), progress_dir, now)}
    except Exception as e:  # noqa: BLE001 - a measurement must never disturb the driver
        return {"time": now, "procs": {}, "files": {}, "git": {}, "gate": {}, "error": repr(e)[:200]}


def active_axes(old, new, hz=None):
    """Which axes moved between two measuring points? -> {axis: short text}."""
    hz = hz or _hz()
    out = {}
    if not old or not new:
        return out
    dt = max(1.0, float(new["time"]) - float(old["time"]))
    cpu = 0.0
    who_cpu, who_io = [], []
    for pid, p in (new.get("procs") or {}).items():
        a = (old.get("procs") or {}).get(pid)
        if not a or a.get("start") != p.get("start"):
            continue
        d = max(0, p["ticks"] - a["ticks"]) / float(hz)
        cpu += d
        if d >= 0.2:
            who_cpu.append(p["prog"])
        if p["io_c"] - a["io_c"] >= IO_CHARS or p["io_b"] - a["io_b"] >= IO_BLOCK:
            who_io.append(p["prog"])
    if cpu >= CPU_SHARE * dt:
        out["cpu"] = "%s (%.0f %%)" % (", ".join(sorted(set(who_cpu))[:3]) or "tools", 100.0 * cpu / dt)
    if who_io:
        out["io"] = ", ".join(sorted(set(who_io))[:3])
    for axis in ("files", "git"):
        a, n = old.get(axis) or {}, new.get(axis) or {}
        changed = [os.path.basename(k) for k, v in n.items() if v is not None and k in a and a[k] != v]
        if axis == "git":
            changed += [os.path.basename(k) for k, v in n.items() if v is not None and k not in a and a]
        if changed:
            out[axis] = ", ".join(sorted(set(changed))[:3])
    ga, gn = old.get("gate") or {}, new.get("gate") or {}
    for run, stamp in gn.items():
        if stamp and (stamp != ga.get(run) or float(new["time"]) - stamp <= GATE_FRESH_S):
            out["gate"] = "gate progress"
            break
    return out


def _hz():
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError, AttributeError):
        return 100


# ------------------------------------------------------------------------------------------------ stage 2: end tools
def endable(measurement):
    """Hung tools: processes in the tree under the provider CLI. If a tool subtree (everything under a direct child of the
    CLI) contains a protected program, the WHOLE subtree stays untouched - ending a `bash -c` or the `tail` at the end of a
    pipe would take the gate down with it (SIGPIPE). -> (to_end descending, protected)"""
    procs = {int(k): v for k, v in (measurement.get("procs") or {}).items()}
    protected = sorted(pid for pid, p in procs.items() if p.get("protected"))

    def top(pid):
        x = pid
        while procs.get(x, {}).get("ppid") in procs:
            x = procs[x]["ppid"]
        return x

    taboo = {top(pid) for pid in protected}
    return sorted((pid for pid in procs if top(pid) not in taboo), reverse=True), protected


def end(pids, wait_s=3.0, kill=os.kill):
    """SIGTERM, after `wait_s` SIGKILL for survivors. -> list of PIDs actually signalled."""
    hit = []
    for pid in pids:
        try:
            kill(int(pid), signal.SIGTERM)
            hit.append(int(pid))
        except (OSError, ProcessLookupError):
            pass
    if not hit:
        return hit
    until = time.time() + wait_s
    while time.time() < until:
        if not any(_alive(p) for p in hit):
            return hit
        time.sleep(0.2)
    for pid in hit:
        if _alive(pid):
            try:
                kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    return hit


def _alive(pid):
    st = _read("/proc/%d/stat" % int(pid))
    if not st:
        return False
    try:
        return st.rsplit(")", 1)[1].split()[0] != "Z"
    except IndexError:
        return False


# ------------------------------------------------------------------------------------------------ state machine
class Guard:
    """Remembers the last activity per axis and reports reached stages exactly once."""

    def __init__(self, stages=STAGES_DEFAULT, clock=time.time):
        self.stages = tuple(int(x) for x in stages)
        self.clock = clock
        self.last = clock()                 # last activity on any axis
        self.axes = {}                      # axis -> (time, text)
        self.stage = 0
        self.measurement = None
        self.healed = []                    # [{stage, axis, text, quiet_s}] since the last pick-up
        self.deferred = False               # stage 3 waits for a protected step

    def active(self, axis, text="", now=None):
        now = self.clock() if now is None else now
        self.axes[axis] = (now, text)
        if self.stage:
            self.healed.append({"stage": self.stage, "axis": axis, "text": text, "quiet_s": int(now - self.last)})
            self.stage = 0
            self.deferred = False
        self.last = max(self.last, now)

    def output(self, now=None):
        self.active("output", "", now)

    def rest(self, now=None):
        """No open turn (answer delivered, background wait handled elsewhere, API pause): the clock does not run."""
        now = self.clock() if now is None else now
        self.last = now
        self.stage = 0
        self.deferred = False
        self.measurement = None

    def tick(self, measurement, now=None):
        """New measuring point. -> newly reached stage (1..3) or 0."""
        now = self.clock() if now is None else now
        if self.measurement is not None:
            for axis, text in active_axes(self.measurement, measurement).items():
                self.active(axis, text, now)
        self.measurement = measurement
        quiet = now - self.last
        due = 0
        for i, s in enumerate(self.stages, 1):
            if quiet >= s:
                due = i
        if due > self.stage:
            self.stage = due
            return due
        return 0

    def quiet_s(self, now=None):
        return int((self.clock() if now is None else now) - self.last)

    def state(self, now=None):
        """For the status file (overview): only numbers and fixed words."""
        now = self.clock() if now is None else now
        return {"stage": self.stage, "quiet_s": int(now - self.last), "last_activity": int(self.last),
                "axes": {a: int(now - t) for a, (t, _x) in self.axes.items()},
                "thresholds": list(self.stages), "deferred": bool(self.deferred)}

    def pick_up_healed(self):
        h, self.healed = self.healed, []
        return h


def stages_from(text):
    """"900,1500,2400" -> (900, 1500, 2400); invalid -> default. Values must be strictly rising."""
    try:
        w = [int(float(x)) for x in str(text or "").split(",") if x.strip()]
    except ValueError:
        return STAGES_DEFAULT
    if len(w) != 3 or not (0 < w[0] < w[1] < w[2]):
        return STAGES_DEFAULT
    return tuple(w)


def minutes(s):
    return "%d min" % max(1, int(round(float(s) / 60.0)))


def quiet_text():
    return "no output, no CPU or I/O from the tools, no growing logs, no git activity, no gate progress"


# ------------------------------------------------------------------------------------------------ re-queue context
def work_state(wt, timeout=8):
    """Short, factual state of the working directory for the re-queue context (branch, HEAD, uncommitted files, commits
    not yet on the upstream branch). Read-only git, never raises, empty text without a repository."""
    def git(*a):
        try:
            r = subprocess.run(["git", "-C", wt] + list(a), capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    if not wt or not git("rev-parse", "--git-dir"):
        return ""
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "?"
    head = git("log", "-1", "--format=%h %s") or "no commit"
    dirty = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    ahead = git("rev-list", "--count", "@{upstream}..HEAD")
    lines = ["Branch: %s, last commit: %s" % (branch, head[:120]),
             "Uncommitted changes: %s" % (("%d file(s)" % len(dirty)) if dirty else "none")]
    if ahead.isdigit():
        lines.append("Commits not on the upstream branch: %s" % ahead)
    return "\n".join(lines)
