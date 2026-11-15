#!/usr/bin/env python3
"""remote_test.py - run a test suite on a second test machine and show its load (AIWerkstatt 0.22.0).

    remote_test.py <repo-path> --profile NAME --mode NAME [--commit REF] [-n N] [--extra <repo-path>[@REF]]
                   [--extra-path sub/dir] [--env K=V] [--pins a==1,b==2] [--junit FILE]
                   [--wait-max-min 60] [--quiet-build] [-- test-runner-arguments ...]
    remote_test.py --status                  # state of the runner (load, CPU, running tests) as JSON
    remote_test.py --available               # exit 0 = runner reachable and set up
    remote_test.py --collect [--once]        # collector: status.json + one jsonl per day (14 days)

What happens:
  1. The commit is packed with `git archive` (tracked files only: no secrets from untracked files) and sent over a
     dedicated SSH key with a forced command to the receiver (remote_test_receiver.py on the test machine).
  2. Throttle instead of abort: while the runner is busy (CPU >= 70 %, load >= 3/4 of the cores, pressure >= 40 %, or a
     test already running) the start waits (at most --wait-max-min, default 60) and then runs anyway. The receiver never
     accepts more than 2 runs at once; then the client keeps waiting (at most as long again).
  3. The output of the tests goes to stdout unchanged, build and info lines to stderr. The exit code is the exit code of
     the test run. If the connection drops, the run keeps going over there and the result is fetched afterwards.
  4. A protocol per run (duration, build/test time, load and CPU of the runner, unhealthy containers before/during/after)
     is written to the log folder.

Exit 100 = runner unavailable, request refused (for example an argument outside the allow list), a fault of the runner
instead of the code, or result not obtainable: the caller runs the tests locally. Exit 2 only for wrong use of this tool.
Anything else is the exit code of the test run (pytest: 0 green, 1 red, 2 collection error, ...).

Settings (no defaults with hosts): AIWERKSTATT_TEST_RUNNER_CONFIG = JSON file {"target": "user@host", "key": "/path"}
or AIWERKSTATT_TEST_RUNNER_TARGET / AIWERKSTATT_TEST_RUNNER_KEY. Optional: AIWERKSTATT_TEST_RUNNER_LOGS,
AIWERKSTATT_TEST_RUNNER_STATUS_DIR (collector output), AIWERKSTATT_TEST_RUNNER_OFF=1 (switch off, exit 100).
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
    """Why the runner is busy right now, or ''."""
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
            elif kind == "error":
                self.error = d.get("text", "")
                info("receiver: %s" % self.error)
            elif kind == "build":
                self.protocol.write("[build] %s\n" % d.get("text", ""))
                if not self.args.quiet_build:
                    info("build: %s" % d.get("text", ""))
            elif kind == "info":
                self.protocol.write("[info] %s\n" % d.get("text", ""))
                info(d.get("text", ""))
            return
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
            last = dict(last, t=now, ok=False, error=reason or "unreachable")
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
    try:
        package = os.path.join(work, "package.tar")
        size = build_package(package, repo, commit, extra_path, extra_commit, a.extra_path)
        meta["package_kb"] = size // 1024
        info("%s@%s -> runner (%s/%s, package %d KB, run %s)" % (name, commit[:12], a.profile, a.mode, size // 1024, rid))
        deadline = time.time() + a.wait_max_min * 60
        deadline_busy = deadline + a.wait_max_min * 60
        waited = 0
        with open(base + ".log", "w") as prot:
            run = Run(a, prot)
            while True:
                st = runner_status()
                if st is None:
                    info("runner not reachable -> run locally")
                    meta.update(outcome="unreachable")
                    return UNAVAILABLE
                busy = busy_reason(st)
                meta.setdefault("runner_before", st)
                if busy and time.time() < deadline:
                    if waited % 300 == 0:
                        info("runner busy (%s) - waiting (at most %d more min, then it runs anyway)"
                             % (busy, max(0, int((deadline - time.time()) / 60))))
                    time.sleep(WAIT_TICK_S)
                    waited += WAIT_TICK_S
                    continue
                if busy:
                    info("deadline over - starting despite load (%s)" % busy)
                run.busy = False
                with open(package, "rb") as stdin:
                    rc, shown, err = read_stream(run, ssh_command("run %s" % base64.b64encode(
                        json.dumps(request).encode()).decode()), stdin=stdin)
                if run.busy:
                    if time.time() < deadline_busy:
                        if waited % 300 == 0:
                            info("runner fully booked (2 runs) - waiting for a free slot")
                        time.sleep(WAIT_TICK_S)
                        waited += WAIT_TICK_S
                        continue
                    info("no free slot on the runner after %d min -> run locally" % (waited // 60))
                    meta.update(outcome="busy")
                    return UNAVAILABLE
                break
            meta["waited_s"] = waited
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
                                       meta.get("waited_s", 0), e.get("load_max"), e.get("cpu_max"),
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
    p.add_argument("--wait-max-min", type=int, default=60)
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
    return execute(a)


if __name__ == "__main__":
    sys.exit(main())
