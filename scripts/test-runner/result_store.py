#!/usr/bin/env python3
"""result_store.py - per-test result store: "green stays green" (AIWerkstatt 0.30.0).

A result belongs to (project, environment, test id) and to the commit it was measured on. A later run reuses a GREEN
result only if nothing the test depends on changed between that commit and the current one:
  * same commit -> valid;
  * only test-side files changed (everything below the tests folder) -> valid, except for the changed test files
    themselves and for tests that import a changed test helper (a changed conftest.py never invalidates others);
  * app-side files changed -> the affected test files run again. Rule: an optional project selector command
    (`--selector "<command>"`) receives the changed app files as arguments and prints the affected test files, one per
    line, or the word FULL; without a selector the default rule follows Python imports (transitively), and any non-Python
    app file means everything runs again. "When in doubt, run more."
  * results older than MAX_AGE_S (7 days) run again.
Red, aborted and never-run tests ALWAYS run. The environment (for example "image+FLAG=0") is never mixed.

Which test ids a file has is learned from the last run that executed the WHOLE file in that environment (junit report).
Partial collections (deselected tests) therefore do not count.

Storage: one SQLite file per project in $AIWERKSTATT_RESULT_STORE_DIR (default ~/.local/share/aiwerkstatt-results/
<project>.sqlite), WAL, every result is committed at once - if a run is cut off (session end, out of memory, restart,
time limit) everything measured so far is safe and the next run continues exactly there (already green tests of the
current commit are deselected with --deselect).

Flaky ("wobbler"): red in the run, green in the serial retry -> the result is stored green (serial) plus a row in the
`flaky` table. Never silent.

    result_store.py ingest --project P --env E --commit C --run R --junit FILE [--repo R] [--whole | --targets T ...]
                           [--kind parallel|serial] [--attempt N] [--time ISO]
    result_store.py plan   --project P --env E --repo R [--selector "<command>"] [--tests-dir tests] [--json]
    result_store.py status --project P [--env E] [--red]
    result_store.py flaky  --project P [--hours 24] [--json]
    result_store.py report [--hours 24]       # all projects: runs, reused/new, flaky, stably red (nightly summary)

Standard library only."""
import argparse
import ast
import datetime
import fnmatch
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

VERSION = "1.0.0"
MAX_AGE_S = int(os.environ.get("AIWERKSTATT_RESULT_STORE_MAX_AGE_S", str(7 * 24 * 3600)))
GREEN = ("passed", "skipped")
RED = ("failed", "error")
RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
COMMIT_RX = re.compile(r"^[0-9a-f]{40}$")
MAX_TARGETS = int(os.environ.get("AIWERKSTATT_RESULT_STORE_MAX_TARGETS", "380"))   # the runner takes at most 400 arguments
MAX_TARGET_LEN = 390                                                                 # ... of at most 400 characters each
# pytest -v: "[gw3] [ 12%] PASSED tests/x.py::t[a]" (xdist) or "tests/x.py::t[a] PASSED  [ 12%]" (serial)
_KIND = r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
RX_V_XDIST = re.compile(r"^\[gw\d+\] \[\s*\d+%\] " + _KIND + r" (\S.*?)\s*$")
RX_V_SERIAL = re.compile(r"^(\S+?::\S.*?) " + _KIND + r"(?: \(.*\))?\s+\[\s*\d+%\]\s*$")
KIND_OUTCOME = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error", "SKIPPED": "skipped", "XFAIL": "skipped", "XPASS": "passed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (env TEXT NOT NULL, test_id TEXT NOT NULL, file TEXT, outcome TEXT, commit_ TEXT, run TEXT,
  kind TEXT, attempt INTEGER, duration_s REAL, ts REAL, last TEXT, message TEXT, PRIMARY KEY (env, test_id));
