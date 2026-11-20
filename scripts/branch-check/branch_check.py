#!/usr/bin/env python3
"""Is the work of a finished task really on main?  (standard library only, read-only)

A task that reports "built, waiting for the release" is only waiting if its work is on main (or on a release-train
branch).  Work that lives on a local branch nobody pushed is lost the day the work tree is cleaned up, and a later release
does not contain it.  This tool answers the question with git and nothing else; it never merges, pushes or deletes.

    branch_check.py <clone> [--branch NAME]... [--commit SHA]... [--text FILE|-] [--main origin/main] [--json]

Where the work is (worst answer wins for branches; without a branch the NEWEST resolvable commit counts, so work-in-progress
commits that were squashed later do not matter):

    in_tag          on main and in the newest ``v*`` tag
    on_main         on main, in a release-train branch (``origin/*train*``, ``origin/release/*``) or the same patch
                    on main under another SHA (rebased); the tag is still to come
    not_pushed      branch/commit only local, or only on an unrelated remote branch  -> NOT on main
    unclear         nothing resolvable named

``--text`` reads a task's final answer and extracts branch names (``task/...``, ``work/...``; ``--prefix`` adds more) and SHAs
(7-40 hex with at least one digit and one letter).  Exit codes: 0 in_tag/on_main, 3 not_pushed, 2 unclear/usage error.
"""
import argparse
import json
import os
import re
import subprocess
import sys

TRAIN_REFS = ("refs/remotes/origin/*train*", "refs/remotes/origin/release/*")
PATCH_WINDOW = "60 days ago"
BRANCH_PREFIXES = ("task", "work")             # override with --prefix; a task branch is whatever the agent rule names
BRANCH_RE = re.compile(r"(?:%s)/[A-Za-z0-9._/-]*[A-Za-z0-9]" % "|".join(BRANCH_PREFIXES))
SHA_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,40}(?![0-9A-Za-z])")
GOOD = ("in_tag", "on_main", "train", "rebased")


def git(clone, *args, timeout=60):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), "GIT_TERMINAL_PROMPT": "0",
           "LANG": "C.UTF-8"}
    for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    try:
        return subprocess.run(["git", "-C", clone] + list(args), env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 124, "", str(e))


def out(r):
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def extract(text, prefixes=BRANCH_PREFIXES):
    """Branch names and commit candidates from a final answer -> (branches, shas)."""
    t = str(text or "")
    branch_re = BRANCH_RE if tuple(prefixes) == BRANCH_PREFIXES else re.compile(
        r"(?:%s)/[A-Za-z0-9._/-]*[A-Za-z0-9]" % "|".join(re.escape(p) for p in prefixes))
    shas = []
    for m in SHA_RE.findall(t):
        if re.search(r"\d", m) and re.search(r"[a-f]", m) and m not in shas:
            shas.append(m)
    branches = []
    for m in branch_re.findall(t):
        if m not in branches:
            branches.append(m)
    return shas[:40], branches[:20]


