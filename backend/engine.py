"""Engine — projects, threads, tasks and the task/event pipeline.

The web control plane owns SQLite. To dispatch work it drops a descriptor file in
``/data/task-queue`` which the orchestrator picks up; the agent-runner streams
typed events into the shared ``events`` volume which ``ingest_once`` folds back
into SQLite. This file-drop-box decoupling keeps the web process and the
ephemeral run containers loosely coupled.
"""
import json
import os
import re
import time

import config
import db
import store
import providers

TASK_QUEUE = os.path.join(config.DATA, "task-queue")
EVENTS_DIR = os.environ.get("AIWERKSTATT_EVENTS_DIR", "/events")
INGESTED_DIR = os.path.join(EVENTS_DIR, ".ingested")
USAGE_DIR = os.path.join(EVENTS_DIR, "usage")
LIMIT_FILE = os.path.join(EVENTS_DIR, "limit.json")

# Agent-authored event types that count as "new for the user" (an actual answer
# or state change), so pure acks/user turns don't light up the unread badge.
_UNREAD_TYPES = ("reply", "failed", "stopped", "limited")

PALETTE = ["#f472b6", "#fb7185", "#f59e0b", "#facc15", "#34d399",
           "#22d3ee", "#60a5fa", "#a78bfa", "#f97316", "#2dd4bf"]

# Given to the agent as the first thing it reads: what environment it is in and
# the one hard rule for the live preview to work (serve on 0.0.0.0:8080).
PREAMBLE = (
    "You are an AI coding agent working inside AIWerkstatt. Your working directory "
    "is a fresh project workspace. Build what the user asks as a small self-contained "
    "web app.\n\n"
    "IMPORTANT for the live preview to work:\n"
    "- Put the app in this directory.\n"
    "- It MUST be viewable on http://0.0.0.0:8080 . The simplest way: create an "
    "`index.html` (and any css/js) here — AIWerkstatt serves this folder statically "
    "on port 8080 automatically. For a dynamic app, add a `Dockerfile` that builds "
    "and runs it listening on 0.0.0.0:8080.\n"
    "- Keep it simple, working and self-contained. Explain briefly what you built.\n\n"
    "The user's request:\n"
)


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:24] or "project"


def _used_ports():
    return {r["port"] for r in db.query_all("SELECT port FROM projects WHERE port IS NOT NULL")}


def _free_port():
    lo, hi = config.PORT_RANGE
    used = _used_ports()
    for p in range(lo, hi + 1):
        if p not in used:
            return p
    raise RuntimeError("No free ports left in the configured range.")


# ---------- projects ----------

def create_project(name, emoji, descr, created_by, provider=None, model=None, effort=""):
    base = _slug(name)
    existing = {r["id"] for r in db.query_all("SELECT id FROM projects")}
    pid, i = base, 2
    while pid in existing:
        pid, i = "%s-%d" % (base, i), i + 1
    provider = provider or "claude"
    spec = providers.get_spec(provider)
    model = model or (spec.default_model if spec else "")
    accent = PALETTE[abs(hash(pid)) % len(PALETTE)]
    db.execute(
        "INSERT INTO projects(id,name,emoji,descr,accent,provider,model,effort,port,live_ready,created_at,created_by)"
        " VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
        (pid, name.strip(), (emoji or "✨").strip(), (descr or "").strip(), accent,
         provider, model, effort or "", _free_port(), store.now(), created_by))
    return get_project(pid)


def list_projects():
    return [dict(r) for r in db.query_all("SELECT * FROM projects ORDER BY created_at DESC")]


def get_project(pid):
    r = db.query_one("SELECT * FROM projects WHERE id=?", (pid,))
    return dict(r) if r else None


def set_project_settings(pid, provider=None, model=None, effort=None):
    p = get_project(pid)
    if not p:
        raise ValueError("No such project.")
    provider = provider or p["provider"]
    spec = providers.get_spec(provider)
    if model is None:
        model = p["model"] if provider == p["provider"] else (spec.default_model if spec else "")
    if effort is None:
        effort = p["effort"]
    db.execute("UPDATE projects SET provider=?, model=?, effort=? WHERE id=?",
               (provider, model, effort or "", pid))
    return get_project(pid)


