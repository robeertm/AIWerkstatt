"""Single SQLite database (engine.db) — the only writer is this web process.

One shared connection guarded by a lock (WAL mode). Simple and correct for a
single-node self-hosted app. Holds users, secrets, projects, threads, tasks and
events. Lives in the private data volume.
"""
import os
import sqlite3
import threading

import config

_DB = os.path.join(config.DATA, "engine.db")
_lock = threading.RLock()
_conn = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  name TEXT PRIMARY KEY, pin_hash TEXT, role TEXT, emoji TEXT, must_change INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT, emoji TEXT, descr TEXT, accent TEXT,
  provider TEXT, model TEXT, effort TEXT, port INTEGER,
  live_ready INTEGER DEFAULT 0, created_at TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, title TEXT,
  state TEXT DEFAULT 'open', session_id TEXT DEFAULT '', created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, kind TEXT,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,
  created_at TEXT, started_at TEXT, finished_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, task_id INTEGER,
  type TEXT, text TEXT, author TEXT, created_at TEXT
);
-- Per-user read markers: the id of the newest event a user has seen in a thread.
CREATE TABLE IF NOT EXISTS thread_seen (
  user TEXT, thread_id INTEGER, seen_id INTEGER DEFAULT 0,
  PRIMARY KEY (user, thread_id)
);
-- Publish/release bookkeeping per project (the GitHub repo it was pushed to).
CREATE TABLE IF NOT EXISTS pubmeta (
  project_id TEXT PRIMARY KEY, repo TEXT, html_url TEXT,
  visibility TEXT, version TEXT, release_url TEXT,
  published_at TEXT, released_at TEXT
);
-- 🤖 Auto mode: one row per dispatched task recording the chosen model/intensity
-- (feeds the "which model ran most, with which intensity" overview), plus the latest
-- auto decision per project (model/effort/reason/plan) for the live view.
CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, provider TEXT,
  model TEXT, effort TEXT, auto INTEGER DEFAULT 0, reason TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS auto_decision (
  project_id TEXT PRIMARY KEY, model TEXT, effort TEXT, model_label TEXT,
  effort_label TEXT, reason TEXT, tier TEXT, plan TEXT, at TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_id);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_project ON model_usage(project_id);
"""


def conn():
    global _conn
    if _conn is None:
        os.makedirs(config.DATA, exist_ok=True)
        _conn = sqlite3.connect(_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(_SCHEMA)
        _conn.commit()
        migrate(_conn)
    return _conn


# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` never touches an
# existing table, so a database created by an older version lacks them until ``migrate``
# adds them. Order matters only for readability; every entry is checked on its own.
#   tasks.req_model / req_effort  model/effort chosen by the requester (kept across retries)
#   threads.origin / review       review gate of restricted tasks ("api:<service>", bound to
#                                 the token) plus the review state as JSON
#   threads.origin_profile        token profile that created the thread; only the explicit
#                                 value "coordinator" exempts a thread with an origin from
#                                 review (NULL = restricted)
#   tasks.origin                  which token sent each task (new task or follow-up)
#   tasks.started_at / error      start of the latest attempt and the reason a task was given
#   tasks.first_started_at        start of the FIRST attempt (never moved by a retry) → "task since"
#                                 up; the self-healing retry needs both
_ADDED_COLUMNS = (
    ("tasks", "req_model"), ("tasks", "req_effort"),
    ("threads", "origin"), ("threads", "review"),
    ("threads", "origin_profile"), ("tasks", "origin"),
    ("tasks", "started_at"), ("tasks", "error"),
    ("tasks", "first_started_at"),
)


def _columns(c, table):
    return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}


def migrate(c):
    """Bring an existing database up to the current schema. Idempotent: a column is added
    only when it is missing, so running this any number of times changes nothing further.
    Returns the list of ``table.column`` names it added."""
    added = []
    for table, col in _ADDED_COLUMNS:
        if col in _columns(c, table):
            continue
        c.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (table, col))
        added.append("%s.%s" % (table, col))
    if "tasks.error" in added:
        # Before this column existed the self-healing retry could not run at all. Tasks that
        # had already failed then were shown as failed and the user has moved on; they are
        # marked as given up so an upgrade does not suddenly re-run them.
        c.execute("UPDATE tasks SET error='ended before automatic retries were available' "
                  "WHERE status='failed' AND error IS NULL")
    c.commit()
    return added


def execute(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur


def query_all(sql, args=()):
    with _lock:
        return conn().execute(sql, args).fetchall()


def query_one(sql, args=()):
    with _lock:
        return conn().execute(sql, args).fetchone()
