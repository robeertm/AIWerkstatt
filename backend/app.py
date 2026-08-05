"""AIWerkstatt — web control plane.

Flask app that serves the React UI and the API, owns SQLite, and runs two
background threads: the orchestrator (launches agent-runner containers) and the
ingest loop (folds agent events into SQLite and deploys the app the agent built).
"""
import mimetypes
import os
import stat
import threading
import time

from functools import wraps

import requests
from flask import Flask, jsonify, request, send_from_directory, send_file, abort, session

import config
import db
import store
import engine
import providers
import publish
import orchestrator as orch

DIST = os.environ.get("AIWERKSTATT_DIST",
                      os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"))
WORKSPACES = os.environ.get("AIWERKSTATT_WORKSPACES", "/workspaces")

app = Flask(__name__, static_folder=None)
store.ensure_seed()
app.secret_key = store.get_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=False,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)


# ---------- auth helpers ----------

def current_user():
    return store.get_user(session.get("user")) if session.get("user") else None


def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if not current_user():
            return jsonify(error="Please sign in.", need_login=True), 401
        return fn(*a, **k)
    return wrap


def admin_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return jsonify(error="Please sign in.", need_login=True), 401
        if u["role"] != "admin":
            return jsonify(error="Admins only."), 403
        return fn(*a, **k)
    return wrap


# ---------- provider credentials ----------

def provider_creds(provider_id):
    """Env vars to hand a run, from the encrypted store. Never persisted elsewhere."""
    spec = providers.get_spec(provider_id)
    if not spec:
        return {}
    env = {}
    key = store.get_secret("provider:%s:key" % provider_id)
    oauth = store.get_secret("provider:%s:oauth" % provider_id)
    if key and spec.key_env:
        env[spec.key_env] = key
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN" if provider_id == "claude" else (spec.key_env or "OAUTH_TOKEN")] = oauth
    return env


def provider_connected(provider_id):
    spec = providers.get_spec(provider_id)
    if spec and not spec.auth_modes:
        return True   # demo — no credentials needed (always runnable)
    return store.provider_connected(provider_id)


def any_real_connected():
    """True once at least one credential-based provider (Claude/OpenAI/Gemini) is
    connected. Used to retire the zero-key demo from the choosers."""
    return any(store.provider_connected(s.id) for s in providers.all_specs() if s.auth_modes)


# ---------- auth routes ----------

@app.get("/api/health")
def health():
    return jsonify(ok=True, providers=[s.id for s in providers.all_specs()])


@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        users = store.list_users()
        first_run = len(users) == 1 and users[0].get("must_change")
        return jsonify(logged_in=False,
                       users=[{"name": x["name"], "emoji": x.get("emoji", "🙂")} for x in users],
                       first_run=bool(first_run))
    return jsonify(logged_in=True, user=store.public_user(u))


@app.post("/api/login")
def login():
    d = request.get_json(force=True) or {}
    u = store.verify(d.get("name"), d.get("pin"))
    if not u:
        return jsonify(error="Wrong name or PIN."), 401
    session["user"] = u["name"]
    session.permanent = True
    return jsonify(user=store.public_user(u))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/me/pin")