def delete_project(pid):
    tids = [r["id"] for r in db.query_all("SELECT id FROM threads WHERE project_id=?", (pid,))]
    for tid in tids:
        db.execute("DELETE FROM events WHERE thread_id=?", (tid,))
        db.execute("DELETE FROM tasks WHERE thread_id=?", (tid,))
    db.execute("DELETE FROM threads WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))


def mark_live_ready(pid):
    db.execute("UPDATE projects SET live_ready=1 WHERE id=?", (pid,))


# ---------- threads / tasks / events ----------

def _add_event(thread_id, task_id, etype, text, author):
    db.execute("INSERT INTO events(thread_id,task_id,type,text,author,created_at) VALUES(?,?,?,?,?,?)",
               (thread_id, task_id, etype, text, author, store.now()))


def _drop_descriptor(task_id, project, thread_id, kind, text, session_id=""):
    os.makedirs(TASK_QUEUE, exist_ok=True)
    d = {"id": task_id, "slug": project["id"], "thread_id": thread_id, "repo": "",
         "kind": kind, "text": text, "provider": project["provider"],
         "model": project["model"], "effort": project["effort"], "session_id": session_id or ""}
    p = os.path.join(TASK_QUEUE, "%d.json" % task_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, p)


def create_thread(project, title, body, author):
    cur = db.execute("INSERT INTO threads(project_id,title,state,created_at) VALUES(?,?, 'open', ?)",
                     (project["id"], title.strip(), store.now()))
    thread_id = cur.lastrowid
    cur = db.execute("INSERT INTO tasks(thread_id,kind,status,created_at) VALUES(?, 'new','queued', ?)",
                     (thread_id, store.now()))
    task_id = cur.lastrowid
    _add_event(thread_id, task_id, "user", body.strip(), author)
    _drop_descriptor(task_id, project, thread_id, "new", PREAMBLE + body.strip())
    return {"thread_id": thread_id}


def create_comment(project, thread_id, text, author):
    th = db.query_one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if not th:
        raise ValueError("No such thread.")
    cur = db.execute("INSERT INTO tasks(thread_id,kind,status,created_at) VALUES(?, 'followup','queued', ?)",
                     (thread_id, store.now()))
    task_id = cur.lastrowid
    _add_event(thread_id, task_id, "user", text.strip(), author)
    _drop_descriptor(task_id, project, thread_id, "followup", text.strip(), th["session_id"] or "")


def enqueue_compact(project, thread_id):
    _drop_descriptor(int(time.time()), project, thread_id, "compact", "")


def enqueue_stop(project, thread_id):
    # Cancel not-yet-run tasks immediately; the live agent (if any) gets a stop signal.
    db.execute("UPDATE tasks SET status='cancelled' WHERE thread_id=? AND status IN ('queued','running')",
               (thread_id,))
    _add_event(thread_id, None, "stopped", "⏹️ Stop requested.", "System")
    _drop_descriptor(int(time.time()), project, thread_id, "stop", "")


# ---------- session state (from the shared events volume) ----------

def thread_session(thread_id):
    p = os.path.join(EVENTS_DIR, "status-%d.json" % thread_id)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not d.get("alive"):
        return None
    return {"alive": True, "ctx_pct": int(d.get("ctx_pct", 0)),
            "pending": int(d.get("pending", 0)), "compacting": bool(d.get("compacting"))}


# ---------- usage (aggregated from the runners' usage files) ----------

def usage_summary():
    """Fold the per-run usage files the runners drop in the events volume into a
    small summary: totals, a per-provider breakdown, and today's figures."""
    total_tokens = total_cost = runs = 0
    today_tokens = today_cost = today_runs = 0.0
    by_provider = {}
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    try:
        names = os.listdir(USAGE_DIR)
    except OSError:
        names = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(USAGE_DIR, n), "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        tok = int(d.get("output_tokens", 0) or 0)
        cost = float(d.get("cost_usd", 0) or 0)
        prov = d.get("provider", "?")
        total_tokens += tok
        total_cost += cost
        runs += 1
        p = by_provider.setdefault(prov, {"provider": prov, "tokens": 0, "cost": 0.0, "runs": 0})
        p["tokens"] += tok
        p["cost"] += cost
        p["runs"] += 1
        if int(d.get("at", 0) or 0) >= day_start:
            today_tokens += tok
            today_cost += cost
            today_runs += 1
    for p in by_provider.values():
        p["cost"] = round(p["cost"], 4)
    return {
        "total": {"tokens": total_tokens, "cost": round(total_cost, 4), "runs": runs},
        "today": {"tokens": int(today_tokens), "cost": round(today_cost, 4), "runs": int(today_runs)},
        "by_provider": sorted(by_provider.values(), key=lambda x: -x["tokens"]),
    }


# ---------- rate-limit status (written by a runner when it hits a limit) ----------

def limit_status():
    try:
        with open(LIMIT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"active": False}
    until = int(d.get("until", 0) or 0)
    now = int(time.time())
    if until <= now:
        return {"active": False}
    # Only expose a resume clock time when it's a REAL provider reset — otherwise the pause
    # is just our internal retry cadence and a shown time would be a made-up value.
    known = bool(d.get("known"))
    out = {"active": True, "until": until, "reset_known": known}
    if known:
        out["resumes_at"] = time.strftime("%H:%M", time.localtime(until))
    return out


# ---------- live agent feed (watch what the agent is doing, step by step) ----------

def read_live(thread_id, offset=0):
    """Incremental read of the shared live-activity file a runner appends to.
    Returns new entries since `offset` (a byte position), the next offset, and
    whether the session is still alive."""
    path = os.path.join(EVENTS_DIR, "live-%d.jsonl" % thread_id)
    live = bool(thread_session(thread_id))
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"entries": [], "offset": 0, "live": live}
    start = max(0, int(offset or 0))
    if start > size:      # file rotated/shrank → restart from the top
        start = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            raw = f.read()
    except OSError:
        return {"entries": [], "offset": start, "live": live}
    cut = raw.rfind("\n")   # only parse up to the last COMPLETE line
    if cut < 0:
        return {"entries": [], "offset": start, "live": live}
    usable = raw[:cut + 1]
    new_offset = start + len(usable.encode("utf-8"))
    entries = []
    for line in usable.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if len(entries) > 400:
        entries = entries[-400:]
    return {"entries": entries, "offset": new_offset, "live": live}


