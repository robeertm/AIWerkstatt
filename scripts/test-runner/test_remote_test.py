"""Tests for the remote test runner: receiver (forced command on the test machine) + client.

Everything runs with stand-ins for ssh and docker - never a real ssh, never a real docker, never a real machine.

Covers:
  * profiles.json: allow-list loading, ownership/permission check, example file, cpu modes.
  * check_request: allow list (profile/mode/fields/commit/id/repo/n/arguments/environment/pins/origin).
  * check_package: only source/ (extra/ only if allowed), no absolute or .. paths, links only inwards, nothing below
    links, no hard links/devices/FIFOs.
  * End to end: exit code, stdout without control lines, JUnit, protocol, result on the "machine", docker limits.
  * Queue (0.24.0: free slot -> at once despite high CPU, both taken -> FIFO without deadline, order, orphans, the
    receiver refuses a third run, estimate, collector), `result <id>`, collector, `status` keys, busy().
Run: python3 -m unittest discover -s scripts/test-runner
"""
import base64
import fcntl
import glob
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import re
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER = os.path.join(HERE, "remote_test_receiver.py")
CLIENT = os.path.join(HERE, "remote_test.py")
PY = sys.executable
COMMIT = "a" * 40
JUNIT_XML = b'<testsuite tests="3" failures="0"><testcase name="t1"/></testsuite>'
P = "AIWERKSTATT_TEST_RUNNER_"

DOCKER_STUB = '''import base64, json, os, sys
a = sys.argv[1:]
with open(os.environ["ATT_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
mode = os.environ.get("ATT_MODE", "green")
c = a[0] if a else ""
if c == "ps":
    if os.environ.get("ATT_PS"):
        print("some-other-service|Up 2 days (unhealthy)")
    sys.exit(0)
if c == "image":
    if "--format" in a:
        print("sha256:abc")
        sys.exit(0)
    sys.exit(1)
if c == "build":
    print("Step 1/3 : FROM python")
    print("Successfully built abc")
    sys.exit(0)
if c in ("tag", "rmi", "images", "stop"):
    sys.exit(0)
if c == "run":
    print("...")
    print("1 failed, 2 passed in 0.1s" if mode == "red" else "3 passed in 0.1s")
    sys.stdout.flush()
    if mode == "norc":
        sys.exit(137)
    rc = 1 if mode == "red" else 0
    print("\\x1eRTR-JUNIT-BEGIN")
    print(base64.b64encode(open(os.environ["ATT_JUNIT"], "rb").read()).decode())
    print("\\x1eRTR-JUNIT-END")
    print("\\x1eRTR-RC %d" % rc)
    sys.exit(rc)
sys.exit(0)
'''

SSH_STUB = '''import os, sys
cmd = sys.argv[-1]
if cmd == "status":
    if os.environ.get("ATT_STATUS_FAIL") == "1":
        sys.exit(1)
    if os.environ.get("ATT_STATUS"):
        print(os.environ["ATT_STATUS"])
        sys.exit(0)
env = dict(os.environ, SSH_ORIGINAL_COMMAND=cmd)
os.execvpe(sys.executable, [sys.executable, os.environ["ATT_RECEIVER"]], env)
'''

CALM = {"load": [0.1, 0.1, 0.1], "cores": 4, "psi_cpu": None, "psi_io": None, "ram_free_gb": 3.0, "cpu_percent": 5.0,
        "test_runs": 0, "runs": [], "max_runs": 2, "time": 1}


def profiles(docker_cmd, cpu_mode="cpus"):
    pytest = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"]
    return {
        "settings": {"max_runs": 2, "memory": "4g", "cpu_mode": cpu_mode, "cpus": "2", "cpusets": ["0-1", "2-3"],
                     "cpu_shares": 256, "shm_size": "512m", "max_run_seconds": 600, "docker": docker_cmd},
        "base_images": {"py": {"dockerfile": ["FROM scratch", "COPY requirements.txt pins.txt /base/"]}},
        "profiles": {
            "app": {"build": "repo", "env_allowed": {"APP_FLAG": "^[01]$"},
                    "modes": {"image": {"read_only": True, "command": pytest, "targets": ["tests"],
                                        "env": {"APP_DATA": "/tmp/p"}},
                              "full": {"read_only": False, "command": pytest, "targets": ["tests"]}}},
            "lib": {"build": "base", "base": "py", "pins": ["pytest==8.3.4", "pytest-xdist==3.6.1"], "env_allowed": {},
                    "modes": {"pytest": {"read_only": True, "command": pytest, "targets": [], "workdir": "/source"},
                              "extras": {"read_only": True, "command": ["bash", "/extra/run.sh"], "targets": [],
                                         "workdir": "/source", "extra": True, "junit": False}}},
        },
    }


def load_module(name, path, base):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"RTR_BASE": base}):
        spec.loader.exec_module(m)
    return m


_TMP = tempfile.mkdtemp(prefix="rtr-mod-")
_PROF = os.path.join(_TMP, "profiles.json")
with open(_PROF, "w") as _fh:
    json.dump(profiles(["docker"]), _fh)
os.chmod(_PROF, 0o644)
REC = load_module("rtr_receiver_t", RECEIVER, os.path.join(_TMP, "never-used"))
REC.PROFILES_FILE = _PROF
CLI = load_module("rtr_client_t", CLIENT, _TMP)
CFG = REC.load_config()


def teardown_module_dir():
    shutil.rmtree(_TMP, ignore_errors=True)


def tearDownModule():
    teardown_module_dir()


def enc(d):
    return base64.b64encode(json.dumps(d).encode()).decode()


def good(**over):
    a = {"id": "1115123456-abcd1234", "profile": "app", "mode": "image", "repo": "my-repo", "commit": COMMIT, "n": 1}
    a.update(over)
    return a


class Profiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtr-prof-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def load(self, data, mode=0o644):
        p = os.path.join(self.tmp, "profiles.json")
        with open(p, "w") as fh:
            json.dump(data, fh)
        os.chmod(p, mode)
        with mock.patch.object(REC, "PROFILES_FILE", p):
            return REC.load_config()

    def test_example_file_is_valid(self):
        with open(os.path.join(HERE, "profiles.example.json")) as fh:
            data = json.load(fh)
        cfg = self.load(data)
        self.assertEqual(set(cfg["profiles"]), {"example-app", "example-lib"})
        self.assertEqual(cfg["settings"]["max_runs"], 2)

    def test_group_or_world_writable_refused(self):
        for mode in (0o664, 0o646, 0o666):
            with self.subTest(oct(mode)):
                with self.assertRaises(ValueError):
                    self.load(profiles(["docker"]), mode)

    def test_max_runs_capped_at_two(self):
        d = profiles(["docker"])
        d["settings"]["max_runs"] = 9
        self.assertEqual(self.load(d)["settings"]["max_runs"], 2)

    def test_invalid_settings(self):
        mutations = {
            "memory": lambda d: d["settings"].update(memory="lots"),
            "cpu mode": lambda d: d["settings"].update(cpu_mode="all"),
            "cpus": lambda d: d["settings"].update(cpus="2; rm"),
            "cpuset too short": lambda d: d["settings"].update(cpu_mode="cpuset", cpusets=["0-1"]),
            "cpuset bad": lambda d: d["settings"].update(cpu_mode="cpuset", cpusets=["a", "b"]),
            "unknown base": lambda d: d["profiles"]["lib"].update(base="nope"),
            "bad build": lambda d: d["profiles"]["app"].update(build="host"),
            "bad pin": lambda d: d["profiles"]["lib"].update(pins=["evil"]),
            "no command": lambda d: d["profiles"]["app"]["modes"]["image"].update(command=[]),
            "docker not list": lambda d: d["settings"].update(docker="docker"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                d = profiles(["docker"])
                mutate(d)
                with self.assertRaises(ValueError):
                    self.load(d)


class CheckRequest(unittest.TestCase):
    def test_valid(self):
        a = REC.check_request(enc(good(version="1.2.3", args=["tests/test_a.py::T::t", "-k", "foo"],
                                       env={"APP_FLAG": "1"}, origin={"slot": 1, "ref": 22})), CFG)
        self.assertEqual(a["n"], 1)
        self.assertEqual(a["origin"], {"slot": 1, "ref": 22})
        self.assertEqual(a["env"], {"APP_FLAG": "1"})
        b = REC.check_request(enc(good(profile="lib", mode="pytest", pins=["pytest==8.3.4", "httpx==0.28.1"], n=8)), CFG)
        self.assertEqual(b["origin"], {"slot": None, "ref": None})

    def test_refused(self):
        cases = {
            "unknown profile": good(profile="evil"),
            "unknown mode": good(mode="pytest"),
            "mode of another profile": good(profile="lib", mode="image"),
            "unknown field": good(extra=1),
            "commit 39": good(commit="a" * 39), "commit 41": good(commit="a" * 41),
            "commit upper": good(commit="A" * 40), "commit not hex": good(commit="g" * 40),
            "id special": good(id="abc def;rm-rf"), "id upper": good(id="ABCDEFGH-1234"), "id short": good(id="ab"),
            "repo slash": good(repo="a/b"), "repo dots": good(repo="../x"), "repo semicolon": good(repo="a;b"),
            "repo upper": good(repo="Repo"),
            "bad version": good(version="1.2; rm"),
            "n 0": good(n=0), "n 9": good(n=9), "n text": good(n="2"), "n bool": good(n=True),
            "args not list": good(args="tests"), "args not str": good(args=[1]),
            "junitxml": good(args=["--junitxml=/x.xml"]), "junit-xml": good(args=["--junit-xml=/x"]),
            "basetemp": good(args=["--basetemp=/x"]), "basetemp split": good(args=["--basetemp", "/x"]),
            "rootdir": good(args=["--rootdir=/"]), "-c x": good(args=["-c", "x"]), "-cx": good(args=["-cx"]),
            "-oFoo": good(args=["-oFoo"]), "-o": good(args=["-o", "a=b"]), "override-ini": good(args=["--override-ini=a=b"]),
            "absolute": good(args=["/etc/passwd"]), "dots": good(args=["../x"]), "deep dots": good(args=["tests/../../x"]),
            "dots before ::": good(args=["a/../b.py::t"]), "newline": good(args=["a\nb"]), "cr": good(args=["a\rb"]),
            "nul": good(args=["a\x00b"]), "too long": good(args=["x" * 401]), "too many": good(args=["a"] * 401),
            "env foreign": good(env={"PATH": "/x"}), "env LD": good(env={"LD_PRELOAD": "/x"}),
            "env bad value": good(env={"APP_FLAG": "2"}), "env bad value text": good(env={"APP_FLAG": "01"}),
            "env not dict": good(env=["a"]),
            "env other profile": good(profile="lib", mode="pytest", env={"APP_FLAG": "1"}),
            "pins on repo build": good(pins=["pytest==8.3.4"]),
            "pins form": good(profile="lib", mode="pytest", pins=["evil"]),
            "pins injection": good(profile="lib", mode="pytest", pins=["a==1.0\nRUN x"]),
            "pins not list": good(profile="lib", mode="pytest", pins="pytest==1.0"),
            "origin bool": good(origin={"slot": True}), "origin negative": good(origin={"slot": -1}),
            "origin foreign": good(origin={"host": 1}), "origin str": good(origin={"ref": "1"}),
            "origin not dict": good(origin=[1]), "origin huge": good(origin={"slot": 10 ** 9}),
        }
        for name, a in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    REC.check_request(enc(a), CFG)

    def test_not_readable(self):
        for raw in ("!!!", base64.b64encode(b"not json").decode(), base64.b64encode(b"[1]").decode()):
            with self.subTest(raw):
                with self.assertRaises(ValueError):
                    REC.check_request(raw, CFG)


def make_tar(path, entries):
    """entries: (name, type, linkname|None, data|None)."""
    with tarfile.open(path, "w") as tf:
        for name, typ, link, data in entries:
            ti = tarfile.TarInfo(name)
            ti.type = typ
            ti.mode = 0o755 if typ == tarfile.DIRTYPE else 0o644
            if link is not None:
                ti.linkname = link
            if data is not None:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            else:
                tf.addfile(ti)


D, F, S, L = tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE


class CheckPackage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtr-pkg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def check(self, entries, extra=False):
        p = os.path.join(self.tmp, "p.tar")
        make_tar(p, entries)
        REC.check_package(p, extra)

    def test_ok(self):
        self.check([("source/", D, None, None), ("source/a.txt", F, None, b"x"), ("source/sub/", D, None, None),
                    ("source/sub/b.py", F, None, b"y")])
        self.check([("source/", D, None, None), ("source/a.txt", F, None, b"x"), ("source/l1", S, "a.txt", None),
                    ("source/sub/", D, None, None), ("source/sub/l2", S, "../a.txt", None), ("source/l3", S, ".", None)])

    def test_extra(self):
        entries = [("source/", D, None, None), ("extra/", D, None, None), ("extra/run.sh", F, None, b"x")]
        self.check(entries, extra=True)
        with self.assertRaises(ValueError):
            self.check(entries, extra=False)

    def test_refused(self):
        ok = ("source/", D, None, None)
        cases = {
            "absolute": [ok, ("/source/x", F, None, b"x")],
            "absolute etc": [("/etc/passwd", F, None, b"x")],
            "dots": [ok, ("source/../x", F, None, b"x")],
            "dots first": [("../x", F, None, b"x")],
            "empty parts": [ok, ("source//x", F, None, b"x")],
            "outside": [ok, ("other/x", F, None, b"x")],
            "outside file": [("x.txt", F, None, b"x")],
            "symlink absolute": [ok, ("source/l", S, "/etc/passwd", None)],
            "symlink up": [ok, ("source/l", S, "../../x", None)],
            "symlink one level": [ok, ("source/l", S, "..", None)],
            "symlink to extra": [ok, ("extra/", D, None, None), ("source/l", S, "../extra/x", None)],
            "below symlink": [ok, ("source/l", S, ".", None), ("source/l/x", F, None, b"x")],
            "below symlink deep": [ok, ("source/l", S, "sub", None), ("source/l/a/b", F, None, b"x")],
            "hard link": [ok, ("source/a", F, None, b"x"), ("source/h", L, "source/a", None)],
            "device": [ok, ("source/dev", tarfile.CHRTYPE, None, None)],
            "block device": [ok, ("source/dev", tarfile.BLKTYPE, None, None)],
            "fifo": [ok, ("source/pipe", tarfile.FIFOTYPE, None, None)],
        }
        for name, entries in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    self.check(entries)
        with self.subTest("extra without permission"):
            with self.assertRaises(ValueError):
                self.check([ok, ("extra/x", F, None, b"x")])
        with self.subTest("extra symlink outside despite permission"):
            with self.assertRaises(ValueError):
                self.check([ok, ("extra/", D, None, None), ("extra/l", S, "../source/x", None)], extra=True)


class Base(unittest.TestCase):
    cpu_mode = "cpus"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rtr-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        t = self.tmp
        self.machine = os.path.join(t, "machine")
        self.logs = os.path.join(t, "logs")
        self.work = os.path.join(t, "work")
        self.status_dir = os.path.join(t, "status")
        self.dlog = os.path.join(t, "docker.log")
        bin_ = os.path.join(t, "bin")
        os.makedirs(bin_)
        self.docker = os.path.join(bin_, "docker.py")
        self.ssh = os.path.join(bin_, "ssh.py")
        for p, content in ((self.docker, DOCKER_STUB), (self.ssh, SSH_STUB)):
            with open(p, "w") as fh:
                fh.write(content)
        self.profiles = os.path.join(t, "profiles.json")
        with open(self.profiles, "w") as fh:
            json.dump(profiles([PY, self.docker], self.cpu_mode), fh)
        os.chmod(self.profiles, 0o644)
        self.junit = os.path.join(t, "att.xml")
        with open(self.junit, "wb") as fh:
            fh.write(JUNIT_XML)
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith(P):
                del self.env[k]
        self.env.update({
            "RTR_BASE": self.machine, "RTR_PROFILES": self.profiles, "RTR_TICK_S": "1",
            P + "SSH": "%s %s" % (PY, self.ssh), P + "LOGS": self.logs, P + "WORK": self.work, P + "TICK_S": "1",
            P + "STATUS_DIR": self.status_dir, "AIWERKSTATT_PROGRESS_DIR": os.path.join(t, "progress"),
            "ATT_LOG": self.dlog, "ATT_RECEIVER": RECEIVER, "ATT_JUNIT": self.junit, "ATT_STATUS": json.dumps(CALM),
            "ATT_MODE": "green",
        })
        self.repo = self.make_repo(os.path.join(t, "my-repo"))

    def make_repo(self, path):
        os.makedirs(path)
        for n, content in (("README", "hello\n"), ("Dockerfile", "FROM scratch\n"), ("VERSION", "1.2.3\n"),
                           ("requirements.txt", "pytest\n")):
            with open(os.path.join(path, n), "w") as fh:
                fh.write(content)
        for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                                                      "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", path] + cmd, check=True, capture_output=True)
        return path

    def client(self, *args, **env):
        return subprocess.run([PY, CLIENT] + list(args), env=dict(self.env, **env), capture_output=True, text=True,
                              timeout=60, cwd=self.tmp, stdin=subprocess.DEVNULL)

    def receiver(self, command, **env):
        return subprocess.run([PY, RECEIVER], env=dict(self.env, SSH_ORIGINAL_COMMAND=command, **env),
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def docker_calls(self):
        if not os.path.exists(self.dlog):
            return []
        with open(self.dlog) as fh:
            return [json.loads(z) for z in fh if z.strip()]

    def meta(self):
        files = glob.glob(os.path.join(self.logs, "*.json"))
        self.assertEqual(len(files), 1, files)
        with open(files[0]) as fh:
            return json.load(fh)


class EndToEnd(Base):
    def test_green_repo_build(self):
        j = os.path.join(self.tmp, "out.xml")
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--junit", j, "--quiet-build", "-n", "2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("3 passed in 0.1s", p.stdout)
        self.assertIn("...", p.stdout)
        self.assertNotIn("\x1e", p.stdout)
        self.assertNotIn("RTR", p.stdout)
        with open(j, "rb") as fh:
            self.assertEqual(fh.read(), JUNIT_XML)
        m = self.meta()
        self.assertEqual(m["outcome"], "done")
        self.assertEqual(m["rc"], 0)
        res = os.path.join(self.machine, "results", m["id"])
        with open(os.path.join(res, "result.json")) as fh:
            e = json.load(fh)
        self.assertEqual(e["rc"], 0)
        self.assertFalse(e["infra"])
        self.assertRegex(e["commit"], r"^[0-9a-f]{40}$")
        with open(os.path.join(res, "log")) as fh:
            self.assertIn("3 passed in 0.1s", fh.read())
        self.assertTrue(os.path.isfile(os.path.join(res, "junit.xml")))
        self.assertEqual(os.listdir(os.path.join(self.machine, "work")), [])
        with open(os.path.join(self.machine, "log.jsonl")) as fh:
            rows = [z for z in fh if z.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["rc"], 0)
        runs = [a for a in self.docker_calls() if a and a[0] == "run"]
        self.assertEqual(len(runs), 1)
        r = runs[0]
        for flag in ("--rm", "--no-healthcheck", "--read-only", "--cpus"):
            self.assertIn(flag, r)
        self.assertNotIn("--cpuset-cpus", r)
        self.assertEqual(r[r.index("--cpus") + 1], "2")
        self.assertEqual(r[r.index("--network") + 1], "none")
        self.assertEqual(r[r.index("--cap-drop") + 1], "ALL")
        self.assertEqual(r[r.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(r[r.index("--memory") + 1], "4g")
        self.assertEqual(r[r.index("--cpu-shares") + 1], "256")
        self.assertTrue(r[r.index("--name") + 1].startswith("probe-my-repo-"))
        self.assertNotIn("-v", r)                    # a repo build carries the source in the image
        self.assertEqual(r[r.index("-e") + 1], "APP_DATA=/tmp/p")
        self.assertEqual(r[r.index("-n") + 1], "2")
        build = [a for a in self.docker_calls() if a and a[0] == "build"]
        self.assertTrue(build)
        self.assertIn("VERSION=1.2.3", build[0])
        self.assertIn("--cpu-quota", build[0])       # docker build has no --cpus
        self.assertEqual(build[0][build[0].index("--cpu-quota") + 1], "200000")
        self.assertNotIn("/var/run/docker.sock", " ".join(r))

    def test_red(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build", ATT_MODE="red")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("1 failed", p.stdout)
        self.assertNotIn("\x1e", p.stdout)
        self.assertEqual(self.meta()["rc"], 1)

    def test_no_final_status_is_infra_100(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build", ATT_MODE="norc")
        self.assertEqual(p.returncode, 100, p.stderr)
        self.assertIn("run locally", p.stderr)
        self.assertNotIn("\x1e", p.stdout)
        with open(glob.glob(os.path.join(self.machine, "results", "*", "result.json"))[0]) as fh:
            e = json.load(fh)
        self.assertTrue(e["infra"])
        self.assertEqual(e["rc"], 3)

    def test_base_build_mounts_source_read_only(self):
        p = self.client(self.repo, "--profile", "lib", "--mode", "pytest", "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = [a for a in self.docker_calls() if a and a[0] == "run"][0]
        self.assertTrue(r[r.index("-v") + 1].endswith(":/source:ro"))
        self.assertEqual(r[r.index("-w") + 1], "/source")
        self.assertEqual(r.count("-v"), 1)
        for flag in ("--rm", "--no-healthcheck", "--read-only"):
            self.assertIn(flag, r)
        self.assertEqual(r[r.index("--network") + 1], "none")
        build = [a for a in self.docker_calls() if a and a[0] == "build"]
        self.assertEqual(len(build), 1)
        self.assertTrue(any(x.startswith("rtr-base/lib:") for x in build[0]))

    def test_extra_is_mounted_read_only(self):
        extra = self.make_repo(os.path.join(self.tmp, "tooling"))
        p = self.client(self.repo, "--profile", "lib", "--mode", "extras", "--extra", extra, "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = [a for a in self.docker_calls() if a and a[0] == "run"][0]
        mounts = [r[i + 1] for i, x in enumerate(r) if x == "-v"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(":ro") for m in mounts))
        self.assertTrue(any(":/extra:ro" in m for m in mounts))

    def test_refused_argument_gives_100(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build", "--", "--junitxml=/x")
        self.assertEqual(p.returncode, 100, p.stderr)
        self.assertIn("refused", p.stderr)
        self.assertFalse([a for a in self.docker_calls() if a and a[0] in ("build", "run")])

    def test_unknown_profile_gives_100(self):
        p = self.client(self.repo, "--profile", "nope", "--mode", "image")
        self.assertEqual(p.returncode, 100, p.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_no_target_gives_100(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", **{P + "SSH": ""})
        self.assertEqual(p.returncode, 100)
        self.assertIn("no target configured", p.stderr)

    def test_result_again(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        rid = self.meta()["id"]
        e = self.receiver("result %s" % rid)
        self.assertEqual(e.returncode, 0, e.stderr)
        self.assertIn("3 passed in 0.1s", e.stdout)
        self.assertIn('"kind": "end"', e.stdout)
        self.assertNotIn("RTR-RC", e.stdout)

    def test_result_unknown(self):
        for command in ("result unknown-123456", "result ../../etc"):
            with self.subTest(command):
                e = self.receiver(command)
                self.assertEqual(e.returncode, 2)
                self.assertIn('"kind": "error"', e.stdout)

    def test_unknown_command_and_duplicate_id(self):
        for command in ("rm -rf /", "", "run", "status now", "result a b"):
            with self.subTest(command):
                e = self.receiver(command)
                self.assertEqual(e.returncode, 2)
                self.assertIn('"kind": "error"', e.stdout)
        req = enc(good())
        first = subprocess.run([PY, RECEIVER], env=dict(self.env, SSH_ORIGINAL_COMMAND="run " + req),
                               input=b"", capture_output=True, timeout=60)
        self.assertEqual(first.returncode, 3)        # empty package: refused as a package, id is used up
        e = self.receiver("run " + req)
        self.assertEqual(e.returncode, 2)
        self.assertIn("id already used", e.stdout)

    def test_bad_package_is_refused_before_any_docker_call(self):
        req = enc(good())
        bad = os.path.join(self.tmp, "bad.tar")
        make_tar(bad, [("source/", D, None, None), ("source/l", S, "/etc/passwd", None)])
        with open(bad, "rb") as fh:
            p = subprocess.run([PY, RECEIVER], env=dict(self.env, SSH_ORIGINAL_COMMAND="run " + req), stdin=fh,
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 3)
        self.assertIn("link points outside", p.stdout)
        self.assertEqual([a for a in self.docker_calls() if a and a[0] in ("build", "run")], [])


class CpusetMode(Base):
    cpu_mode = "cpuset"

    def test_cpuset_instead_of_cpus(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        r = [a for a in self.docker_calls() if a and a[0] == "run"][0]
        self.assertNotIn("--cpus", r)
        self.assertEqual(r[r.index("--cpuset-cpus") + 1], "0-1")
        build = [a for a in self.docker_calls() if a and a[0] == "build"][0]
        self.assertEqual(build[build.index("--cpuset-cpus") + 1], "0-1")
        self.assertNotIn("--cpu-quota", build)


class QueueTools(Base):
    """Helpers for tests with taken slots and a queue (no tests of its own)."""

    def setUp(self):
        super().setUp()
        self.queue = os.path.join(self.status_dir, "queue")
        self.holders = {}
        self.addCleanup(lambda: [fh.close() for fh in self.holders.values()])

    def hold_slot(self, i, rid):
        slots = os.path.join(self.machine, "slots")
        os.makedirs(slots, exist_ok=True)
        fh = open(os.path.join(slots, "slot%d.lock" % i), "a+")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"id": rid, "repo": "other", "profile": "app", "mode": "image", "start": 1, "slot": 4, "ref": 9}))
        fh.flush()
        self.holders[i] = fh

    def free_slot(self, i):
        self.holders.pop(i).close()

    def start(self, *extra, **env):
        e = dict(self.env, **env)
        if "ATT_STATUS" not in env:
            e.pop("ATT_STATUS", None)                      # real status of the receiver (real slot locks)
        return subprocess.Popen([PY, CLIENT, self.repo, "--profile", "app", "--mode", "image", "--quiet-build"] + list(extra),
                                env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.tmp,
                                stdin=subprocess.DEVNULL)

    def wait_for(self, cond, s=30):
        end = time.time() + s
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def entries(self):
        return sorted(n for n in os.listdir(self.queue) if n.endswith(".json")) if os.path.isdir(self.queue) else []

    def metas(self):
        out = []
        for d in glob.glob(os.path.join(self.logs, "*.json")):
            with open(d) as fh:
                out.append(json.load(fh))
        return out


class Queue(QueueTools):
    """0.24.0: no wait lock any more - free slot -> at once; both slots taken -> FIFO queue without deadline."""

    def test_free_slot_with_high_cpu_starts_at_once(self):
        loaded = dict(CALM, cpu_percent=97.0, load=[7.5, 7, 7], psi_cpu=80.0, test_runs=1)
        t = time.time()
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build", "--wait-max-min", "60",
                        ATT_STATUS=json.dumps(loaded))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertLess(time.time() - t, 20)
        self.assertIn("starting at once anyway", p.stderr)
        self.assertIn("no effect", p.stderr)
        self.assertNotIn("queue position", p.stderr)
        self.assertIn("3 passed", p.stdout)
        self.assertEqual(self.meta()["waited_s"], 0)
        self.assertEqual(self.entries(), [])

    def test_status_fails_gives_100_without_run(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build", ATT_STATUS_FAIL="1")
        self.assertEqual(p.returncode, 100, p.stderr)
        self.assertIn("not reachable", p.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertFalse(glob.glob(os.path.join(self.machine, "results", "*")))

    def test_both_taken_fifo_without_deadline_in_order(self):
        self.hold_slot(0, "other-111111")
        self.hold_slot(1, "other-222222")
        a = self.start("--wait-max-min", "0")             # deadline 0 used to mean exit 100 at once
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 1), "A not queued")
        b = self.start()
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 2), "B not queued")
        c = self.start()
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 3), "C not queued")
        time.sleep(3)                                      # several ticks: nobody aborts, nobody starts
        self.assertEqual([x.poll() for x in (a, b, c)], [None, None, None])
        self.assertEqual([x for x in self.docker_calls() if x and x[0] == "run"], [])
        self.free_slot(0)                                  # one slot free -> only A (the first) starts, then B, then C
        for x in (a, b, c):
            self.assertTrue(self.wait_for(lambda: x.poll() is not None))
        err = [x.communicate()[1] for x in (a, b, c)]
        self.assertEqual([x.returncode for x in (a, b, c)], [0, 0, 0], err)
        self.assertIn("queue position 1, ahead of you: running other/image (slot 4 / #9)", err[0])
        self.assertIn("expected free in ~", err[0])
        self.assertIn("queue position 2, ahead of you: my-repo/image", err[1])
        self.assertIn("queue position 3, ahead of you: my-repo/image, my-repo/image", err[2])
        started = sorted(self.metas(), key=lambda m: m["started"])
        ids = [re.search(r"run (\S+)\)", e).group(1) for e in err]
        self.assertEqual([m["id"] for m in started], ids)
        self.assertEqual(self.entries(), [])

    def test_busy_despite_free_status_stays_in_queue(self):
        self.hold_slot(0, "other-111111")
        self.hold_slot(1, "other-222222")
        p = self.start(ATT_STATUS=json.dumps(CALM))
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 1))
        time.sleep(3)
        self.assertIsNone(p.poll())
        self.free_slot(0)
        self.assertTrue(self.wait_for(lambda: p.poll() is not None))
        err = p.communicate()[1]
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("fully booked after all", err)
        self.assertEqual(self.entries(), [])

    def test_receiver_refuses_a_third_run(self):
        self.hold_slot(0, "other-111111")
        self.hold_slot(1, "other-222222")
        e = self.receiver("run %s" % enc(good()))
        self.assertEqual(e.returncode, 75)
        self.assertIn('"kind": "busy"', e.stdout)

    def test_third_run_is_refused_even_if_setting_says_more(self):
        d = profiles([PY, self.docker])
        d["settings"]["max_runs"] = 9
        with open(self.profiles, "w") as fh:
            json.dump(d, fh)
        self.hold_slot(0, "other-111111")
        self.hold_slot(1, "other-222222")
        e = self.receiver("run %s" % enc(good()))
        self.assertEqual(e.returncode, 75)
        self.assertFalse(os.path.exists(os.path.join(self.machine, "slots", "slot2.lock")))

    def test_orphan_entries_are_removed(self):
        os.makedirs(self.queue)
        dead = subprocess.Popen([PY, "-c", "pass"])
        dead.wait()
        reused = {"id": "x-reused", "pid": os.getpid(), "pid_start": -1, "since": 1, "repo": "r"}
        alive = {"id": "x-alive-123456", "pid": os.getpid(), "pid_start": CLI._pid_start(os.getpid()), "since": 1, "repo": "r"}
        for n, d in (("%020d-a.json" % 1, {"id": "x-dead-123456", "pid": dead.pid, "pid_start": 5, "since": 1}),
                     ("%020d-b.json" % 2, reused), ("%020d-c.json" % 3, alive), ("%020d-d.json" % 4, "broken")):
            with open(os.path.join(self.queue, n), "w") as fh:
                fh.write(json.dumps(d) if isinstance(d, dict) else d)
        with mock.patch.object(CLI, "QUEUE", self.queue):
            self.assertEqual([x["id"] for x in CLI.queue_read()], ["x-alive-123456"])
            self.assertEqual(self.entries(), ["%020d-c.json" % 3, "%020d-d.json" % 4])   # half written stays 60 s
            q = CLI.queue_summary()
            self.assertEqual((q["length"], q["waiting"][0]["position"]), (1, 1))

    def test_estimate_from_protocols(self):
        os.makedirs(self.logs, exist_ok=True)
        for i, d in enumerate((600, 900, 1200)):
            with open(os.path.join(self.logs, "%d.json" % i), "w") as fh:
                json.dump({"outcome": "done", "repo": "r", "profile": "app", "mode": "image", "total_s": d + 100,
                           "waited_s": 100}, fh)
        with mock.patch.object(CLI, "LOGS", self.logs):
            self.assertEqual(CLI.typical_duration_s("r", "app", "image"), 900)
            self.assertEqual(CLI.typical_duration_s("x", "app", "image"), CLI.DEFAULT_DURATION_S)
            st = {"max_runs": 2, "runs": [{"repo": "r", "profile": "app", "mode": "image", "runtime_s": 300},
                                          {"repo": "r", "profile": "app", "mode": "image", "runtime_s": 800}]}
            self.assertEqual(CLI.free_in_s(st, []), 100)
            self.assertEqual(CLI.free_in_s(st, [{"repo": "r", "profile": "app", "mode": "image"}]), 600)

    def test_collector_shows_the_queue(self):
        os.makedirs(self.queue)
        with open(os.path.join(self.queue, "%020d-x.json" % 1), "w") as fh:
            json.dump({"id": "w-123456", "pid": os.getpid(), "pid_start": CLI._pid_start(os.getpid()),
                       "since": int(time.time()) - 90, "repo": "lib", "profile": "lib", "mode": "pytest", "slot": 2, "ref": 77}, fh)
        p = self.client("--collect", "--once")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.status_dir, "status.json")) as fh:
            st = json.load(fh)
        w = st["queue"]["waiting"][0]
        self.assertEqual((st["queue"]["length"], w["position"], w["repo"], w["slot"], w["ref"]), (1, 1, "lib", 2, 77))
        self.assertGreaterEqual(w["since_s"], 90)


