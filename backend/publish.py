"""Publish & release a project the agent built.

Two layers:
  * a leak-scan gate over the project's workspace (never push secrets), and
  * optional GitHub delivery — push the workspace to a repo, and tag a release.

GitHub access is a personal access token the user connects once (stored 0600 in
the private data volume, exactly like a provider credential). Nothing here runs
unless the user explicitly clicks Publish/Release; the token never leaves this
machine except as the Authorization header to api.github.com.

A zip export needs no account at all — scan, then download.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import zipfile

import requests

import store
from scrub.scan import scan_tree

WORKSPACES = os.environ.get("AIWERKSTATT_WORKSPACES", "/workspaces")
API = "https://api.github.com"
_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build"}
_UA = "AIWerkstatt"


def workspace(pid: str) -> str:
    return os.path.join(WORKSPACES, pid)


def has_files(pid: str) -> bool:
    p = workspace(pid)
    return os.path.isdir(p) and bool([x for x in os.listdir(p) if x not in _SKIP])


# ---------- leak scan ----------

def scan_project(pid: str) -> dict:
    root = workspace(pid)
    if not os.path.isdir(root):
        return {"ok": True, "blocking": 0, "review": 0, "findings": []}
    return scan_tree(root)


# ---------- zip export (no account needed) ----------

def zip_project(pid: str) -> io.BytesIO:
    root = workspace(pid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arc = os.path.join(pid, os.path.relpath(full, root))
                try:
                    z.write(full, arc)
                except OSError:
                    continue
    buf.seek(0)
    return buf


# ---------- GitHub token (connected once, like a provider) ----------

def _headers(token: str) -> dict:
    return {"Authorization": "Bearer %s" % token, "Accept": "application/vnd.github+json",
            "User-Agent": _UA, "X-GitHub-Api-Version": "2022-11-28"}


def verify_token(token: str) -> dict | None:
    try:
        r = requests.get("%s/user" % API, headers=_headers(token), timeout=15)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    return {"login": d.get("login"), "name": d.get("name") or d.get("login")}


def github_login() -> str | None:
    return store.get_secret("github:login")


def github_connected() -> bool:
    return bool(store.get_secret("github:token") and store.get_secret("github:login"))


def connect_github(token: str) -> dict:
    info = verify_token(token)
    if not info or not info.get("login"):
        raise ValueError("That GitHub token didn't work. Create one with 'repo' scope.")
    store.set_secret("github:token", token)
    store.set_secret("github:login", info["login"])
    return info


def disconnect_github():
    store.delete_secret("github:token")
    store.delete_secret("github:login")


# ---------- GitHub repo + push ----------

def _ensure_repo(token: str, login: str, name: str, private: bool) -> dict:
    h = _headers(token)
    r = requests.get("%s/repos/%s/%s" % (API, login, name), headers=h, timeout=15)
    if r.status_code == 200:
        return r.json()
    if r.status_code != 404:
        raise ValueError("GitHub error (%s) looking up the repository." % r.status_code)
    r = requests.post("%s/user/repos" % API, headers=h, timeout=20, json={
        "name": name, "private": bool(private), "auto_init": False,
        "description": "Built with AIWerkstatt."})
    if r.status_code not in (200, 201):
        msg = ""
        try:
            msg = r.json().get("message", "")
        except ValueError:
            pass
        raise ValueError("Could not create the repository: %s" % (msg or r.status_code))
    return r.json()


def _git(args, cwd, token=None):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(["git", *args], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        # Never surface the token if it appears in a remote URL.
        err = (p.stderr or p.stdout or "git failed").strip()
        if token:
            err = err.replace(token, "***")
        raise ValueError(err[:400])
    return p.stdout


def publish_project(pid: str, project_name: str, repo: str, private: bool) -> dict:
    """Leak-scan, then push the project's workspace to GitHub (using the connected
    account). Returns {full_name, html_url, visibility}."""
    if not has_files(pid):
        raise ValueError("This project has no files to publish yet.")
    scan = scan_project(pid)
    if scan["blocking"]:
        raise ValueError("Leak scan found %d blocking issue(s) — fix before publishing."
                         % scan["blocking"])
    token = store.get_secret("github:token")
    login = store.get_secret("github:login")
    if not token or not login:
        raise ValueError("Connect a GitHub account first.")
    repo = (repo or pid).strip()
    info = _ensure_repo(token, login, repo, private)
    branch = info.get("default_branch") or "main"

    src = workspace(pid)
    ctx = tempfile.mkdtemp(prefix="aiw-pub-")
    try:
        for entry in os.listdir(src):
            if entry in _SKIP:
                continue
            s, d = os.path.join(src, entry), os.path.join(ctx, entry)
            if os.path.isdir(s):
                shutil.copytree(s, d, ignore=shutil.ignore_patterns(*_SKIP))
            else:
                shutil.copy2(s, d)
        readme = os.path.join(ctx, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write("# %s\n\nBuilt with [AIWerkstatt](https://github.com/%s).\n"
                        % (project_name or repo, "robeertm/AIWerkstatt"))
        ident = ["-c", "user.email=%s@users.noreply.github.com" % login,
                 "-c", "user.name=%s" % login]
        _git(["init", "-q", "-b", branch], ctx)
        _git(["add", "-A"], ctx)
        _git([*ident, "commit", "-q", "-m", "Publish from AIWerkstatt"], ctx, token)
        remote = "https://x-access-token:%s@github.com/%s/%s.git" % (token, login, repo)
        _git(["remote", "add", "origin", remote], ctx)
        _git(["push", "-f", "origin", branch], ctx, token)
    finally:
        shutil.rmtree(ctx, ignore_errors=True)

    full = "%s/%s" % (login, repo)
    html = info.get("html_url") or ("https://github.com/%s" % full)
    vis = "private" if private else "public"
    db_upsert_pub(pid, repo, html, vis)
    return {"full_name": full, "html_url": html, "visibility": vis}


def release_project(pid: str, version: str, notes: str) -> dict:
    """Create a GitHub release/tag on the project's already-published repo."""
    import db
    meta = db.query_one("SELECT * FROM pubmeta WHERE project_id=?", (pid,))
    if not meta or not meta["repo"]:
        raise ValueError("Publish this project to GitHub first.")
    token = store.get_secret("github:token")
    login = store.get_secret("github:login")
    if not token or not login:
        raise ValueError("Connect a GitHub account first.")
    tag = version.strip()
    if not tag:
        raise ValueError("A version tag is required (e.g. v1.0.0).")
    if not tag[0].isdigit() and not tag.startswith("v"):
        tag = "v" + tag
    r = requests.post("%s/repos/%s/%s/releases" % (API, login, meta["repo"]),
                      headers=_headers(token), timeout=20,
                      json={"tag_name": tag, "name": tag, "body": notes or "",
                            "draft": False, "prerelease": False})
    if r.status_code not in (200, 201):
        msg = ""
        try:
            msg = r.json().get("message", "")
        except ValueError:
            pass
        raise ValueError("GitHub release failed: %s" % (msg or r.status_code))
    url = r.json().get("html_url")
    db_set_release(pid, tag, url)
    return {"tag": tag, "html_url": url}


# ---------- pubmeta persistence ----------

def db_upsert_pub(pid, repo, html_url, visibility):
    import db
    import store as _s
    db.execute(
        "INSERT INTO pubmeta(project_id,repo,html_url,visibility,published_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(project_id) DO UPDATE SET repo=excluded.repo, html_url=excluded.html_url, "
        "visibility=excluded.visibility, published_at=excluded.published_at",
        (pid, repo, html_url, visibility, _s.now()))


def db_set_release(pid, tag, url):
    import db
    import store as _s
    db.execute("UPDATE pubmeta SET version=?, release_url=?, released_at=? WHERE project_id=?",
               (tag, url, _s.now(), pid))


def pub_status(pid) -> dict:
    import db
    m = db.query_one("SELECT * FROM pubmeta WHERE project_id=?", (pid,))
    return dict(m) if m else {}