# ---------- live activity (for the ticker) ----------

def project_activity(project_id):
    """What each thread is doing right now — feeds the live activity strip."""
    out = []
    for th in db.query_all("SELECT id, title FROM threads WHERE project_id=? ORDER BY id DESC",
                           (project_id,)):
        status = _thread_status(th["id"])
        sess = thread_session(th["id"])
        if status not in ("working", "queued") and not sess:
            continue
        last = db.query_one(
            "SELECT type, text, created_at FROM events WHERE thread_id=? ORDER BY id DESC LIMIT 1",
            (th["id"],))
        out.append({"thread_id": th["id"], "title": th["title"], "status": status,
                    "session": sess,
                    "last": ({"type": last["type"], "text": (last["text"] or "")[:160],
                              "created_at": last["created_at"]} if last else None)})
    return out


def _thread_status(thread_id):
    tasks = db.query_all("SELECT status FROM tasks WHERE thread_id=? ORDER BY id", (thread_id,))
    st = [t["status"] for t in tasks]
    if "running" in st:
        return "working"
    if "queued" in st:
        return "queued"
    if "waiting" in st:
        return "waiting"
    last = db.query_one("SELECT type FROM events WHERE thread_id=? ORDER BY id DESC LIMIT 1", (thread_id,))
    if last and last["type"] == "stopped":
        return "stopped"
    return "done"


# ---------- unread markers (per user) ----------

def _seen_id(user, thread_id):
    if not user:
        return 0
    r = db.query_one("SELECT seen_id FROM thread_seen WHERE user=? AND thread_id=?", (user, thread_id))
    return int(r["seen_id"]) if r else 0


def thread_unread(thread_id, user):
    if not user:
        return 0
    ph = ",".join("?" * len(_UNREAD_TYPES))
    r = db.query_one(
        "SELECT COUNT(*) c FROM events WHERE thread_id=? AND id>? AND type IN (%s)" % ph,
        (thread_id, _seen_id(user, thread_id), *_UNREAD_TYPES))
    return int(r["c"]) if r else 0