class Progress(QueueTools):
    """0.25.0: progress file for the remaining-time display (backend/eta.py): queue -> test, own file only without a
    parent run, a missing module changes nothing."""

    def setUp(self):
        super().setUp()
        self.progress = self.env["AIWERKSTATT_PROGRESS_DIR"]

    def history(self):
        out = []
        for n in glob.glob(os.path.join(self.progress, "history-*.jsonl")):
            with open(n) as fh:
                out += [json.loads(z) for z in fh if z.strip()]
        return out

    def run_files(self):
        return glob.glob(os.path.join(self.progress, "remote_test-*.json"))

    def test_free_slot_writes_queue_then_test_and_history(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        files = self.run_files()
        self.assertEqual(len(files), 1)
        with open(files[0]) as fh:
            d = json.load(fh)
        self.assertEqual(d["format"], "aiwerkstatt-progress/1")
        self.assertEqual(d["result"], "ok")
        self.assertEqual([p["name"] for p in d["phases"]], ["queue", "test"])
        self.assertEqual(d["title"], "my-repo/image")
        h = self.history()
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["kind"], "remote_test")
        self.assertIn("test", h[0]["phases"])

    def test_queue_phase_has_expected_wait(self):
        self.hold_slot(0, "other-111111")
        self.hold_slot(1, "other-222222")
        a = self.start()
        self.assertTrue(self.wait_for(lambda: len(self.entries()) == 1), "not queued")

        def expected():
            for f in self.run_files():
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except ValueError:
                    continue
                ph = d["phases"][0]
                if d.get("current") == 0 and ph.get("expected_s") is not None:
                    return True
            return False
        self.assertTrue(self.wait_for(expected), "no expected wait written")
        self.free_slot(0)
        self.assertTrue(self.wait_for(lambda: a.poll() is not None))
        self.assertEqual(a.returncode, 0, a.communicate()[1])

    def test_parent_run_means_no_own_file(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build",
                        AIWERKSTATT_PROGRESS_PARENT="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.run_files(), [])
        self.assertEqual(self.history(), [])

    def test_unwritable_folder_changes_nothing(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--quiet-build",
                        AIWERKSTATT_PROGRESS_DIR="/proc/no-folder-possible")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_missing_module_changes_nothing(self):
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        shutil.copy(CLIENT, os.path.join(lone, "remote_test.py"))     # no eta.py next to it, none in ../../backend
        p = subprocess.run([PY, os.path.join(lone, "remote_test.py"), self.repo, "--profile", "app", "--mode", "image",
                            "--quiet-build"], env=self.env, capture_output=True, text=True, timeout=60, cwd=self.tmp,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.run_files(), [])


class Collector(Base):
    def test_ok_and_outage(self):
        p = self.client("--collect", "--once")
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.status_dir, "status.json")) as fh:
            a = json.load(fh)
        self.assertTrue(a["ok"])
        for k in ("load", "cores", "test_runs", "runs", "max_runs", "stand"):
            self.assertIn(k, a)
        self.assertEqual(a["cores"], 4)
        days = glob.glob(os.path.join(self.status_dir, "*.jsonl"))
        self.assertEqual(len(days), 1)
        with open(days[0]) as fh:
            rows = [json.loads(z) for z in fh if z.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cores"], 4)
        stand = a["stand"]
        p = self.client("--collect", "--once", ATT_STATUS_FAIL="1")
        self.assertEqual(p.returncode, 100)
        with open(os.path.join(self.status_dir, "status.json")) as fh:
            b = json.load(fh)
        self.assertFalse(b["ok"])
        self.assertTrue(b["error"])
        self.assertEqual(b["cores"], 4)
        self.assertEqual(b["stand"], stand)
        self.assertEqual(b["load"], a["load"])

    def test_old_day_files_are_removed(self):
        os.makedirs(self.status_dir)
        old = os.path.join(self.status_dir, "2020-01-01.jsonl")
        keep = os.path.join(self.status_dir, "notes.txt")
        for p in (old, keep):
            with open(p, "w") as fh:
                fh.write("x\n")
            os.utime(p, (1, 1))
        self.assertEqual(self.client("--collect", "--once").returncode, 0)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(keep))

    def test_unavailable_reason(self):
        with mock.patch.dict(os.environ, {P + "SSH": "x"}):
            os.environ.pop(P + "OFF", None)
            self.assertEqual(CLI.unavailable_reason(), "")
        with mock.patch.dict(os.environ, {P + "SSH": "x", P + "OFF": "1"}):
            self.assertIn("switched off", CLI.unavailable_reason())
        self.assertEqual(self.client("--available", **{P + "OFF": "1"}).returncode, 100)
        self.assertEqual(self.client("--available").returncode, 0)

    def test_config_file_without_default_host(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in list(os.environ):
                if k.startswith(P):
                    del os.environ[k]
            with mock.patch.object(CLI, "CONFIG", os.path.join(self.tmp, "none.json")):
                self.assertEqual(CLI.settings(), (None, None))
                self.assertIn("no target", CLI.unavailable_reason())
            cfg = os.path.join(self.tmp, "c.json")
            with open(cfg, "w") as fh:
                json.dump({"target": "u@h", "key": "~/k"}, fh)
            with mock.patch.object(CLI, "CONFIG", cfg):
                t, k = CLI.settings()
                self.assertEqual(t, "u@h")
                self.assertTrue(k.endswith("/k") and not k.startswith("~"))
                self.assertIn("key missing", CLI.unavailable_reason())


class Busy(unittest.TestCase):
    def test_calm(self):
        self.assertEqual(CLI.busy_reason(CALM), "")
        self.assertEqual(CLI.busy_reason({}), "")

    def test_reasons(self):
        cases = {"run": dict(CALM, test_runs=1), "cpu": dict(CALM, cpu_percent=70.0),
                 "load": dict(CALM, load=[3.0, 0, 0], cores=4), "psi": dict(CALM, psi_cpu=40.0)}
        for name, st in cases.items():
            with self.subTest(name):
                self.assertNotEqual(CLI.busy_reason(st), "")
        for name, st in {"cpu close": dict(CALM, cpu_percent=69.9), "load close": dict(CALM, load=[2.9, 0, 0]),
                         "psi close": dict(CALM, psi_cpu=39.9)}.items():
            with self.subTest(name):
                self.assertEqual(CLI.busy_reason(st), "")


class StatusCommand(Base):
    def test_keys_without_container_names(self):
        e = self.receiver("status", ATT_PS="1")
        self.assertEqual(e.returncode, 0, e.stderr)
        d = json.loads(e.stdout.strip().splitlines()[-1])
        self.assertEqual(set(d), {"load", "cores", "psi_cpu", "psi_io", "ram_free_gb", "cpu_percent", "test_runs", "runs",
                                  "max_runs", "time"})
        self.assertEqual(len(d["load"]), 3)
        self.assertEqual(d["test_runs"], 0)
        self.assertEqual(d["runs"], [])
        self.assertEqual(d["max_runs"], 2)
        self.assertGreaterEqual(d["cores"], 1)
        self.assertNotIn("some-other-service", e.stdout)
        self.assertNotIn("unhealthy", e.stdout)
        self.assertEqual(self.docker_calls(), [])    # status never touches docker

    def test_status_works_without_profiles(self):
        os.remove(self.profiles)
        e = self.receiver("status")
        self.assertEqual(e.returncode, 0, e.stderr)
        self.assertEqual(json.loads(e.stdout.strip().splitlines()[-1])["max_runs"], 2)

    def test_running_visible(self):
        slots = os.path.join(self.machine, "slots")
        os.makedirs(slots)
        fh = open(os.path.join(slots, "slot0.lock"), "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(json.dumps({"id": "x-123456", "repo": "r", "profile": "app", "mode": "image", "start": 1, "slot": 2,
                                 "ref": 3}))
            fh.flush()
            d = json.loads(self.receiver("status").stdout.strip().splitlines()[-1])
            self.assertEqual(d["test_runs"], 1)
            self.assertEqual(d["runs"][0]["id"], "x-123456")
        finally:
            fh.close()