class Clone:
    def __init__(self, path, main="origin/main"):
        self.path, self.main = path, main
        self._tag = self._ids = self._train = None

    def selftest(self):
        r = git(self.path, "rev-parse", "--git-dir")
        if r.returncode != 0:
            raise RuntimeError("git cannot run in %s: %s" % (self.path, (r.stderr or "").strip()[:120]))

    def resolve(self, name):
        """-> (full sha, kind) with kind local|origin|sha, or ("", "")."""
        for ref, kind in (("refs/heads/" + name, "local"), ("refs/remotes/origin/" + name, "origin"), (name, "sha")):
            if kind == "sha" and not re.fullmatch(r"[0-9a-f]{7,40}", name):
                continue
            v = out(git(self.path, "rev-parse", "--verify", "-q", ref + "^{commit}"))
            if v:
                return v, kind
        return "", ""

    def ancestor(self, sha, ref):
        return git(self.path, "merge-base", "--is-ancestor", sha, ref).returncode == 0

    def tag(self):
        if self._tag is None:
            t = out(git(self.path, "tag", "--sort=-v:refname", "--list", "v*"))
            self._tag = t.splitlines()[0] if t else ""
        return self._tag

    def trains(self):
        if self._train is None:
            self._train = [x for x in out(git(self.path, "for-each-ref", "--format=%(refname)", *TRAIN_REFS)).splitlines() if x]
        return self._train

    def patch_id(self, sha):
        p = subprocess.run("git -C '%s' show %s | git patch-id --stable" % (self.path, sha), shell=True, capture_output=True,
                           text=True)
        z = p.stdout.split()
        return z[0] if z else ""

    def main_patch_ids(self):
        if self._ids is None:
            self._ids = set()
            log = out(git(self.path, "log", self.main, "--since=" + PATCH_WINDOW, "--no-merges", "--format=%H"))
            for h in log.split()[:800]:
                p = self.patch_id(h)
                if p:
                    self._ids.add(p)
        return self._ids

    def where(self, sha):
        if self.ancestor(sha, self.main):
            t = self.tag()
            return "in_tag" if t and self.ancestor(sha, "refs/tags/" + t) else "on_main"
        for z in self.trains():
            if self.ancestor(sha, z):
                return "train"
        p = self.patch_id(sha)
        if p and p in self.main_patch_ids():
            return "rebased"
        if out(git(self.path, "branch", "-r", "--contains", sha)):
            return "remote_only"
        return "local_only"


def check(clone, branches=(), commits=()):
    """-> {state, reason, branch, commit}.  `clone` is a Clone."""
    found = []                                         # (branch, sha, where)
    for b in branches:
        sha, kind = clone.resolve(b)
        if sha and kind in ("local", "origin"):
            found.append((b, sha, clone.where(sha)))
    if not found:
        cands = []
        for c in commits:
            sha, _kind = clone.resolve(c)
            if sha:
                cands.append((int(out(git(clone.path, "log", "-1", "--format=%ct", sha)) or 0), sha))
        if cands:
            cands.sort(reverse=True)
            found.append(("", cands[0][1], clone.where(cands[0][1])))
    if not found:
        return {"state": "unclear", "reason": "no resolvable branch or commit named", "branch": "", "commit": ""}
    bad = [f for f in found if f[2] not in GOOD]
    if bad:
        b, sha, w = bad[0]
        what = ("branch %s" % b) if b else "commit %s" % sha[:9]
        reason = ("%s exists only locally (never pushed)" % what) if w == "local_only" else \
            ("%s is only on a remote branch, not on main or in a release-train branch" % what)
        return {"state": "not_pushed", "reason": reason, "branch": b, "commit": sha[:9]}
    b, sha, w = found[0]
    state = "in_tag" if all(f[2] == "in_tag" for f in found) else "on_main"
    return {"state": state, "reason": {"in_tag": "in the newest tag", "on_main": "on main, the tag is still to come",
                                       "train": "in a release-train branch", "rebased": "same patch on main (rebased)"}.get(w, w),
            "branch": b, "commit": sha[:9]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("clone")
    ap.add_argument("--branch", action="append", default=[])
    ap.add_argument("--commit", action="append", default=[])
    ap.add_argument("--text", help="file with a final answer, or - for stdin")
    ap.add_argument("--prefix", action="append", default=[], help="extra branch prefix to look for in --text")
    ap.add_argument("--main", default="origin/main")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    branches, commits = list(a.branch), list(a.commit)
    if a.text:
        t = sys.stdin.read() if a.text == "-" else open(a.text, encoding="utf-8").read()
        shas, brs = extract(t, BRANCH_PREFIXES + tuple(a.prefix))
        branches += [b for b in brs if b not in branches]
        commits += [s for s in shas if s not in commits]
    cl = Clone(a.clone, a.main)
    try:
        cl.selftest()
    except RuntimeError as e:
        print("error:", e, file=sys.stderr)
        return 2
    res = check(cl, branches, commits)
    print(json.dumps(res, ensure_ascii=False) if a.json else "%s: %s%s" % (
        res["state"], res["reason"], (" (%s)" % res["commit"]) if res["commit"] else ""))
    return {"in_tag": 0, "on_main": 0, "not_pushed": 3}.get(res["state"], 2)


if __name__ == "__main__":
    sys.exit(main())
