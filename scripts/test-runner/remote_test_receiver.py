#!/usr/bin/python3 -I
"""remote_test_receiver - receiving end of the remote test runner (AIWerkstatt 0.22.0).

Runs ONLY as the forced command of a dedicated SSH key:

    restrict,from="<client address>",command="/usr/bin/python3 -I <dir>/remote_test_receiver.py" ssh-ed25519 ...

The caller can do nothing but the subcommands below (SSH_ORIGINAL_COMMAND), and "run" only for profiles of the
allow list in profiles.json next to this file (see profiles.example.json). Python 3.8+, standard library only.
No Docker socket is handed out, no mount except the read-only source package, no network for the tests.

Subcommands:
    status                 one JSON line: load[3], cores, psi_cpu/psi_io (null without /proc/pressure), ram_free_gb,
                           cpu_percent, test_runs, runs[] (id, repo, profile, mode, runtime_s, slot, ref), max_runs, time.
                           Nothing about other containers or services of this machine.
    run <base64-json>      stdin = tar with source/ (and optionally extra/); build + test run; output streamed
    result <id>            deliver the result of a run again (after a dropped connection) or "running"
    images                 one JSON line (read only, 0.32.0): per base profile the identity of the test image WITHOUT code
                           (checksum of the Dockerfile template + image id of its FROM image), so a client can put it into
                           its result keys before it runs anything.

Image identity (0.32.0): after every build the receiver sends a control line {"kind": "image", "id": <image id>, "fp": <fingerprint
of the image ENVIRONMENT>, "fp_type": ..., ...} and also puts it into the end object. Environment = base image (the last FROM)
+ all layers before the first copy of a directory (COPY/ADD dir: - the source code); for a base profile (the source is only
mounted) the whole image is the environment. A result store takes over green results only from the same environment.
fp_type: "layers" (boundary found), "base_only" (history and layers do not match: only the base image counts), "base" (base
profile), "unknown" (no usable base image information, fp is null). Never raises: without an identity the run is unaffected.

Output of "run"/"result": plain lines are the output of the test run, unchanged. Control lines start with
"\\x1eRTR " + JSON ({"kind": "build"|"info"|"image"|"junit"|"busy"|"error"|"end"|"running", ...}).

Rules: at most MAX_SLOTS (2) runs at the same time (one flock per slot, checked at once -> "busy"). Containers run with
--rm --name probe-<repo>-<time>-<id> --network none --cap-drop ALL --security-opt no-new-privileges --no-healthcheck
--memory, a low --cpu-shares and a CPU limit chosen in profiles.json: "cpus" (--cpus, needs a kernel with CFS quota)
or "cpuset" (--cpuset-cpus, one set per slot). A running test is never aborted, not even when the connection drops
(SIGHUP/SIGPIPE are ignored; the result stays retrievable for RESULT_DAYS). The only limit is an emergency brake
(max_run_seconds) against hung containers. A protocol line per run (load/CPU samples, unhealthy containers
before/during/after, own probe-* containers excluded) goes to log.jsonl next to this file.
"""
import base64
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time

BASE = os.environ.get("RTR_BASE") or os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(BASE, "work")
RESULTS = os.path.join(BASE, "results")
SLOTS = os.path.join(BASE, "slots")
LOG = os.path.join(BASE, "log.jsonl")
PROFILES_FILE = os.environ.get("RTR_PROFILES") or os.path.join(BASE, "profiles.json")
CTL = "\x1eRTR "
MAX_SLOTS = 2
MAX_PACKAGE = 400 * 1024 * 1024
RESULT_DAYS = 2
TICK_S = int(os.environ.get("RTR_TICK_S") or 30)
ID_RX = re.compile(r"^[a-z0-9][a-z0-9-]{5,40}$")
REPO_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
COMMIT_RX = re.compile(r"^[0-9a-f]{40}$")
VERSION_RX = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.]+)?$")
PIN_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\[\]-]{0,60}==[0-9][0-9A-Za-z.+!-]{0,30}$")
MEMORY_RX = re.compile(r"^[1-9][0-9]*[mg]$")
CPUS_RX = re.compile(r"^[0-9]+(\.[0-9]+)?$")
CPUSET_RX = re.compile(r"^[0-9]+([,-][0-9]+)*$")
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
FORBIDDEN_ARGS = ("--junitxml", "--junit-xml", "--basetemp", "--rootdir", "-c", "--confcutdir", "--override-ini", "-o",
                  "--pdb", "--trace", "--debug")
DEFAULT_SETTINGS = {"max_runs": 2, "memory": "4g", "cpu_mode": "cpus", "cpus": "2", "cpusets": [], "cpu_shares": 256,
                    "shm_size": "512m", "max_run_seconds": 4 * 3600, "docker": ["docker"]}
PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"]


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---- configuration (allow list)
def load_config():
    """profiles.json: must be owned by root or by the user running this receiver and not writable by others."""
    st = os.stat(PROFILES_FILE)
    if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
        raise ValueError("profiles.json must belong to root or the runner user and not be writable by others")
    with open(PROFILES_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("profiles.json is not an object")
    s = dict(DEFAULT_SETTINGS)
    s.update(raw.get("settings") or {})
    s["max_runs"] = max(1, min(int(s["max_runs"]), MAX_SLOTS))
    if not MEMORY_RX.match(str(s["memory"])) or not MEMORY_RX.match(str(s["shm_size"])):
        raise ValueError("memory/shm_size must look like 512m or 4g")
    if s["cpu_mode"] not in ("cpus", "cpuset"):
        raise ValueError("cpu_mode must be cpus or cpuset")
    if not CPUS_RX.match(str(s["cpus"])):
        raise ValueError("cpus must be a number")
    if s["cpu_mode"] == "cpuset":
        sets = s["cpusets"]
        if not isinstance(sets, list) or len(sets) < s["max_runs"] or not all(CPUSET_RX.match(str(x)) for x in sets):
            raise ValueError("cpusets needs one set like 0-3 per slot")
    if not isinstance(s["cpu_shares"], int) or not 2 <= s["cpu_shares"] <= 1024:
        raise ValueError("cpu_shares must be 2..1024")
    if not isinstance(s["max_run_seconds"], int) or s["max_run_seconds"] < 60:
        raise ValueError("max_run_seconds must be at least 60")
    if not isinstance(s["docker"], list) or not s["docker"] or not all(isinstance(x, str) for x in s["docker"]):
        raise ValueError("docker must be a command as a list")
    bases = raw.get("base_images") or {}
    profiles = raw.get("profiles") or {}
    if not isinstance(bases, dict) or not isinstance(profiles, dict):
        raise ValueError("base_images and profiles must be objects")
    for name, b in bases.items():
        df = b.get("dockerfile") if isinstance(b, dict) else None
        if isinstance(df, list):
            df = "\n".join(df) + "\n"
        if not NAME_RX.match(name) or not isinstance(df, str):
            raise ValueError("base image %r needs a dockerfile (string or list of lines)" % name)
        b["dockerfile"] = df
    for name, p in profiles.items():
        if not NAME_RX.match(name) or not isinstance(p, dict) or p.get("build") not in ("repo", "base"):
            raise ValueError("profile %r needs build repo|base" % name)
        if p["build"] == "base" and p.get("base") not in bases:
            raise ValueError("profile %r names an unknown base image" % name)
        if not isinstance(p.get("modes"), dict) or not p["modes"]:
            raise ValueError("profile %r has no modes" % name)
        for mname, m in p["modes"].items():
            if not NAME_RX.match(mname) or not isinstance(m.get("command"), list) or not m["command"] \
                    or not all(isinstance(x, str) for x in m["command"]):
                raise ValueError("profile %r mode %r needs a command list" % (name, mname))
            if "targets_required" in m and not isinstance(m["targets_required"], bool):
                raise ValueError("profile %r mode %r: targets_required must be true or false" % (name, mname))
        for pin in p.get("pins", []):
            if not PIN_RX.match(pin):
                raise ValueError("profile %r has an invalid pin" % name)
        for pat in (p.get("env_allowed") or {}).values():
            re.compile(pat)
    return {"settings": s, "base_images": bases, "profiles": profiles}


class Output:
    """Writes to stdout (until the connection drops) and always into the run log."""

    def __init__(self, log_path=None):
        self.log = open(log_path, "a", encoding="utf-8", errors="replace") if log_path else None
        self.gone = False
        self.lock = threading.Lock()

    def line(self, text):
        with self.lock:
            if self.log:
                self.log.write(text + "\n")
                self.log.flush()
            if not self.gone:
                try:
                    sys.stdout.write(text + "\n")
                    sys.stdout.flush()
                except (BrokenPipeError, OSError):
                    self.gone = True

    def control(self, kind, **data):
        data["kind"] = kind
        self.line(CTL + json.dumps(data, ensure_ascii=False))


# ---- measurements
def _cpu_counters():
    with open("/proc/stat") as fh:
        vals = [int(x) for x in fh.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_percent(duration=2.0):
    t1, i1 = _cpu_counters()
    time.sleep(duration)
    t2, i2 = _cpu_counters()
    return round(100.0 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def load1():
    with open("/proc/loadavg") as fh:
        return float(fh.read().split()[0])


def ram_free_mb():
    try:
        with open("/proc/meminfo") as fh:
            for z in fh:
                if z.startswith("MemAvailable:"):
                    return int(z.split()[1]) // 1024
    except OSError:
        pass
    return None


def psi(kind):
    """some avg60 from /proc/pressure/<kind> in percent, None without pressure stall information."""
    try:
        with open("/proc/pressure/%s" % kind) as fh:
            for z in fh:
                if z.startswith("some "):
                    return float(dict(t.split("=") for t in z.split()[1:])["avg60"])
    except (OSError, KeyError, ValueError):
        pass
    return None


def docker_cmd():
    try:
        return load_config()["settings"]["docker"]
    except (OSError, ValueError, KeyError):
        return list(DEFAULT_SETTINGS["docker"])


def docker(args, timeout=None, **kw):
    return subprocess.run(docker_cmd() + args, capture_output=True, text=True, timeout=timeout, **kw)


def unhealthy():
    p = docker(["ps", "--format", "{{.Names}}|{{.Status}}"], timeout=60)
    return sorted(z.split("|")[0] for z in p.stdout.splitlines()
                  if "(unhealthy)" in z and not z.startswith("probe-"))      # own test containers do not count


def running_runs():
    """Running tests (held slot locks): [{id, repo, profile, mode, runtime_s, slot, ref}]."""
    out = []
    for i in range(MAX_SLOTS):
        path = os.path.join(SLOTS, "slot%d.lock" % i)
        if not os.path.exists(path):
            continue
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                fh.seek(0)
                try:
                    d = json.loads(fh.read() or "{}")
                except ValueError:
                    d = {}
                out.append({"id": d.get("id", "?"), "repo": d.get("repo"), "profile": d.get("profile"),
                            "mode": d.get("mode"), "runtime_s": int(time.time() - (d.get("start") or time.time())),
                            "slot": d.get("slot"), "ref": d.get("ref")})
    return out


def status():
    """Read-only: load, cores, pressure, RAM, CPU and our own test runs - nothing about other services."""
    with open("/proc/loadavg") as fh:
        t = fh.read().split()
    free = ram_free_mb()
    runs = running_runs()
    try:
        max_runs = load_config()["settings"]["max_runs"]
    except (OSError, ValueError, KeyError):
        max_runs = DEFAULT_SETTINGS["max_runs"]
    print(json.dumps({"load": [float(t[0]), float(t[1]), float(t[2])], "cores": os.cpu_count(),
                      "psi_cpu": psi("cpu"), "psi_io": psi("io"),
                      "ram_free_gb": round(free / 1024.0, 1) if free is not None else None,
                      "cpu_percent": cpu_percent(), "test_runs": len(runs), "runs": runs, "max_runs": max_runs,
                      "time": int(time.time())}, ensure_ascii=False))
    return 0


# ---- request check
def check_request(raw, cfg):
    try:
        a = json.loads(base64.b64decode(raw.encode(), validate=True).decode())
    except (ValueError, UnicodeDecodeError):
        raise ValueError("request not readable")
    if not isinstance(a, dict):
        raise ValueError("request is not an object")
    known = {"id", "profile", "mode", "repo", "commit", "version", "n", "args", "env", "pins", "origin"}
    if set(a) - known:
        raise ValueError("unknown fields: %s" % sorted(set(a) - known))
    if not ID_RX.match(str(a.get("id", ""))):
        raise ValueError("invalid id")
    prof = cfg["profiles"].get(a.get("profile"))
    if not prof:
        raise ValueError("profile not in the allow list: %r" % a.get("profile"))
    if a.get("mode") not in prof["modes"]:
        raise ValueError("profile %s has no mode %r" % (a["profile"], a.get("mode")))
    if not REPO_RX.match(str(a.get("repo", ""))):
        raise ValueError("invalid repo")
    if not COMMIT_RX.match(str(a.get("commit", ""))):
        raise ValueError("commit must be 40 hex characters")
    if a.get("version") not in (None, "") and not VERSION_RX.match(str(a["version"])):
        raise ValueError("invalid version")
    n = a.get("n", 1)
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 8:
        raise ValueError("n must be 1..8")
    args = a.get("args") or []
    if not isinstance(args, list) or len(args) > 400 or not all(isinstance(x, str) for x in args):
        raise ValueError("args must be a list of strings (at most 400)")
    for x in args:
        if len(x) > 400 or any(c in x for c in "\x00\n\r"):
            raise ValueError("argument too long or with control characters")
        head = (x.split("=", 1)[0].split() or [""])[0]
        if head in FORBIDDEN_ARGS or re.match(r"^-[co].", x):
            raise ValueError("argument not allowed: %s" % head)
        if x.startswith("/") or ".." in x.split("::")[0].split("/"):
            raise ValueError("paths only relative to the source package: %s" % x[:80])
    if prof["modes"][a["mode"]].get("targets_required") and not [x for x in args if not x.startswith("-")]:
        raise ValueError("mode %s needs test targets (for example tests/test_x.py)" % a["mode"])
    env = a.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError("env must be an object")
    for k, v in env.items():
        pattern = (prof.get("env_allowed") or {}).get(k)
        if not pattern or not isinstance(v, str) or not re.match(pattern, v):
            raise ValueError("environment variable not allowed: %s" % k)
    pins = a.get("pins")
    if pins is not None:
        if prof["build"] != "base" or not isinstance(pins, list) or len(pins) > 30 \
                or not all(isinstance(p, str) and PIN_RX.match(p) for p in pins):
            raise ValueError("pins only for base profiles, form package==version")
    o = a.get("origin") or {}
    if not isinstance(o, dict) or set(o) - {"slot", "ref"} or any(
            v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < 10 ** 9) for v in o.values()):
        raise ValueError("invalid origin")
    a.update(n=n, args=args, env=env, origin={"slot": o.get("slot"), "ref": o.get("ref")})
    return a


def check_package(path, extra_allowed):
    """Only source/ (and extra/), no absolute or .. paths, no devices, links only inwards, nothing below links."""
    links = set()
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            name = m.name.rstrip("/")
            if m.name == "pax_global_header" or m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE):
                continue
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or "" in parts[:-1]:
                raise ValueError("unsafe path in package: %s" % name[:120])
            if parts[0] not in (("source", "extra") if extra_allowed else ("source",)):
                raise ValueError("package may only contain source/%s: %s" % ("+extra/" if extra_allowed else "", name[:120]))
            for i in range(1, len(parts)):
                if "/".join(parts[:i]) in links:
                    raise ValueError("entry below a link: %s" % name[:120])
            if m.issym():
                target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                if m.linkname.startswith("/") or target.split("/")[0] != parts[0] or target.startswith(".."):
                    raise ValueError("link points outside: %s" % name[:120])
                links.add(name)
            elif m.islnk():
                raise ValueError("hard links not allowed: %s" % name[:120])
            elif not (m.isfile() or m.isdir()):
                raise ValueError("special file in package: %s" % name[:120])


def unpack(path, dest):
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE) or m.name == "pax_global_header":
                continue
            m.mode = (m.mode | 0o644 | (0o111 if m.isdir() or m.mode & 0o100 else 0)) & 0o755
            m.uid = m.gid = 0
            m.uname = m.gname = ""
            tf.extract(m, dest, set_attrs=not m.issym())
    for root, dirs, _ in os.walk(dest):
        for d in dirs:
            p = os.path.join(root, d)
            if not os.path.islink(p):
                os.chmod(p, 0o755)
    os.chmod(dest, 0o755)