def mark_seen(thread_id, user):
    if not user:
        return
    top = db.query_one("SELECT COALESCE(MAX(id),0) m FROM events WHERE thread_id=?", (thread_id,))["m"]
    db.execute(
        "INSERT INTO thread_seen(user,thread_id,seen_id) VALUES(?,?,?) "
        "ON CONFLICT(user,thread_id) DO UPDATE SET seen_id=excluded.seen_id",
        (user, thread_id, int(top)))


def project_unread(project_id, user):
    if not user:
        return 0
    total = 0
    for th in db.query_all("SELECT id FROM threads WHERE project_id=?", (project_id,)):
        total += thread_unread(th["id"], user)
    return total


def list_threads(project_id, user=None):
    out = []
    for th in db.query_all("SELECT * FROM threads WHERE project_id=? ORDER BY id DESC", (project_id,)):
        snippet = db.query_one("SELECT text FROM events WHERE thread_id=? AND type='user' ORDER BY id LIMIT 1",
                               (th["id"],))
        out.append({"id": th["id"], "title": th["title"], "status": _thread_status(th["id"]),
                    "snippet": (snippet["text"][:160] if snippet else ""),
                    "created_at": th["created_at"], "unread": thread_unread(th["id"], user)})
    return out


def thread_timeline(thread_id):
    th = db.query_one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if not th:
        return None
    evs = db.query_all("SELECT * FROM events WHERE thread_id=? ORDER BY id", (thread_id,))
    timeline = [{"id": "e-%d" % e["id"], "type": e["type"], "author": e["author"],
                 "text": e["text"], "created_at": e["created_at"]} for e in evs]
    return {"id": thread_id, "title": th["title"], "status": _thread_status(thread_id),
            "timeline": timeline, "session": thread_session(thread_id)}


# ---------- ingest events written by agent-runners ----------

def ingest_once():
    """Fold event files from the shared volume into SQLite. Returns a list of
    (project_id, thread_id, etype) for the app to react to (e.g. deploy on reply)."""
    reacted = []
    try:
        names = sorted(os.listdir(EVENTS_DIR))
    except OSError:
        return reacted
    for n in names:
        if not n.endswith(".json") or n.startswith("status-") or n.startswith("limit") or n == ".ingested":
            continue
        p = os.path.join(EVENTS_DIR, n)
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            try: os.remove(p)
            except OSError: pass
            continue
        thread_id = d.get("thread_id")
        task_id = d.get("task_id")
        etype = d.get("type")
        text = d.get("text") or ""
        th = db.query_one("SELECT project_id FROM threads WHERE id=?", (thread_id,)) if thread_id else None
        if th is None:
            try: os.remove(p)
            except OSError: pass
            continue
        # store the visible event (skip pure acks from cluttering; keep as 'ack')
        _add_event(thread_id, task_id, etype, text, d.get("author", "Agent"))
        # update task/thread state
        if etype == "ack" and task_id:
            db.execute("UPDATE tasks SET status='running' WHERE id=? AND status='queued'", (task_id,))
        elif etype == "reply" and task_id:
            db.execute("UPDATE tasks SET status='done', finished_at=? WHERE id=?", (store.now(), task_id))
        elif etype == "failed" and task_id:
            db.execute("UPDATE tasks SET status='failed', finished_at=? WHERE id=?", (store.now(), task_id))
        elif etype == "stopped" and task_id:
            db.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?", (store.now(), task_id))
        elif etype == "limited" and task_id:
            db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task_id,))
        # capture session id for resume
        sp = os.path.join(EVENTS_DIR, "sess-%d.txt" % thread_id)
        try:
            with open(sp, "r", encoding="utf-8") as f:
                sid = f.read().strip()
            if sid:
                db.execute("UPDATE threads SET session_id=? WHERE id=?", (sid, thread_id))
        except OSError:
            pass
        reacted.append((th["project_id"], thread_id, etype))
        try: os.remove(p)
        except OSError: pass
    return reacted
