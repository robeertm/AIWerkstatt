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
  created_at TEXT, finished_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_threads_project ON threads(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_id);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id);
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
    return _conn


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