# ---- docker
def stream(args, out, kind=None, env=None, timeout=None):
    """Run a docker command, pass its lines on live (kind=None: plain line, else control line). Returns rc."""
    p = subprocess.Popen(docker_cmd() + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=env, text=True, errors="replace", bufsize=1)
    if timeout:
        deadline = time.time() + timeout

        def brake():
            while p.poll() is None:
                if time.time() > deadline:
                    p.kill()
                    return
                time.sleep(5)
        threading.Thread(target=brake, daemon=True).start()
    for z in p.stdout:
        z = z.rstrip("\n")
        if kind:
            out.control(kind, text=z[:2000])
        else:
            out.line(z)
    return p.wait()


def cpu_args(cfg, slot, build=False):
    s = cfg["settings"]
    if s["cpu_mode"] == "cpuset":
        return ["--cpuset-cpus", str(s["cpusets"][slot])]
    if build:                                   # docker build has no --cpus; the same limit as quota/period
        return ["--cpu-period", "100000", "--cpu-quota", str(int(float(s["cpus"]) * 100000))]
    return ["--cpus", str(s["cpus"])]


def limits(cfg):
    s = cfg["settings"]
    return ["--no-healthcheck", "--memory", s["memory"], "--cpu-shares", str(s["cpu_shares"]), "--shm-size", s["shm_size"],
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


def build_args_common(cfg, slot):
    return ["--cpu-shares", str(cfg["settings"]["cpu_shares"]), "--memory", cfg["settings"]["memory"]] + cpu_args(cfg, slot, True)


def build_base(cfg, prof, req, source, work, out):
    pins = req.get("pins") if req.get("pins") is not None else prof.get("pins", [])
    df = cfg["base_images"][prof["base"]]["dockerfile"]
    ctx = os.path.join(work, "base")
    os.makedirs(ctx)
    req_path = os.path.join(source, "requirements.txt")
    reqs = open(req_path, "rb").read() if os.path.isfile(req_path) and not os.path.islink(req_path) else b""
    with open(os.path.join(ctx, "Dockerfile"), "w") as fh:
        fh.write(df)
    with open(os.path.join(ctx, "requirements.txt"), "wb") as fh:
        fh.write(reqs)
    with open(os.path.join(ctx, "pins.txt"), "w") as fh:
        fh.write("\n".join(pins) + "\n")
    stamp = hashlib.sha256(df.encode() + b"\0" + reqs + b"\0" + "\n".join(pins).encode()).hexdigest()[:16]
    image = "rtr-base/%s:%s" % (req["profile"], stamp)
    if docker(["image", "inspect", image], timeout=60).returncode == 0:
        out.control("info", text="test image %s from cache" % image)
        return image, None
    out.control("info", text="building test image %s (dependencies, once per state)" % image)
    rc = stream(["build"] + build_args_common(cfg, req["_slot"]) + ["-t", image, ctx], out, "build",
                env=dict(os.environ, DOCKER_BUILDKIT="0"), timeout=cfg["settings"]["max_run_seconds"])
    if rc != 0:
        raise RuntimeError("building the test image failed (rc %d)" % rc)
    p = docker(["images", "--format", "{{.Tag}}|{{.CreatedAt}}", "rtr-base/%s" % req["profile"]], timeout=60)
    old = sorted((z.split("|") for z in p.stdout.splitlines() if "|" in z), key=lambda t: t[1], reverse=True)[2:]
    for tag, _ in old:                                   # at most the 2 newest states stay
        docker(["rmi", "rtr-base/%s:%s" % (req["profile"], tag)], timeout=120)
    return image, None


def build_repo(cfg, req, source, out):
    image = "rtr-%s:%s" % (req["profile"], req["id"])
    args = ["build"] + build_args_common(cfg, req["_slot"]) + ["-t", image]
    if req.get("version"):
        args += ["--build-arg", "VERSION=%s" % req["version"]]
    out.control("info", text="building image %s from the Dockerfile of the package" % image)
    rc = stream(args + [source], out, "build", env=dict(os.environ, DOCKER_BUILDKIT="0"),
                timeout=cfg["settings"]["max_run_seconds"])
    if rc != 0:
        raise RuntimeError("building the image failed (rc %d)" % rc)
    return image, image


CACHE_KEEP = int(os.environ.get("RTR_CACHE_KEEP") or 3)
CACHE_USE = os.path.join(BASE, "cache-use.json")


def cache_stamp(source):
    """State of the dependency layers: Dockerfile + requirements*.txt + .dockerignore of the package -> 12 hex, or None."""
    h = hashlib.sha256()
    try:
        names = sorted(os.listdir(source))
    except OSError:
        return None
    for n in names:
        if n in ("Dockerfile", ".dockerignore") or re.fullmatch(r"requirements[\w.-]*\.txt", n):
            p = os.path.join(source, n)
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as fh:
                    h.update(n.encode() + b"\0" + fh.read() + b"\0")
    return h.hexdigest()[:12]


def keep_cache(req, image, stamp=None):
    """Keep one cache image per state of Dockerfile + requirements (rtr-cache/<profile>:<stamp>; its layers stay in the build
    cache) and the CACHE_KEEP most recently USED ones; drop older ones and the run tag. 0.33.0: before, a single "last" slot made
    branches with different requirements rebuild every dependency layer on each switch (and change the image environment)."""
    repo = "rtr-cache/%s" % req["profile"]
    tag = stamp or "last"
    docker(["tag", image, "%s:%s" % (repo, tag)], timeout=60)
    docker(["rmi", image], timeout=300)
    try:
        with open(CACHE_USE) as fh:
            use = json.load(fh)
        if not isinstance(use, dict):
            use = {}
    except (OSError, ValueError):
        use = {}
    use["%s:%s" % (repo, tag)] = time.time()
    p = docker(["images", "--format", "{{.Tag}}", repo], timeout=60)
    tags = ["%s:%s" % (repo, t.strip()) for t in p.stdout.splitlines() if t.strip() and t.strip() != "<none>"]
    for t in sorted(tags, key=lambda t: use.get(t, 0), reverse=True)[CACHE_KEEP:]:
        docker(["rmi", t], timeout=300)
        use.pop(t, None)
    use = {k: v for k, v in use.items() if not k.startswith(repo + ":") or k in tags}
    try:
        tmp = CACHE_USE + ".new"
        with open(tmp, "w") as fh:
            json.dump(use, fh)
        os.replace(tmp, CACHE_USE)
    except OSError:
        pass


# ---- image identity (0.32.0)
def _sha(text, n=24):
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def last_from_text(text):
    """Reference of the FROM of the last build stage, or None (stage name, ARG in the FROM, no FROM)."""
    stages, ref = set(), None
    for line in (text or "").splitlines():
        t = line.strip().split()
        if len(t) >= 2 and t[0].upper() == "FROM":
            rest = [x for x in t[1:] if not x.startswith("--")]
            if not rest:
                continue
            ref = rest[0]
            if len(rest) >= 3 and rest[1].upper() == "AS":
                stages.add(rest[2].lower())
    if not ref or "$" in ref or ref.lower() in stages:
        return None
    return ref


def last_from(dockerfile):
    try:
        with open(dockerfile, encoding="utf-8", errors="replace") as fh:
            return last_from_text(fh.read())
    except OSError:
        return None


def _layers(image):
    """(image id, [layer digests]) or (None, None)."""
    p = docker(["image", "inspect", "--format", "{{.Id}}|{{json .RootFS.Layers}}", image], timeout=60)
    if p.returncode != 0 or "|" not in p.stdout:
        return None, None
    iid, _, lj = p.stdout.strip().partition("|")
    try:
        return iid, json.loads(lj) or []
    except ValueError:
        return iid, None


def _history(image):
    """Build history, oldest entry first (CreatedBy), or None."""
    p = docker(["history", "--no-trunc", "--format", "{{.CreatedBy}}", image], timeout=60)
    if p.returncode != 0:
        return None
    return list(reversed(p.stdout.splitlines()))


def _makes_layer(created_by):
    """Classic builder (DOCKER_BUILDKIT=0): RUN, COPY, ADD and WORKDIR create a file layer, other "#(nop)" steps do not."""
    if "#(nop)" not in created_by:
        return True
    return created_by.split("#(nop)", 1)[1].strip().startswith(("COPY ", "ADD ", "WORKDIR "))


def _code_boundary(created_by):
    rest = created_by.split("#(nop)", 1)[1].strip() if "#(nop)" in created_by else ""
    return rest.startswith(("COPY dir:", "ADD dir:", "COPY multi:", "ADD multi:"))


def image_info(prof, image, source):
    """Image id + fingerprint of the image environment (base + layers before the source code) -> dict or None. Never raises."""
    try:
        iid, layers = _layers(image)
        if not iid or layers is None:
            return None
        info = {"id": iid, "layers": len(layers)}
        if prof["build"] == "base":
            info.update(fp_type="base", fp=_sha("\n".join(layers)), env_layers=len(layers))
            return info
        ref = last_from(os.path.join(source, "Dockerfile"))
        info["base"] = ref
        base_id, base_layers = _layers(ref) if ref else (None, None)
        if not base_layers or layers[:len(base_layers)] != base_layers:
            info.update(fp_type="unknown", fp=None)
            return info
        info["base_id"] = base_id
        hist, base_hist = _history(image), _history(ref)
        boundary = None
        if hist and base_hist and hist[:len(base_hist)] == base_hist:
            n = len(base_layers)
            for cb in hist[len(base_hist):]:
                if _code_boundary(cb):
                    boundary = n
                    break
                if _makes_layer(cb):
                    n += 1
            own = sum(1 for cb in hist[len(base_hist):] if _makes_layer(cb))
            if len(base_layers) + own != len(layers):
                boundary = None
        if boundary is None:
            # history and layers do not match: only the base image is the environment (dependencies are covered by the code rule)
            info.update(fp_type="base_only", fp=_sha("base\n" + "\n".join(base_layers)), env_layers=len(base_layers))
        else:
            info.update(fp_type="layers", fp=_sha("\n".join(layers[:boundary])), env_layers=boundary)
        return info
    except Exception:  # noqa: BLE001 - the identity is a bonus, never endanger the run
        return None


def images(cfg):
    """Read only: identity of the base test images without code (Dockerfile template + FROM image) per base profile."""
    out = {}
    for name, prof in sorted(cfg["profiles"].items()):
        if prof.get("build") != "base":
            continue
        df = cfg["base_images"][prof["base"]]["dockerfile"]
        ref = last_from_text(df)
        iid = _layers(ref)[0] if ref else None
        out[name] = {"base": prof["base"], "dockerfile": _sha(df, 16), "from": ref, "from_id": iid,
                     "pins": _sha("\n".join(prof.get("pins", [])), 16)}
    print(json.dumps({"format": "rtr-images/1", "images": out, "time": int(time.time())}, ensure_ascii=False))
    return 0


WRAP = ('mkdir -p /tmp/rtr; "$@"; rc=$?; '
        'if [ -f /tmp/rtr/junit.xml ]; then printf "\\036RTR-JUNIT-BEGIN\\n"; '
        'python -c "import base64,sys; sys.stdout.write(base64.encodebytes(open(\'/tmp/rtr/junit.xml\',\'rb\').read()).decode())"; '
        'printf "\\036RTR-JUNIT-END\\n"; fi; printf "\\036RTR-RC %s\\n" "$rc"; exit $rc')


def container_command(mode, req):
    cmd = list(mode["command"])
    if cmd[:3] == ["python", "-m", "pytest"]:
        if req["n"] > 1:
            cmd += ["-n", str(req["n"]), "--dist", "worksteal"]
        if mode.get("junit", True):
            cmd += ["--junitxml=/tmp/rtr/junit.xml"]
        cmd += ["--basetemp=/tmp/rtr/bt"]
        cmd += req["args"] or mode.get("targets", [])
    return ["sh", "-c", WRAP, "_"] + cmd


def run_args(cfg, prof, req, image, source, extra):
    mode = prof["modes"][req["mode"]]
    # 0.40.1: the time stamp alone has one-second resolution - two runs of one gate (e.g. two overflow chunks on two slots)
    # that start in the same second collided on the name (docker rc 125). The tail of the run id makes it unique.
    tail = re.sub(r"[^0-9a-f]", "", str(req["id"]).rsplit("-", 1)[-1].lower())[:8] or "%04x" % (os.getpid() & 0xFFFF)
    name = "probe-%s-%s-%s" % (req["repo"], datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), tail)
    args = ["run", "--rm", "--name", name, "--label", "io.aiwerkstatt.test-runner=%s" % req["id"],
            "--network", "none"] + cpu_args(cfg, req["_slot"]) + limits(cfg)
    args += ["--read-only", "--tmpfs", "/tmp"] if mode.get("read_only", True) else ["--tmpfs", "/tmp"]
    for k, v in sorted(dict(mode.get("env", {}), **req["env"]).items()):
        args += ["-e", "%s=%s" % (k, v)]
    if prof["build"] == "base":
        args += ["-v", "%s:/source:ro" % source]
        if extra:
            args += ["-v", "%s:/extra:ro" % extra]
    if mode.get("workdir"):
        args += ["-w", mode["workdir"]]
    return name, args + [image] + container_command(mode, req)