NEEDED_DOCKER_STUB = '''import base64, json, os, sys
a = sys.argv[1:]
with open(os.environ["ATT_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
c = a[0] if a else ""
if c == "ps":
    sys.exit(0)
if c == "image":
    if "--format" in a:
        print("sha256:abc")
        sys.exit(0)
    sys.exit(1)
if c == "build":
    print("Successfully built abc")
    sys.exit(0)
if c in ("tag", "rmi", "images", "stop"):
    sys.exit(0)
if c == "run":
    rest = a[a.index("pytest") + 1:]
    xdist = "-n" in rest
    spec = json.loads(os.environ["ATT_SPEC"])
    red = set(json.loads(os.environ.get("ATT_RED", "[]")))
    flaky = set(json.loads(os.environ.get("ATT_FLAKY", "[]")))
    deselect = {x.split("=", 1)[1] for x in rest if x.startswith("--deselect=")}
    targets = [x for x in rest if x.startswith("tests")]
    ids = []
    for t in targets:
        if "::" in t:
            ids.append(t)
        else:
            ids += ["%s::%s" % (t, n) for n in spec[t]]
    ids = [i for i in ids if i not in deselect]
    cases, rc = [], 0
    for k, i in enumerate(ids):
        bad = i in red and not (i in flaky and not xdist)
        rc = 1 if bad else rc
        word = "FAILED" if bad else "PASSED"
        pct = (k + 1) * 100 // len(ids)
        print(("[gw0] [%3d%%] %s %s" % (pct, word, i)) if xdist else ("%s %s  [%3d%%]" % (i, word, pct)))
        f, n = i.split("::", 1)
        cases.append('<testcase classname="%s" name="%s" time="0.1">%s</testcase>' % (
            f[:-3].replace("/", "."), n, '<failure message="boom">x</failure>' if bad else ""))
    xml = "<testsuite>%s</testsuite>" % "".join(cases)
    print("done")
    print("\\x1eRTR-JUNIT-BEGIN")
    print(base64.b64encode(xml.encode()).decode())
    print("\\x1eRTR-JUNIT-END")
    print("\\x1eRTR-RC %d" % rc)
    sys.exit(rc)
sys.exit(0)
'''