CREATE TABLE IF NOT EXISTS history (nr INTEGER PRIMARY KEY, env TEXT, test_id TEXT, file TEXT, outcome TEXT, commit_ TEXT,
  run TEXT, kind TEXT, attempt INTEGER, duration_s REAL, ts REAL, last TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS history_run ON history (run);
CREATE TABLE IF NOT EXISTS files (env TEXT NOT NULL, file TEXT NOT NULL, commit_ TEXT, ids TEXT, ts REAL, run TEXT,
  PRIMARY KEY (env, file));
CREATE TABLE IF NOT EXISTS flaky (nr INTEGER PRIMARY KEY, env TEXT, test_id TEXT, commit_ TEXT, run TEXT, ts REAL,
  kind TEXT, attempt INTEGER, last TEXT);
CREATE TABLE IF NOT EXISTS runs (run TEXT PRIMARY KEY, env TEXT, commit_ TEXT, start REAL, finish REAL, status TEXT, info TEXT);
"""


def base_dir():
    return os.environ.get("AIWERKSTATT_RESULT_STORE_DIR") or os.path.join(
        os.path.expanduser("~"), ".local", "share", "aiwerkstatt-results")


def _now():
    return time.time()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else "-"


class Store:
    """Result store of one project. Every method commits at once (abort-safe)."""

    def __init__(self, project, directory=None):
        if not NAME_RX.match(project or ""):
            raise ValueError("invalid project name: %r" % project)
        self.project = project
        d = directory or base_dir()
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
        self.path = os.path.join(d, "%s.sqlite" % project)
        fresh = not os.path.exists(self.path)
        self.db = sqlite3.connect(self.path, timeout=120, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        if fresh:
            try:
                os.chmod(self.path, 0o644)
            except OSError:
                pass

    def close(self):
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ write
    def run_begin(self, run, env, commit, info=None):
        self.db.execute("INSERT OR REPLACE INTO runs (run, env, commit_, start, finish, status, info) VALUES (?,?,?,?,?,?,?)",
                        (run, env, commit, _now(), None, "running", json.dumps(info or {}, ensure_ascii=False)))

    def run_end(self, run, status, info=None):
        old = self.db.execute("SELECT info FROM runs WHERE run=?", (run,)).fetchone()
        d = {}
        if old and old["info"]:
            try:
                d = json.loads(old["info"])
            except ValueError:
                d = {}
        d.update(info or {})
        self.db.execute("UPDATE runs SET finish=?, status=?, info=? WHERE run=?", (_now(), status, json.dumps(d, ensure_ascii=False), run))

    def record(self, env, test_id, outcome, commit, run, file=None, kind="parallel", attempt=0, duration_s=None,
               ts=None, last=None, message=None):
        """Commit one result. `state` keeps the newest per (environment, test); an older timestamp never overwrites."""
        if outcome not in RANK:
            raise ValueError("unknown outcome: %r" % outcome)
        ts = ts or _now()
        file = file or test_id.split("::")[0]
        message = (message or "")[:300] or None
        last_json = json.dumps(last, ensure_ascii=False) if isinstance(last, dict) else last
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO history (env, test_id, file, outcome, commit_, run, kind, attempt, duration_s, ts, last, message)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (env, test_id, file, outcome, commit, run, kind, attempt, duration_s, ts, last_json, message))
            old = self.db.execute("SELECT ts, outcome, run FROM state WHERE env=? AND test_id=?", (env, test_id)).fetchone()
            if old is None or (old["ts"] or 0) <= ts:
                self.db.execute("INSERT OR REPLACE INTO state (env, test_id, file, outcome, commit_, run, kind, attempt, duration_s, ts,"
                                " last, message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (env, test_id, file, outcome, commit, run, kind, attempt, duration_s, ts, last_json, message))
            if kind == "serial" and outcome in GREEN and old is not None and old["outcome"] in RED and old["run"] == run:
                self.db.execute("INSERT INTO flaky (env, test_id, commit_, run, ts, kind, attempt, last) VALUES (?,?,?,?,?,?,?,?)",
                                (env, test_id, commit, run, ts, "serial_green", attempt, last_json))

    def file_complete(self, env, file, ids, commit, run, ts=None):
        """The WHOLE file ran completely in this run -> remember its test ids in this environment."""
        self.db.execute("INSERT OR REPLACE INTO files (env, file, commit_, ids, ts, run) VALUES (?,?,?,?,?,?)",
                        (env, file, commit, json.dumps(sorted(set(ids)), ensure_ascii=False), ts or _now(), run))

    def ingest_junit(self, xml, env, commit, run, test_files, whole_files=(), kind="parallel", attempt=0, ts=None, last=None):
        """Take over a junit report. whole_files = files this run executed completely (the whole tests folder or the file).
        -> {passed, failed, error, skipped, total, red: [ids]}"""
        cases = junit_read(xml, test_files)
        ts = ts or _now()
        count = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        red = []
        per_file = {}
        for tid, file, outcome, duration, message in cases:
            self.record(env, tid, outcome, commit, run, file=file, kind=kind, attempt=attempt, duration_s=duration, ts=ts,
                        last=last, message=message)
            count[outcome] += 1
            if outcome in RED:
                red.append(tid)
            per_file.setdefault(file, []).append(tid)
        for file in whole_files:
            self.file_complete(env, file, per_file.get(file, []), commit, run, ts=ts)
        count.update(total=len(cases), red=red)
        return count

    # ------------------------------------------------------------------ read
    def state_of(self, env):
        return {r["test_id"]: dict(r) for r in self.db.execute("SELECT * FROM state WHERE env=?", (env,))}

    def files_of(self, env):
        out = {}
        for r in self.db.execute("SELECT * FROM files WHERE env=?", (env,)):
            d = dict(r)
            try:
                d["ids"] = json.loads(d["ids"] or "[]")
            except ValueError:
                d["ids"] = []
            out[r["file"]] = d
        return out

    def run_results(self, run, env=None):
        """Newest result per test inside one run -> {test_id: row}."""
        q = "SELECT * FROM history WHERE run=?" + (" AND env=?" if env else "") + " ORDER BY nr"
        out = {}
        for r in self.db.execute(q, (run, env) if env else (run,)):
            out[r["test_id"]] = dict(r)
        return out

    def flaky_since(self, since_ts, env=None):
        q = "SELECT * FROM flaky WHERE ts>=?" + (" AND env=?" if env else "") + " ORDER BY ts"
        return [dict(r) for r in self.db.execute(q, (since_ts, env) if env else (since_ts,))]

    def runs_since(self, since_ts):
        return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE start>=? ORDER BY start", (since_ts,))]


# ---------------------------------------------------------------------- junit + pytest output
def _node_id(classname, name, test_files):
    """junit (xunit2) has no file path: classname "tests.test_x[.Class]" -> longest prefix that is a test file."""
    parts = (classname or "").split(".")
    for k in range(len(parts), 0, -1):
        candidate = "/".join(parts[:k]) + ".py"
        if candidate in test_files:
            return "::".join([candidate] + parts[k:] + [name]), candidate
    candidate = "/".join(parts) + ".py" if parts and parts[0] else ""
    return ("%s::%s" % (candidate, name) if candidate else name), (candidate or name.split("::")[0])


def junit_read(xml, test_files=()):
    """-> [(test_id, file, outcome, duration_s, message)], the worst outcome per test (an error in teardown counts)."""
    if isinstance(xml, (bytes, str)) and not (isinstance(xml, str) and os.path.exists(xml)):
        root = ET.fromstring(xml)
    else:
        root = ET.parse(xml).getroot()
    known = set(test_files or ())
    out = {}
    for tc in root.iter("testcase"):
        outcome, message = "passed", None
        for child in tc:
            if child.tag == "failure" and RANK["failed"] > RANK[outcome]:
                outcome, message = "failed", child.get("message")
            elif child.tag == "error" and RANK["error"] > RANK[outcome]:
                outcome, message = "error", child.get("message")
            elif child.tag == "skipped" and RANK["skipped"] > RANK[outcome]:
                outcome, message = "skipped", child.get("message")
        tid, file = _node_id(tc.get("classname"), tc.get("name") or "", known)
        try:
            duration = float(tc.get("time") or 0)
        except ValueError:
            duration = None
        old = out.get(tid)
        if old is None or RANK[outcome] > RANK[old[2]]:
            out[tid] = (tid, file, outcome, (duration or 0) + ((old[3] or 0) if old else 0), message)
        else:
            out[tid] = (old[0], old[1], old[2], (old[3] or 0) + (duration or 0), old[4])
    return list(out.values())


def output_line(line):
    """pytest -v result line -> (test_id, outcome) or None."""
    line = line.rstrip("\n")
    m = RX_V_XDIST.match(line)
    if m:
        return m.group(2).strip(), KIND_OUTCOME[m.group(1)]
    m = RX_V_SERIAL.match(line)
    if m:
        return m.group(1).strip(), KIND_OUTCOME[m.group(2)]
    return None


# ---------------------------------------------------------------------- dependencies
def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return p.returncode, p.stdout


def list_test_files(repo, commit="HEAD", folder="tests", patterns=("test_*.py", "*_test.py")):
    rc, out = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", folder)
    if rc != 0:
        return []
    return sorted(f for f in out.splitlines() if any(fnmatch.fnmatch(os.path.basename(f), m) for m in patterns))


def _imports(path):
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), path)
    except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
        return None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
            names.add(node.module)
            names.update("%s.%s" % (node.module, a.name) for a in node.names)
    return names


class Dependencies:
    """Which test files are affected between an old commit and the current state (work tree `repo` checked out at `commit`)?
    affected(old) -> set(test files) or None (= everything). Cached per old commit.

    selector: optional shell-style command; it gets the changed app-side files as arguments (cwd = repo) and prints the
    affected test files, one per line, or the word FULL. Failure or empty output means everything."""

    def __init__(self, repo, commit, tests_dir="tests", selector=None, patterns=("test_*.py", "*_test.py")):
        self.repo, self.commit, self.tests_dir, self.patterns = repo, commit, tests_dir.rstrip("/"), patterns
        self.tests = list_test_files(repo, commit, self.tests_dir, patterns)
        self.selector = selector or os.environ.get("AIWERKSTATT_RESULT_STORE_SELECTOR") or None
        self._cache = {}
        self._imports = {}
        self.reasons = {}

    def changed(self, old):
        if old == self.commit:
            return []
        if not COMMIT_RX.match(old or ""):
            return None
        rc, _ = _git(self.repo, "cat-file", "-e", old + "^{commit}")
        if rc != 0:
            return None
        rc, out = _git(self.repo, "diff", "--name-only", old, self.commit)
        return [z for z in out.splitlines() if z.strip()] if rc == 0 else None

    def _is_test(self, f):
        return f.startswith(self.tests_dir + "/") and any(fnmatch.fnmatch(os.path.basename(f), m) for m in self.patterns)

    def _test_side(self, f):
        return f.startswith(self.tests_dir + "/")

    def _importers(self, module_names):
        """Test files that import one of the modules (directly)."""
        out = set()
        for t in self.tests:
            if t not in self._imports:
                self._imports[t] = _imports(os.path.join(self.repo, t))
            imp = self._imports[t]
            if imp is None or imp & module_names:
                out.add(t)
        return out

    def _app_rule(self, files):
        """Rule for app-side files -> set, or None (= everything)."""
        if not files:
            return set()
        if self.selector:
            try:
                p = subprocess.run(shlex.split(self.selector) + list(files), capture_output=True, text=True, cwd=self.repo,
                                   stdin=subprocess.DEVNULL, timeout=600)
            except (OSError, subprocess.SubprocessError, ValueError):
                return None
            if p.returncode != 0:
                return None
            lines = [z.strip() for z in p.stdout.splitlines() if z.strip()]
            if not lines or "FULL" in lines:
                return None
            return {z for z in lines if z.endswith(".py")}
        # default: Python modules only, transitively through imports of the repo's own modules; anything else -> everything
        if any(not f.endswith(".py") for f in files):
            return None
        changed = {os.path.splitext(f)[0].replace("/", ".") for f in files}
        names = set(changed) | {g.split(".")[-1] for g in changed}
        rc, out = _git(self.repo, "ls-tree", "-r", "--name-only", self.commit)
        modules = [f for f in out.splitlines() if f.endswith(".py") and not self._test_side(f)] if rc == 0 else []
        imp = {m: _imports(os.path.join(self.repo, m)) for m in modules}
        for _ in range(20):
            new = set()
            for m, i in imp.items():
                name = os.path.splitext(m)[0].replace("/", ".")
                if name in names or name.split(".")[-1] in names:
                    continue
                if i is None or i & names:
                    new.update({name, name.split(".")[-1]})
            if not new:
                break
            names |= new
        return self._importers(names)

    def affected(self, old):
        if old in self._cache:
            return self._cache[old]
        files = self.changed(old)
        if files is None:
            result, reason = None, "commit %s unknown" % (old or "?")[:12]
        else:
            test_side = [f for f in files if self._test_side(f)]
            app_side = [f for f in files if not self._test_side(f)]
            result = {f for f in test_side if self._is_test(f)}
            helpers = {os.path.splitext(os.path.basename(f))[0] for f in test_side
                       if f.endswith(".py") and not self._is_test(f) and os.path.basename(f) != "conftest.py"}
            if helpers:
                result |= self._importers(helpers | {"%s.%s" % (self.tests_dir.replace("/", "."), h) for h in helpers})
            app = self._app_rule(app_side)
            if app is None:
                result, reason = None, "app files changed (%s) -> everything" % ", ".join(app_side[:4])
            else:
                result |= app
                reason = "%d files changed (%d test-side, %d app-side) -> %d test files affected" % (
                    len(files), len(test_side), len(app_side), len(result))
        self._cache[old] = result
        self.reasons[old] = reason
        return result


# ---------------------------------------------------------------------- planning
class Plan:
    def __init__(self, env, commit):
        self.env, self.commit = env, commit
        self.green = {}           # test_id -> state row (reused)
        self.whole = []           # test files that run completely
        self.single = []          # single test ids
        self.reasons = {}         # file|test_id -> reason
        self.red_before = []      # tests that were red before and run now
        self.sources = {}         # run -> number of reused results
        self.deselect = []        # already green on THIS commit inside whole files (resume) -> --deselect

    @property
    def empty(self):
        return not self.whole and not self.single

    def count_new(self, known_ids=None):
        return len(self.single) + sum(len((known_ids or {}).get(d, ())) or 1 for d in self.whole)

    def targets(self, max_targets=None):
        """pytest targets: whole files + single tests; too many or too long ids collapse to their file."""
        max_targets = MAX_TARGETS if max_targets is None else max_targets
        whole = list(self.whole)
        single = []
        for tid in self.single:
            file = tid.split("::")[0]
            if file in whole:
                continue
            if len(tid) > MAX_TARGET_LEN or "\n" in tid:
                whole.append(file)
            else:
                single.append(tid)
        single = [t for t in single if t.split("::")[0] not in whole]
        if len(whole) + len(single) > max_targets:
            for t in single:
                if t.split("::")[0] not in whole:
                    whole.append(t.split("::")[0])
            single = []
        return sorted(set(whole)) + single

    def deselect_args(self, targets, max_targets=None):
        """--deselect=<id> for tests already measured in whole files (resume after an abort). If that does not fit the budget
        they are dropped (then they run again - slower, never wrong)."""
        max_targets = MAX_TARGETS if max_targets is None else max_targets
        whole = {z for z in targets if "::" not in z}
        args = ["--deselect=" + t for t in self.deselect if t.split("::")[0] in whole and len(t) <= MAX_TARGET_LEN - 11]
        return args if len(targets) + len(args) <= max_targets else []

    def as_dict(self):
        return {"env": self.env, "commit": self.commit, "reused": len(self.green), "whole": self.whole,
                "single": self.single, "red_before": self.red_before, "sources": self.sources, "deselect": len(self.deselect),
                "reasons": dict(list(self.reasons.items())[:200])}


def plan(store, env, commit, current_test_files, deps=None, max_age_s=None, now=None, only=None):
    """What has to run in `env` on `commit`? current_test_files = all test files of the state; only = restricted selection
    (files/folders), otherwise all. deps = Dependencies (None -> only the same commit counts)."""
    max_age_s = MAX_AGE_S if max_age_s is None else max_age_s
    now = now or _now()
    result = Plan(env, commit)
    state = store.state_of(env)
    files = store.files_of(env)

    def affected(old, file):
        if old == commit:
            return False
        if deps is None:
            return True
        s = deps.affected(old)
        return s is None or file in s

    selection = current_test_files
    if only:
        selection = [f for f in current_test_files if any(f == z or f.startswith(z.rstrip("/") + "/") for z in only)]
    per_file = {}
    for tid, s in state.items():
        per_file.setdefault(s.get("file") or tid.split("::")[0], []).append(s)

    def whole(file, reason):
        """The file runs completely - what is already green on THIS commit (aborted run) is deselected."""
        result.whole.append(file)
        result.reasons[file] = reason
        for s in per_file.get(file, ()):
            if s["commit_"] == commit and s["outcome"] in GREEN and now - (s["ts"] or 0) <= max_age_s:
                result.green[s["test_id"]] = s
                result.deselect.append(s["test_id"])
                result.sources[s["run"]] = result.sources.get(s["run"], 0) + 1

    for file in selection:
        d = files.get(file)
        if not d:
            whole(file, "never ran completely")
            continue
        if now - (d.get("ts") or 0) > max_age_s:
            whole(file, "test list older than %d days" % (max_age_s // 86400))
            continue
        if affected(d.get("commit_"), file):
            whole(file, "changed since %s" % (d.get("commit_") or "?")[:12])
            continue
        open_ = []
        for tid in d["ids"]:
            s = state.get(tid)
            if s is None:
                open_.append((tid, "no result"))
            elif s["outcome"] in RED:
                open_.append((tid, "red (%s) in run %s" % (s["outcome"], s["run"])))
                result.red_before.append(tid)
            elif now - (s["ts"] or 0) > max_age_s:
                open_.append((tid, "result older than %d days" % (max_age_s // 86400)))
            elif affected(s["commit_"], file):
                open_.append((tid, "affected since %s" % (s["commit_"] or "?")[:12]))
            else:
                result.green[tid] = s
                result.sources[s["run"]] = result.sources.get(s["run"], 0) + 1
        if open_ and len(open_) == len(d["ids"]):
            result.whole.append(file)
            result.reasons[file] = open_[0][1] + " (all %d tests)" % len(open_)
        else:
            for tid, reason in open_:
                result.single.append(tid)
                result.reasons[tid] = reason
    return result


# ---------------------------------------------------------------------- report (nightly summary)
def report(hours=24, directory=None):
    d = directory or base_dir()
    since = _now() - hours * 3600
    out = {"format": "result-store-report/1", "time": _iso(_now()), "hours": hours, "projects": {}}
    try:
        names = sorted(n[:-7] for n in os.listdir(d) if n.endswith(".sqlite"))
    except OSError:
        names = []
    for n in names:
        if not NAME_RX.match(n):
            continue
        store = Store(n, d)
        try:
            runs = store.runs_since(since)
            flaky = store.flaky_since(since)
            per_test = {}
            for e in flaky:
                per_test[e["test_id"]] = per_test.get(e["test_id"], 0) + 1
            red = [dict(r) for r in store.db.execute(
                "SELECT env, test_id, run, ts FROM state WHERE outcome IN ('failed','error') AND ts>=?", (since,))]
            out["projects"][n] = {
                "runs": [{"run": r["run"], "env": r["env"], "status": r["status"], "start": _iso(r["start"]),
                          "finish": _iso(r["finish"]), **{k: v for k, v in json.loads(r["info"] or "{}").items()
                                                          if k in ("new", "reused", "flaky", "red", "duration_s")}} for r in runs],
                "flaky": len(flaky), "flaky_tests": [[k, v] for k, v in sorted(per_test.items(), key=lambda kv: -kv[1])[:20]],
                "stably_red": [{"env": r["env"], "test": r["test_id"], "run": r["run"]} for r in red][:30]}
        finally:
            store.close()
    return out


# ---------------------------------------------------------------------- CLI
def _cli_ingest(a):
    store = Store(a.project)
    files = list_test_files(a.repo, a.commit) if a.repo else []
    whole = files if a.whole else [z for z in (a.targets or []) if z in files]
    ts = datetime.datetime.fromisoformat(a.time).timestamp() if a.time else None
    z = store.ingest_junit(a.junit, a.env, a.commit, a.run, files, whole_files=whole, kind=a.kind, attempt=a.attempt, ts=ts)
    print("%s: %d tests (%d green, %d skipped, %d red) from %s -> %s/%s" % (
        a.run, z["total"], z["passed"], z["skipped"], z["failed"] + z["error"], os.path.basename(a.junit), a.project, a.env))
    for t in z["red"][:50]:
        print("  red: %s" % t)
    return 0


def _cli_plan(a):
    store = Store(a.project)
    commit = _git(a.repo, "rev-parse", "HEAD")[1].strip()
    deps = Dependencies(a.repo, commit, tests_dir=a.tests_dir, selector=a.selector)
    result = plan(store, a.env, commit, deps.tests, deps)
    if a.json:
        print(json.dumps(dict(result.as_dict(), targets=result.targets(), deps=deps.reasons), ensure_ascii=False, indent=1))
    else:
        print("Plan %s @ %s: %d reused, %d whole files, %d single tests" % (
            a.env, commit[:12], len(result.green), len(result.whole), len(result.single)))
        for k, v in list(result.reasons.items())[:60]:
            print("  %s - %s" % (k, v))
        for old, g in deps.reasons.items():
            print("  dependencies since %s: %s" % (old[:12], g))
    return 0


def _cli_status(a):
    store = Store(a.project)
    q = "SELECT env, outcome, COUNT(*) AS n FROM state" + (" WHERE env=?" if a.env else "") + " GROUP BY env, outcome"
    for r in store.db.execute(q, (a.env,) if a.env else ()):
        print("%-40s %-8s %d" % (r["env"], r["outcome"], r["n"]))
    if a.red:
        q = "SELECT env, test_id, run, ts FROM state WHERE outcome IN ('failed','error')" + (" AND env=?" if a.env else "")
        for r in store.db.execute(q, (a.env,) if a.env else ()):
            print("  red %s %s (run %s, %s)" % (r["env"], r["test_id"], r["run"], _iso(r["ts"])))
    return 0


def _cli_flaky(a):
    store = Store(a.project)
    rows = store.flaky_since(_now() - a.hours * 3600)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for e in rows:
            print("%s %s %s (run %s, attempt %s)" % (_iso(e["ts"]), e["env"], e["test_id"], e["run"], e["attempt"]))
        print("%d flaky in %d h" % (len(rows), a.hours))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Per-test result store: green stays green")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("ingest")
    e.add_argument("--project", required=True)
    e.add_argument("--env", required=True)
    e.add_argument("--commit", required=True)
    e.add_argument("--run", required=True)
    e.add_argument("--junit", required=True)
    e.add_argument("--repo")
    e.add_argument("--whole", action="store_true", help="the run executed the whole suite (all test files completely)")
    e.add_argument("--targets", nargs="*")
    e.add_argument("--kind", default="parallel", choices=("parallel", "serial"))
    e.add_argument("--attempt", type=int, default=0)
    e.add_argument("--time", help="time of the measurement (ISO), default now")
    pl = sub.add_parser("plan")
    pl.add_argument("--project", required=True)
    pl.add_argument("--env", required=True)
    pl.add_argument("--repo", required=True)
    pl.add_argument("--selector", help="command that gets changed app files and prints test files or FULL")
    pl.add_argument("--tests-dir", default="tests")
    pl.add_argument("--json", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("--project", required=True)
    st.add_argument("--env")
    st.add_argument("--red", action="store_true")
    w = sub.add_parser("flaky")
    w.add_argument("--project", required=True)
    w.add_argument("--hours", type=int, default=24)
    w.add_argument("--json", action="store_true")
    b = sub.add_parser("report")
    b.add_argument("--hours", type=int, default=24)
    a = p.parse_args(argv)
    if a.command == "ingest":
        if not COMMIT_RX.match(a.commit):
            raise SystemExit("--commit needs 40 hex characters")
        return _cli_ingest(a)
    if a.command == "plan":
        return _cli_plan(a)
    if a.command == "status":
        return _cli_status(a)
    if a.command == "flaky":
        return _cli_flaky(a)
    if a.command == "report":
        print(json.dumps(report(a.hours), ensure_ascii=False, indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