class Sampler(threading.Thread):
    """Every TICK_S: load, CPU %, unhealthy containers - for the protocol (proof that other services stay healthy)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.unhealthy_during = set()
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                l1 = load1()
                u = unhealthy()
                self.unhealthy_during.update(u)
                self.samples.append({"t": now(), "load1": l1, "cpu": cpu_percent(1.0), "unhealthy": len(u)})
            except Exception:                       # measuring must never disturb a run
                pass
            self.halt.wait(TICK_S)


def take_slot(req, cfg):
    os.makedirs(SLOTS, exist_ok=True)
    for i in range(cfg["settings"]["max_runs"]):
        fh = open(os.path.join(SLOTS, "slot%d.lock" % i), "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            continue
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"id": req["id"], "repo": req["repo"], "profile": req["profile"], "mode": req["mode"],
                             "start": time.time(), "slot": req["origin"]["slot"], "ref": req["origin"]["ref"]}))
        fh.flush()
        req["_slot"] = i
        return fh
    return None


def old_results_away():
    limit = time.time() - RESULT_DAYS * 86400
    for d in (RESULTS, WORK):
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < limit and ID_RX.match(n):
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def run(raw):
    try:
        cfg = load_config()
        req = check_request(raw, cfg)
    except (ValueError, OSError) as e:
        Output().control("error", text=str(e))
        return 2
    rid = req["id"]
    prof = cfg["profiles"][req["profile"]]
    mode = prof["modes"][req["mode"]]
    res = os.path.join(RESULTS, rid)
    if os.path.exists(res):
        Output().control("error", text="id already used")
        return 2
    slot = take_slot(req, cfg)
    if not slot:
        Output().control("busy", test_runs=len(running_runs()), max_runs=cfg["settings"]["max_runs"])
        return 75
    # from here on: never abort, even if the connection drops
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    os.makedirs(res, mode=0o700)
    out = Output(os.path.join(res, "log"))
    work = os.path.join(WORK, rid)
    os.makedirs(work, mode=0o755)
    t0 = time.time()
    entry = {"id": rid, "origin": req["origin"], "profile": req["profile"], "mode": req["mode"], "repo": req["repo"],
             "commit": req["commit"], "version": req.get("version"), "n": req["n"], "start": now()}
    rc, reason, throwaway, name = 3, "", None, None
    infra = True          # until the test run delivers a result: the fault is the runner's, not the code's
    sampler = Sampler()
    junit, in_junit, rc_inside = [], False, None
    try:
        pkg = os.path.join(work, "package.tar")
        got = 0
        with open(pkg, "wb") as fh:
            while True:
                chunk = sys.stdin.buffer.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_PACKAGE:
                    raise ValueError("source package larger than %d MB" % (MAX_PACKAGE >> 20))
                fh.write(chunk)
        check_package(pkg, mode.get("extra", False))
        tree = os.path.join(work, "tree")
        os.makedirs(tree)
        unpack(pkg, tree)
        os.remove(pkg)
        source = os.path.join(tree, "source")
        req["_cache_stamp"] = cache_stamp(source)
        extra = os.path.join(tree, "extra") if mode.get("extra") else None
        if not os.path.isdir(source) or (extra and not os.path.isdir(extra)):
            raise ValueError("package without source/%s" % (" or extra/" if extra else ""))
        entry["package_kb"] = got // 1024
        entry["unhealthy_before"] = unhealthy()
        sampler.start()
        out.control("info", text="run %s - %s/%s - %s@%s - package %d KB" % (
            rid, req["profile"], req["mode"], req["repo"], req["commit"][:12], got // 1024))
        tb = time.time()
        if prof["build"] == "repo":
            image, throwaway = build_repo(cfg, req, source, out)
        else:
            image, throwaway = build_base(cfg, prof, req, source, work, out)
        entry["build_s"] = int(time.time() - tb)
        ident = image_info(prof, image, source)
        if ident:
            entry["image"] = ident
            out.control("image", **ident)
        name, args = run_args(cfg, prof, req, image, source, extra)
        out.control("info", text="starting %s (%s)" % (name, " ".join(cpu_args(cfg, req["_slot"]) + limits(cfg)[1:4])))
        tt = time.time()
        p = subprocess.Popen(docker_cmd() + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        deadline = tt + cfg["settings"]["max_run_seconds"]

        def brake():
            while p.poll() is None:
                if time.time() > deadline:
                    out.control("info", text="emergency brake after %d s: stopping container %s" % (
                        cfg["settings"]["max_run_seconds"], name))
                    docker(["stop", "-t", "30", name], timeout=120)
                    return
                time.sleep(10)
        threading.Thread(target=brake, daemon=True).start()
        for z in p.stdout:
            z = z.rstrip("\n")
            if z == "\x1eRTR-JUNIT-BEGIN":
                in_junit = True
            elif z == "\x1eRTR-JUNIT-END":
                in_junit = False
            elif in_junit:
                junit.append(z)
            elif z.startswith("\x1eRTR-RC "):
                try:
                    rc_inside = int(z.split()[1])
                except (IndexError, ValueError):
                    pass
            else:
                out.line(z)
        rc_docker = p.wait()
        entry["test_s"] = int(time.time() - tt)
        if rc_inside is None:
            rc, reason = 3, "test run without a final status (container stopped from outside? docker rc %d)" % rc_docker
        else:
            rc, infra = rc_inside, False
        if junit:
            try:
                data = "".join(junit)
                xml = base64.b64decode(data)
                with open(os.path.join(res, "junit.xml"), "wb") as fh:
                    fh.write(xml)
                for i in range(0, len(data), 3000):
                    out.control("junit", b64=data[i:i + 3000])
            except ValueError:
                reason = "unreadable JUnit report"
    except RuntimeError as e:                     # build failed: counts as red (it would locally, too)
        rc, reason, infra = 3, str(e)[:500], False
        out.control("error", text=reason)
    except (ValueError, OSError, tarfile.TarError, subprocess.SubprocessError) as e:
        rc, reason = 3, str(e)[:500]
        out.control("error", text=reason)
    finally:
        sampler.halt.set()
        if throwaway:
            try:
                keep_cache(req, throwaway, req.get("_cache_stamp"))
            except Exception:
                docker(["rmi", "-f", throwaway], timeout=300)
        shutil.rmtree(work, ignore_errors=True)
        try:
            entry["unhealthy_after"] = unhealthy()
        except Exception:
            entry["unhealthy_after"] = None
        s = sampler.samples
        entry.update(end=now(), duration_s=int(time.time() - t0), rc=rc, reason=reason, infra=infra, container=name,
                     samples=s[-600:], unhealthy_during=sorted(sampler.unhealthy_during),
                     load_max=max([x["load1"] for x in s]) if s else None,
                     cpu_max=max([x["cpu"] for x in s]) if s else None)
        final = {k: v for k, v in entry.items() if k != "samples"}
        final["sample_count"] = len(s)
        with open(os.path.join(res, "result.json"), "w") as fh:
            json.dump(dict(entry), fh, ensure_ascii=False)
        with open(LOG, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        out.control("end", **final)
        slot.close()
        old_results_away()
    return rc


def result(rid):
    if not ID_RX.match(rid):
        Output().control("error", text="invalid id")
        return 2
    res = os.path.join(RESULTS, rid)
    out = Output()
    if not os.path.isfile(os.path.join(res, "result.json")):
        if rid in [r["id"] for r in running_runs()] or os.path.isdir(res):
            out.control("running", id=rid)
            return 0
        out.control("error", text="no run with this id")
        return 2
    with open(os.path.join(res, "log"), encoding="utf-8", errors="replace") as fh:
        for z in fh:
            out.line(z.rstrip("\n"))
    return 0


def main():
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    parts = cmd.split()
    os.umask(0o022)
    for d in (WORK, RESULTS, SLOTS):
        os.makedirs(d, exist_ok=True)
    if parts == ["status"]:
        return status()
    if len(parts) == 2 and parts[0] == "run":
        return run(parts[1])
    if len(parts) == 2 and parts[0] == "result":
        return result(parts[1])
    if parts == ["images"]:
        try:
            return images(load_config())
        except (OSError, ValueError, KeyError) as e:
            Output().control("error", text="images: %s" % str(e)[:200])
            return 2
    Output().control("error", text="unknown command (allowed: status | run <request> | result <id> | images)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