class OnlyNeeded(Base):
    """--only-needed: plan through the result store, retry red tests serially, summary with reused + new."""

    def setUp(self):
        super().setUp()
        with open(self.docker, "w") as fh:
            fh.write(NEEDED_DOCKER_STUB)
        self.store_dir = os.path.join(self.tmp, "results")
        self.env[ "AIWERKSTATT_RESULT_STORE_DIR"] = self.store_dir
        for rel, text in (("tests/test_a.py", "def test_one(): pass\n"), ("tests/test_b.py", "def test_two(): pass\n"),
                          ("app/mod.py", "X = 1\n")):
            p = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(text)
        self.commit("tests")
        self.env["ATT_SPEC"] = json.dumps({"tests/test_a.py": ["test_one"], "tests/test_b.py": ["test_two"]})

    def commit(self, msg):
        for cmd in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q",
                                    "-m", msg]):
            subprocess.run(["git", "-C", self.repo] + cmd, check=True, capture_output=True)

    def run_args(self):
        return [a for a in self.docker_calls() if a and a[0] == "run"]

    def test_flaky_then_green_reused_next_time(self):
        red = json.dumps(["tests/test_b.py::test_two"])
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--only-needed", "--quiet-build",
                        ATT_RED=red, ATT_FLAKY=red)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("2 passed (0 reused, 2 new), 1 flaky", p.stdout)
        runs = self.run_args()
        self.assertEqual(len(runs), 2)                                   # parallel run + one serial retry
        self.assertIn("-vv", runs[0])
        self.assertIn("-n", runs[0])
        self.assertNotIn("-n", runs[1])
        self.assertIn("tests/test_b.py::test_two", runs[1])
        # second request on the same commit: everything is reused, nothing runs
        before = len(self.run_args())
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--only-needed", "--quiet-build")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("2 passed (2 reused, 0 new)", p.stdout)
        self.assertEqual(len(self.run_args()), before)

    def test_stably_red_retried_twice_and_fails(self):
        red = json.dumps(["tests/test_b.py::test_two"])
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--only-needed", "--quiet-build", ATT_RED=red)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(len(self.run_args()), 3)                        # parallel + two serial retries
        self.assertIn("1 failed, 1 passed (0 reused, 1 new)", p.stdout)

    def test_explicit_arguments_run_normally(self):
        p = self.client(self.repo, "--profile", "app", "--mode", "image", "--only-needed", "--quiet-build", "--", "tests/test_a.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("running normally", p.stderr)
        self.assertNotIn("-vv", self.run_args()[0])


class Install(unittest.TestCase):
    def test_script_has_no_default_hosts_and_valid_syntax(self):
        path = os.path.join(HERE, "install.sh")
        self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0)
        with open(path) as fh:
            text = fh.read()
        self.assertIn('RTR_TARGET:?', text)
        self.assertIn('RTR_FROM:?', text)
        p = subprocess.run(["bash", path], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("RTR_TARGET", p.stderr)


if __name__ == "__main__":
    unittest.main()