@login_required
def change_pin():
    d = request.get_json(force=True) or {}
    try:
        store.set_pin(current_user()["name"], d.get("pin"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ---------- users (admin) ----------

@app.get("/api/users")
@admin_required
def users_list():
    return jsonify(users=store.list_users())


@app.post("/api/users")
@admin_required
def users_add():
    d = request.get_json(force=True) or {}
    try:
        u = store.add_user(d.get("name"), d.get("pin"), d.get("role", "member"), d.get("emoji", "🙂"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(user=u), 201


@app.delete("/api/users/<name>")
@admin_required
def users_remove(name):
    if name.lower() == current_user()["name"].lower():
        return jsonify(error="You can't remove yourself."), 400
    try:
        store.remove_user(name)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# ---------- providers ----------

@app.get("/api/providers")
@login_required
def providers_list():
    real = any_real_connected()
    out = []
    for s in providers.all_specs():
        d = s.public()
        d["connected"] = provider_connected(s.id)
        # `available` = offer this as a choice. The demo retires once a real
        # provider is connected; it returns when no credential is left.
        d["available"] = (not real) if not s.auth_modes else d["connected"]
        out.append(d)
    return jsonify(providers=out)


@app.post("/api/providers/<pid>/credentials")
@admin_required
def providers_connect(pid):
    spec = providers.get_spec(pid)
    if not spec:
        return jsonify(error="Unknown provider."), 404
    d = request.get_json(force=True) or {}
    mode = d.get("mode", "api_key")
    value = (d.get("value") or "").strip()
    if len(value) < 20:
        return jsonify(error="That doesn't look like a valid key/token."), 400
    if mode == "oauth":
        store.set_secret("provider:%s:oauth" % pid, value)
    else:
        if not spec.validate_key(value):
            return jsonify(error="That key doesn't match the expected format."), 400
        store.set_secret("provider:%s:key" % pid, value)
    return jsonify(ok=True, connected=True)


@app.delete("/api/providers/<pid>/credentials")
@admin_required
def providers_disconnect(pid):
    store.delete_secret("provider:%s:key" % pid)
    store.delete_secret("provider:%s:oauth" % pid)
    return jsonify(ok=True)


# ---------- projects ----------

def _shape_project(p):
    d = dict(p)
    d["descr"] = p.get("descr")
    d["live_port"] = p.get("port")
    d["live_url"] = "http://%s:%s" % (config.PUBLIC_HOST, p["port"]) if p.get("port") else None
    d["live_ready"] = bool(p.get("live_ready"))
    d["mine"] = bool(current_user() and p.get("created_by") == current_user()["name"])
    return d


@app.get("/api/projects")
@login_required
def projects_list():
    me_name = current_user()["name"]
    out = []
    for p in engine.list_projects():
        d = _shape_project(p)
        threads = engine.list_threads(p["id"])
        d["threads"] = len(threads)
        d["active"] = any(t["status"] in ("working", "queued") for t in threads)
        d["unread"] = engine.project_unread(p["id"], me_name)
        out.append(d)
    return jsonify(projects=out, me=me_name)


@app.post("/api/projects")
@login_required
def projects_create():
    d = request.get_json(force=True) or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify(error="Please give the project a name."), 400
    provider = d.get("provider") or "demo"
    if not provider_connected(provider):
        return jsonify(error="Connect that AI provider first (⚙️ Providers)."), 409
    p = engine.create_project(name, d.get("emoji"), d.get("desc"), current_user()["name"],
                              provider=provider, model=d.get("model"), effort=d.get("effort", ""))
    idea = (d.get("idea") or "").strip()
    if idea:
        engine.create_thread(p, "Let's go 🚀", idea, current_user()["name"])
    return jsonify(project=_shape_project(p)), 201


@app.get("/api/projects/<pid>")
@login_required
def project_detail(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    return jsonify(project=_shape_project(p),
                   threads=engine.list_threads(pid, current_user()["name"]),
                   publish=publish.pub_status(pid))


@app.post("/api/projects/<pid>/settings")
@login_required
def project_settings(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    if current_user()["role"] != "admin" and p.get("created_by") != current_user()["name"]:
        return jsonify(error="Only the owner or an admin can change this."), 403
    d = request.get_json(force=True) or {}
    prov = d.get("provider")
    if prov and not provider_connected(prov):
        return jsonify(error="Connect that provider first."), 409
    p = engine.set_project_settings(pid, provider=prov, model=d.get("model"), effort=d.get("effort"))
    return jsonify(ok=True, project=_shape_project(p))


@app.delete("/api/projects/<pid>")
@login_required
def project_delete(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    if current_user()["role"] != "admin" and p.get("created_by") != current_user()["name"]:
        return jsonify(error="Only the owner or an admin can delete this."), 403
    engine.delete_project(pid)
    try:
        ORCH._client.containers.get("aiwerkstatt-app-%s" % pid).remove(force=True)
    except Exception:
        pass
    return jsonify(ok=True)


# ---------- threads ----------

@app.post("/api/projects/<pid>/threads")
@login_required
def thread_create(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    d = request.get_json(force=True) or {}
    title = (d.get("title") or "").strip()
    body = (d.get("request") or d.get("body") or "").strip()
    if not title or not body:
        return jsonify(error="Title and request are required."), 400
    if not provider_connected(p["provider"]):
        return jsonify(error="Connect this project's AI provider first."), 409
    res = engine.create_thread(p, title, body, current_user()["name"])
    return jsonify(number=res["thread_id"], id=res["thread_id"]), 201


@app.get("/api/threads/<int:tid>")
@login_required
def thread_get(tid):
    tl = engine.thread_timeline(tid)
    if not tl:
        abort(404)
    th = db.query_one("SELECT project_id FROM threads WHERE id=?", (tid,))
    p = engine.get_project(th["project_id"]) if th else None
    tl["project_id"] = th["project_id"] if th else None
    tl["live_port"] = p.get("port") if p else None
    tl["live_url"] = ("http://%s:%s" % (config.PUBLIC_HOST, p["port"])) if p and p.get("port") else None
    tl["live_ready"] = bool(p.get("live_ready")) if p else False
    return jsonify(tl)


@app.post("/api/threads/<int:tid>/comment")
@login_required
def thread_comment(tid):
    th = db.query_one("SELECT project_id FROM threads WHERE id=?", (tid,))
    if not th:
        abort(404)
    d = request.get_json(force=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return jsonify(error="Empty message."), 400
    engine.create_comment(engine.get_project(th["project_id"]), tid, text, current_user()["name"])
    return jsonify(ok=True), 201


@app.post("/api/threads/<int:tid>/compact")
@login_required
def thread_compact(tid):
    th = db.query_one("SELECT project_id FROM threads WHERE id=?", (tid,))
    if not th:
        abort(404)
    if not engine.thread_session(tid):
        return jsonify(error="No open session to compact."), 409
    engine.enqueue_compact(engine.get_project(th["project_id"]), tid)
    return jsonify(ok=True), 202


@app.post("/api/threads/<int:tid>/stop")
@login_required
def thread_stop(tid):
    th = db.query_one("SELECT project_id FROM threads WHERE id=?", (tid,))
    if not th:
        abort(404)
    engine.enqueue_stop(engine.get_project(th["project_id"]), tid)
    return jsonify(ok=True), 202


@app.post("/api/threads/<int:tid>/seen")
@login_required
def thread_seen(tid):
    engine.mark_seen(tid, current_user()["name"])
    return jsonify(ok=True)


@app.get("/api/threads/<int:tid>/live")
@login_required
def thread_live(tid):
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return jsonify(engine.read_live(tid, offset))


# ---------- live activity, usage, rate-limit (visibility) ----------

@app.get("/api/projects/<pid>/activity")
@login_required
def project_activity(pid):
    if not engine.get_project(pid):
        abort(404)
    return jsonify(activity=engine.project_activity(pid))


@app.get("/api/usage")
@login_required
def usage():
    return jsonify(engine.usage_summary())


@app.get("/api/limit")
@login_required
def limit():
    return jsonify(engine.limit_status())


# ---------- self-update check (admin) ----------

_update_cache = {"at": 0, "data": None}


@app.get("/api/update")
@login_required
def update_check():
    now = time.time()
    if _update_cache["data"] and now - _update_cache["at"] < 3600:
        return jsonify(_update_cache["data"])
    data = {"current": config.VERSION, "latest": None, "update_available": False,
            "url": "https://github.com/%s/releases" % config.REPO_SLUG}
    try:
        r = requests.get("https://api.github.com/repos/%s/releases/latest" % config.REPO_SLUG,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "AIWerkstatt"},
                         timeout=10)
        if r.status_code == 200:
            tag = (r.json().get("tag_name") or "").lstrip("v")
            data["latest"] = tag or None
            if tag and tag != config.VERSION:
                data["update_available"] = _newer(tag, config.VERSION)
                data["url"] = r.json().get("html_url") or data["url"]
    except requests.RequestException:
        pass
    _update_cache.update(at=now, data=data)
    return jsonify(data)


def _newer(a, b):
    def parts(v):
        out = []
        for x in v.split("."):
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out
    return parts(a) > parts(b)


# ---------- GitHub account (for publishing) ----------

@app.get("/api/github")
@login_required
def github_status():
    return jsonify(connected=publish.github_connected(), login=publish.github_login())


@app.post("/api/github")
@admin_required
def github_connect():
    d = request.get_json(force=True) or {}
    token = (d.get("token") or "").strip()
    if len(token) < 20:
        return jsonify(error="That doesn't look like a GitHub token."), 400
    try:
        info = publish.connect_github(token)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, login=info["login"])


@app.delete("/api/github")
@admin_required
def github_disconnect():
    publish.disconnect_github()
    return jsonify(ok=True)


# ---------- publish & release a project ----------

def _may_publish(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    u = current_user()
    if u["role"] != "admin" and p.get("created_by") != u["name"]:
        return None
    return p


@app.post("/api/projects/<pid>/publish/scan")
@login_required
def publish_scan(pid):
    if not _may_publish(pid):
        return jsonify(error="Only the owner or an admin can publish."), 403
    return jsonify(scan=publish.scan_project(pid), has_files=publish.has_files(pid))


@app.get("/api/projects/<pid>/publish/export")
@login_required
def publish_export(pid):
    if not _may_publish(pid):
        return jsonify(error="Only the owner or an admin can export."), 403
    if not publish.has_files(pid):
        return jsonify(error="No files to export yet."), 400
    buf = publish.zip_project(pid)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="%s.zip" % pid, max_age=0)


@app.post("/api/projects/<pid>/publish")
@login_required
def publish_go(pid):
    p = _may_publish(pid)
    if not p:
        return jsonify(error="Only the owner or an admin can publish."), 403
    if not publish.github_connected():
        return jsonify(error="Connect a GitHub account first (⚙️ Providers → GitHub)."), 409
    d = request.get_json(force=True) or {}
    repo = (d.get("repo") or pid).strip()
    private = bool(d.get("private"))
    try:
        res = publish.publish_project(pid, p["name"], repo, private)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, **res, status=publish.pub_status(pid))


@app.post("/api/projects/<pid>/release")
@login_required
def release_go(pid):
    if not _may_publish(pid):
        return jsonify(error="Only the owner or an admin can release."), 403
    d = request.get_json(force=True) or {}
    try:
        res = publish.release_project(pid, d.get("version", ""), d.get("notes", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, **res, status=publish.pub_status(pid))


# ---------- file browser (read-only, from the project workspace) ----------

_FILES_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist"}
_TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".css", ".scss",
             ".html", ".htm", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh",
             ".env", ".sql", ".csv", ".vue", ".svelte", ".rs", ".go", ".rb", ".php",
             ".gitignore", ".dockerignore"}


def _safe(root, rel):
    rel = (rel or "").replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    return full if (full == root_real or full.startswith(root_real + os.sep)) else None


@app.get("/api/projects/<pid>/files")
@login_required
def project_files(pid):
    p = engine.get_project(pid)
    if not p:
        abort(404)
    root = os.path.join(WORKSPACES, pid)
    if not os.path.isdir(root):
        return jsonify(available=False, reason="No files yet.", path="", entries=[])
    full = _safe(root, request.args.get("path", ""))
    if not full or not os.path.exists(full):
        return jsonify(error="Not found."), 404
    if not os.path.isdir(full):
        return jsonify(error="Not a folder."), 400
    entries = []
    for name in os.listdir(full):
        if name in _FILES_SKIP:
            continue
        fp = os.path.join(full, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        isdir = stat.S_ISDIR(st.st_mode)
        entries.append({"name": name, "dir": isdir, "size": 0 if isdir else st.st_size,
                        "mtime": int(st.st_mtime)})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    rel = os.path.relpath(full, root)
    return jsonify(available=True, path="" if rel == "." else rel.replace("\\", "/"), entries=entries)


@app.get("/api/projects/<pid>/files/raw")
@login_required
def project_file_raw(pid):
    if not engine.get_project(pid):
        abort(404)
    root = os.path.join(WORKSPACES, pid)
    full = _safe(root, request.args.get("path", ""))
    if not full or not os.path.isfile(full):
        abort(404)
    base = os.path.basename(full)
    ext = os.path.splitext(base)[1].lower()
    ctype, _ = mimetypes.guess_type(base)
    as_text = ext in _TEXT_EXT or base.startswith(".")
    if as_text:
        ctype = "text/plain"
    elif ctype is None:
        ctype = "application/octet-stream"
    resp = send_file(full, mimetype=ctype, as_attachment=(request.args.get("dl") == "1"),
                     download_name=base, max_age=0, conditional=True)
    if as_text:
        resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "sandbox allow-scripts allow-popups allow-forms allow-modals;"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------- static SPA ----------

@app.get("/")
def index():
    return send_from_directory(DIST, "index.html")


@app.get("/<path:path>")
def static_proxy(path):
    full = os.path.join(DIST, path)
    if os.path.isfile(full):
        return send_from_directory(DIST, path)
    return send_from_directory(DIST, "index.html")


# ---------- background: orchestrator + ingest ----------

ORCH = orch.Orchestrator(provider_creds)


def _ingest_loop():
    time.sleep(2)
    while True:
        try:
            for project_id, thread_id, etype in engine.ingest_once():
                if etype == "reply":
                    p = engine.get_project(project_id)
                    if p and ORCH.deploy(project_id, p["port"]):
                        engine.mark_live_ready(project_id)
        except Exception:
            pass
        time.sleep(2)


def _start_background():
    threading.Thread(target=ORCH.run_loop, daemon=True, name="orchestrator").start()
    threading.Thread(target=_ingest_loop, daemon=True, name="ingest").start()


_start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=True)
