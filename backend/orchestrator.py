"""Orchestrator — launches one ephemeral agent-runner container per task.

Each agent run gets its own container instead of relying on host-level user
isolation. The orchestrator runs INSIDE the web container and talks to the Docker
daemon ONLY through the hardened socket proxy (`DOCKER_HOST=tcp://dockerproxy:2375`).
It never touches the raw socket, and the containers it starts are unprivileged
(cap-drop ALL, no-new-privileges, no docker access).

Decoupling is by files, exactly like the proven design:
  * the engine drops a descriptor in ``/data/task-queue/<id>.json``,
  * the orchestrator starts an agent-runner (or feeds a live one via the inbox),
  * the agent-runner streams typed events into the shared ``events`` volume,
  * the engine ingests those events into SQLite.

Descriptor shape (written by the engine):
  {id, slug, thread_id, repo, kind: new|followup|compact|stop,
   text, provider, model, effort, session_id?, allowed_tools?}

Volumes (all owned by uid 10001, shared with the runner):
  workspaces → /workspaces/<slug>   events → /events   inbox → /inbox/<thread>
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time

import config
import context_pack
import db
from scrub.scan import scan_tree

try:
    import docker  # docker-py
except Exception:  # pragma: no cover - import guard for tooling
    docker = None

APP_UID = os.environ.get("AIWERKSTATT_APP_UID", "10001")
TASK_QUEUE = os.path.join(config.DATA, "task-queue")
FIRST_DIR = "/inbox"          # per-thread subdir created here
EVENTS_BIND = "/events"
# Same variable the web side already reads (app.py, publish.py); only tests change it.
WORKSPACES_BIND = os.environ.get("AIWERKSTATT_WORKSPACES", "/workspaces")
INBOX_BIND = "/inbox"
# Claude's session store (~/.claude) otherwise lives INSIDE the ephemeral runner and is lost
# when the container exits — so `--resume <sid>` in the next follow-up's fresh container always
# fails instantly (empty result → the run is marked "did not finish cleanly"). Persist it per
# thread on the internal, never-published events volume so a follow-up can resume with full
# context. Shared by web (which creates/inspects it) and every runner (which reads/writes it).
AGENTHOME_BIND = "/events/agenthome"

# Actual Docker volume names (compose gives them explicit names). The runner and
# the web container must reference the SAME names on the daemon.
VOL_WORKSPACES = os.environ.get("AIWERKSTATT_VOL_WORKSPACES", "aiwerkstatt-workspaces")
VOL_EVENTS = os.environ.get("AIWERKSTATT_VOL_EVENTS", "aiwerkstatt-events")
VOL_INBOX = os.environ.get("AIWERKSTATT_VOL_INBOX", "aiwerkstatt-inbox")
# 📚 Shared project vault: a persistent per-project knowledge base the agent reads at the
# start of every run and appends learnings to, so knowledge survives across runs. Mounted
# read/write into every agent-runner (and into web, which seeds + serves it for the UI).
VOL_VAULT = os.environ.get("AIWERKSTATT_VOL_VAULT", "aiwerkstatt-vault")
VAULT_BIND = "/vault"
# Optional knowledge sources for the context pack (new runs). Both are folders as seen by THIS process;
# nothing is inserted when they are unset or empty. MEMORY.md is never inserted, only an index_*.md excerpt.
KNOWLEDGE_DIR = os.environ.get("AIWERKSTATT_KNOWLEDGE_DIR", "")
MEMORY_DIR = os.environ.get("AIWERKSTATT_MEMORY_DIR", "")

MEM_LIMIT = os.environ.get("AIWERKSTATT_RUN_MEM", "2g")
# Fixed worker pool (0.15.0): at most this many agent runs at the same time across ALL projects.
# Further orders wait in the task queue and start strictly in order (lowest task id first) as soon as
# a slot is free. 0 = unlimited (old behaviour).
MAX_PARALLEL_RUNS = int(os.environ.get("AIWERKSTATT_MAX_PARALLEL_RUNS", "5") or 0)
PIDS_LIMIT = int(os.environ.get("AIWERKSTATT_RUN_PIDS", "512"))

# Scan the app the agent built for hard-coded secrets BEFORE it goes live — the same
# deterministic gate that guards publishing. On by default; set to "0" to opt out.
DEPLOY_SECRET_SCAN = os.environ.get("AIWERKSTATT_DEPLOY_SECRET_SCAN", "1") != "0"

# Used when a project workspace has no Dockerfile of its own: serve it statically.
DEFAULT_DOCKERFILE = (
    "FROM python:3.12-alpine\n"
    "WORKDIR /site\n"
    "COPY . /site\n"
    "EXPOSE 8080\n"
    'CMD ["python", "-m", "http.server", "8080"]\n'
)


def _log(msg):
    print("[orchestrator] %s" % msg, flush=True)


class Orchestrator:
    def __init__(self, provider_creds_fn):
        """provider_creds_fn(provider) -> dict of env vars (the API key / token),
        looked up from the encrypted store at launch time (never persisted here)."""
        self._creds_fn = provider_creds_fn
        self._client = docker.from_env() if docker else None
        self._running: dict[str, dict] = {}   # slug -> {container, thread_id}
        self._lock = threading.Lock()
        os.makedirs(TASK_QUEUE, exist_ok=True)

    # ---- helpers ----------------------------------------------------------
    def _thread_inbox(self, thread_id) -> str:
        p = os.path.join("/inbox", str(thread_id))
        os.makedirs(p, exist_ok=True)
        return p

    def _write_first(self, thread_id, text) -> str:
        p = os.path.join(self._thread_inbox(thread_id), "first.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text or "")
        return p

    def _ensure_vault(self, slug) -> str:
        """Create the project's vault dir on the shared volume and seed a knowledge file.
        Returns the container-side path (or '' if the vault volume isn't available)."""
        try:
            d = os.path.join(VAULT_BIND, slug)
            os.makedirs(d, exist_ok=True)
            kf = os.path.join(d, "knowledge.md")
            if not os.path.exists(kf):
                with open(kf, "w", encoding="utf-8") as f:
                    f.write("# Project knowledge — %s\n\n"
                            "> Shared, persistent notes for this project. The AI reads this at the\n"
                            "> start of every run and appends what it learns, so knowledge survives\n"
                            "> across runs. Keep it concise: architecture, decisions, gotchas.\n" % slug)
            return "%s/%s" % (VAULT_BIND, slug)
        except OSError:
            return ""

    @staticmethod
    def _vault_preamble(vault_path) -> str:
        return ("📚 This project has a PERSISTENT knowledge base at %s/knowledge.md that "
                "survives across runs.\n"
                "- FIRST, read %s/knowledge.md for context from earlier runs (architecture, "
                "decisions, gotchas).\n"
                "- WHEN DONE, append any important new facts, decisions or gotchas to it so future "
                "runs remember them. Keep it concise. You may add other .md files under %s/ too.\n\n"
                % (vault_path, vault_path, vault_path))

    def _context_pack(self, slug, title, vault_path, thread_id) -> str:
        """Build the context pack for a NEW run; log + store its metadata. Never raises."""
        try:
            text, meta = context_pack.build(slug, title, vault_dir=vault_path, knowledge_dir=KNOWLEDGE_DIR,
                                            memory_dir=MEMORY_DIR)
        except Exception as e:   # a broken knowledge folder must never block a run
            _log("context pack failed for %s/%s: %r" % (slug, thread_id, e))
            return ""
        _log("context pack %s/%s: docs=%d index=%s bytes=%d no_match=%s omitted=%d"
             % (slug, thread_id, len(meta["docs"]), meta["index"] or "-", meta["bytes"],
                meta["no_match"], meta["omitted"]))
        try:
            p = os.path.join(EVENTS_BIND, "context-pack-%s.json" % thread_id)
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(p + ".tmp", p)
        except OSError:
            pass
        return text + "\n\n" if text else ""

    def _drop_inbox(self, thread_id, payload):
        d = self._thread_inbox(thread_id)
        name = "%d.json" % time.time_ns()
        tmp = os.path.join(d, "." + name)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, name))

    def _note_live(self, thread_id, text, act="say"):
        """Append one line to the thread's live-activity feed the web UI tails, so a
        deploy decision (e.g. a blocked secret) shows up where the user is already
        watching the agent work. Best-effort; never raises into the caller."""
        if not thread_id:
            return
        try:
            path = os.path.join(EVENTS_BIND, "live-%d.jsonl" % int(thread_id))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "activity", "act": act, "text": text},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _alive(self, slug) -> dict | None:
        info = self._running.get(slug)
        if not info:
            return None
        try:
            info["container"].reload()
            if info["container"].status in ("created", "running"):
                return info
        except Exception:
            pass
        self._running.pop(slug, None)
        return None

    def _resume_ok(self, cfg_dir, sid) -> bool:
        """True only if Claude's stored transcript for this session id is actually on the
        persistent store. Otherwise `--resume` would fail in the fresh runner and the
        follow-up would dead-end, so the caller launches a fresh session instead (the
        workspace files carry the state). Covers threads created before this persistence
        existed and any lost/rotated store."""
        try:
            for _root, _dirs, files in os.walk(cfg_dir):
                if any(sid in f for f in files):
                    return True
        except OSError:
            pass
        return False

    # ---- launch -----------------------------------------------------------
    def _launch(self, d):
        slug = d["slug"]
        thread_id = int(d["thread_id"])
        provider = d.get("provider") or "claude"
        # Ensure the project's workspace dir exists (shared volume, uid 10001) — the
        # runner uses it as cwd, which would fail if it were missing.
        os.makedirs("%s/%s" % (WORKSPACES_BIND, slug), exist_ok=True)
        # Persistent Claude config/session dir for THIS thread (survives the ephemeral runner)
        # so a follow-up can `--resume` with full context. If the stored transcript for the
        # requested session id isn't actually here — a thread created before this persistence
        # existed, or a lost store — don't resume into a fresh container (that dead-ends);
        # start a fresh session on the existing workspace instead.
        cfg_dir = "%s/%s/%d" % (AGENTHOME_BIND, slug, thread_id)
        os.makedirs(cfg_dir, exist_ok=True)
        resume_sid = d.get("session_id", "") or ""
        if resume_sid and not self._resume_ok(cfg_dir, resume_sid):
            _log("resume store missing for %s/%s (sid=%s) — starting a fresh session on the "
                 "existing workspace" % (slug, thread_id, resume_sid))
            resume_sid = ""
        # 📚 Prepare the shared project vault and tell the agent about it. Only prepend the
        # instruction on a NEW thread (a follow-up runs in the same live session that already
        # heard it); the vault path is still exported for every run.
        vault_path = self._ensure_vault(slug)
        first_text = d.get("text", "")
        if vault_path and d.get("kind") == "new":
            title = d.get("title") or ""
            if not title:
                try:
                    row = db.query_one("SELECT title FROM threads WHERE id = ?", (thread_id,))
                    title = (row["title"] if row else "") or ""
                except Exception:
                    title = ""
            title = title or (first_text.strip().split("\n")[0] if first_text.strip() else "")
            first_text = (self._vault_preamble(vault_path)
                          + self._context_pack(slug, title, vault_path, thread_id) + first_text)
        first = self._write_first(thread_id, first_text)
        env = {
            "WK_SLUG": slug,
            "WK_VAULT": vault_path,
            # Read by the subagent rules block and the per-run evidence line (agent-runner).
            "AIW_KNOWLEDGE_DIR": vault_path,
            "AIW_MEMORY_DIR": MEMORY_DIR,
            # Pauses before each retry after an API error (runner default 120,300,900; empty = off).
            "AIW_API_RETRY_DELAYS": os.environ.get("AIW_API_RETRY_DELAYS", "120,300,900"),
            "WK_THREAD_ID": str(thread_id),
            "WK_REPO": d.get("repo", ""),
            "WK_PROVIDER": provider,
            "WK_MODEL": d.get("model", ""),
            "WK_EFFORT": d.get("effort", ""),
            "WK_ALLOWED": d.get("allowed_tools", ""),
            # How long the parent run waits for parallel subagents before finishing (Claude Code
            # default 10 min). Raised to 30 min for larger fan-outs; genuine hangs are caught by the
            # stall watchdog. Harmless for non-Claude providers.
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": os.environ.get("WK_BG_WAIT_CEILING_MS", "1800000"),
            # Subagents without an explicit model run on the cheapest one; the todo tools
            # (TaskCreate/TaskUpdate) are off in headless runs unless enabled → no visible progress.
            "CLAUDE_CODE_SUBAGENT_MODEL": os.environ.get("AIWERKSTATT_SUBAGENT_MODEL", "haiku"),
            "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1",
            "WK_WT": "%s/%s" % (WORKSPACES_BIND, slug),
            "WK_FIRST_MSG_FILE": first,
            "WK_FIRST_TASK": str(int(d["id"])),
            "WK_EVENTS": EVENTS_BIND,
            "WK_INBOX": "%s/%s" % (INBOX_BIND, thread_id),
            "WK_USAGE_DIR": "%s/usage" % EVENTS_BIND,
            # Relocate Claude's ~/.claude onto the persistent per-thread dir above so the
            # session transcript survives this container and `--resume` works next time.
            "CLAUDE_CONFIG_DIR": cfg_dir,
            "WK_RESUME_SID": resume_sid,
        }
        env.update(self._creds_fn(provider) or {})   # e.g. {"ANTHROPIC_API_KEY": "..."}
        volumes = {
            VOL_WORKSPACES: {"bind": WORKSPACES_BIND, "mode": "rw"},
            VOL_EVENTS: {"bind": EVENTS_BIND, "mode": "rw"},
            VOL_INBOX: {"bind": INBOX_BIND, "mode": "rw"},
            VOL_VAULT: {"bind": VAULT_BIND, "mode": "rw"},   # 📚 persistent project knowledge
        }
        name = "aiwerkstatt-run-%s-%d-%d" % (slug, thread_id, int(time.time()))
        _log("launch %s (provider=%s model=%s)" % (name, provider, d.get("model")))
        container = self._client.containers.run(
            config.AGENT_RUNNER_IMAGE,
            detach=True, name=name, environment=env, volumes=volumes,
            user="%s:%s" % (APP_UID, APP_UID),
            cap_drop=["ALL"], security_opt=["no-new-privileges"],
            pids_limit=PIDS_LIMIT, mem_limit=MEM_LIMIT,
            network_mode="bridge",   # needs internet for the provider API + git/npm
        )
        with self._lock:
            self._running[slug] = {"container": container, "thread_id": thread_id,
                                   "task_id": d.get("id"), "model": d.get("model") or ""}
        threading.Thread(target=self._monitor, args=(slug, container),
                         daemon=True, name="run-%s" % slug).start()

    def _monitor(self, slug, container):
        """Wait for the run to finish, then clean up. The engine ingests the
        agent's events from the shared volume (retry/limit handled there)."""
        try:
            container.wait()
        except Exception as e:
            _log("wait(%s) error: %s" % (slug, e))
        try:
            container.remove(force=True)
        except Exception:
            pass
        with self._lock:
            info = self._running.get(slug)
            if info and info["container"].id == container.id:
                self._running.pop(slug, None)
        _log("run for %s finished" % slug)

    # ---- queue poll -------------------------------------------------------
    def _pool_full(self) -> bool:
        if MAX_PARALLEL_RUNS <= 0:
            return False
        live = sum(1 for slug in list(self._running) if self._alive(slug))
        return live >= MAX_PARALLEL_RUNS

    def pool_snapshot(self) -> dict:
        """Live view of the worker pool for the admin overview. Counts exactly like the pool
        itself: a run is busy while its container is alive; an order is waiting while its
        descriptor sits in the task queue (follow-ups for a live run are fed straight into it
        and never wait). ``max`` 0 = unlimited."""
        runs = []
        live_threads = {}
        for slug in list(self._running):
            info = self._alive(slug)
            if not info:
                continue
            tid = int(info["thread_id"])
            live_threads[slug] = tid
            row = None
            try:
                if info.get("task_id"):
                    row = db.query_one("SELECT started_at FROM tasks WHERE id = ?", (info["task_id"],))
                if row is None:
                    row = db.query_one("SELECT started_at FROM tasks WHERE thread_id = ? "
                                       "ORDER BY id DESC LIMIT 1", (tid,))
                th = db.query_one("SELECT title FROM threads WHERE id = ?", (tid,))
            except Exception:   # a DB hiccup must not hide the pool
                row, th = None, None
            title = ((th["title"] if th else "") or "").strip()
            if len(title) > 80:
                title = title[:79].rstrip() + "…"
            runs.append({"project": slug, "title": title,
                         "started_at": (row["started_at"] if row else None) or None,
                         "model": info.get("model") or ""})
        runs.sort(key=lambda r: (r["started_at"] or "", r["project"]))
        waiting = 0
        try:
            names = os.listdir(TASK_QUEUE)
        except OSError:
            names = []
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(TASK_QUEUE, n), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            kind = d.get("kind", "new")
            if kind not in ("new", "followup"):
                continue   # compact / stop are control messages, not work
            if kind == "followup" and live_threads.get(d.get("slug")) == int(d.get("thread_id") or 0):
                continue
            waiting += 1
        return {"max": max(MAX_PARALLEL_RUNS, 0), "busy": len(runs), "waiting": waiting, "runs": runs}

    def _handle(self, d, pool_full=False):
        """Route one descriptor. Returns "requeued_pool" when it had to wait for a free slot."""
        slug = d["slug"]
        kind = d.get("kind", "new")
        thread_id = int(d["thread_id"])
        alive = self._alive(slug)
        if kind in ("followup", "compact", "stop"):
            if alive and alive["thread_id"] == thread_id:
                self._drop_inbox(thread_id, {"kind": kind, "id": d.get("id"),
                                             "text": d.get("text", "")})
                return None
            if kind == "followup" and not alive:
                if pool_full or self._pool_full():
                    self._requeue(d)
                    return "requeued_pool"
                self._launch(d)   # resume via session_id if present
            # compact/stop with no live session → nothing to do
            return None
        # kind == new
        if alive:
            # project busy with another thread → requeue this descriptor for later
            self._requeue(d)
            return None
        if pool_full or self._pool_full():
            self._requeue(d)
            return "requeued_pool"
        self._launch(d)
        return None

    def _requeue(self, d):
        try:
            p = os.path.join(TASK_QUEUE, "%s.json" % d["id"])
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError as e:
            _log("requeue failed: %s" % e)

    @staticmethod
    def _queue_order(name):
        """Strict FIFO by task id (9 before 10); non-numeric control descriptors first."""
        stem = name[:-5] if name.endswith(".json") else name
        return (1, int(stem), "") if stem.isdigit() else (0, 0, stem)

    def poll_once(self):
        try:
            names = sorted(os.listdir(TASK_QUEUE), key=self._queue_order)
        except OSError:
            return
        pool_full = False
        for n in names:
            if not n.endswith(".json") or n.endswith(".tmp"):
                continue
            p = os.path.join(TASK_QUEUE, n)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                os.remove(p)
            except (OSError, ValueError):
                try: os.remove(p)
                except OSError: pass
                continue
            try:
                # Once the pool is full, every later order waits too (no overtaking).
                if self._handle(d, pool_full=pool_full) == "requeued_pool":
                    pool_full = True
            except Exception as e:
                _log("handle error for %s: %s" % (n, e))

    # ---- deploy the app the agent built --------------------------------
    def _secret_gate(self, project_id, src, thread_id) -> bool:
        """True if the built code is clear of hard-coded secrets. On a blocking
        finding: log it, tell the user in the live feed, and return False so the
        deploy is skipped and the project stays not-live."""
        try:
            scan = scan_tree(src)
        except Exception as e:   # a scanner bug must not wedge every deploy
            _log("secret scan error for %s: %s — deploying anyway" % (project_id, e))
            return True
        if not scan.get("blocking"):
            return True
        files = sorted({f["file"] for f in scan["findings"] if f["severity"] == "block"})
        preview = ", ".join(files[:5]) + (" …" if len(files) > 5 else "")
        _log("DEPLOY BLOCKED for %s: %d secret finding(s) in %s"
             % (project_id, scan["blocking"], preview))
        self._note_live(thread_id,
            "🚫 Live deploy blocked — the leak scanner found %d hard-coded secret(s) "
            "in the built code (%s). Remove them (read the value from an environment "
            "variable or operator-supplied config instead) and it will deploy."
            % (scan["blocking"], preview))
        return False

    def deploy(self, project_id, port, thread_id=None):
        """Build an image from the project workspace and run it as a sibling
        container on the given host port. If the workspace has no Dockerfile,
        a default static server is used so simple (index.html) apps just work.

        Before building, the workspace is scanned for hard-coded secrets (the same
        gate as publish): a real provider key / token / private-key block baked
        into the code is refused HERE, so it never lands inside a live image."""
        src = "%s/%s" % (WORKSPACES_BIND, project_id)
        if not os.path.isdir(src) or not os.listdir(src):
            return False
        if DEPLOY_SECRET_SCAN and not self._secret_gate(project_id, src, thread_id):
            return False
        ctx = tempfile.mkdtemp(prefix="aiw-build-")
        try:
            for entry in os.listdir(src):
                if entry in (".git", "node_modules", ".venv"):
                    continue
                s, d = os.path.join(src, entry), os.path.join(ctx, entry)
                if os.path.isdir(s):
                    shutil.copytree(s, d, ignore=shutil.ignore_patterns(".git", "node_modules", ".venv"))
                else:
                    shutil.copy2(s, d)
            if not os.path.exists(os.path.join(ctx, "Dockerfile")):
                with open(os.path.join(ctx, "Dockerfile"), "w", encoding="utf-8") as f:
                    f.write(DEFAULT_DOCKERFILE)
            tag = "aiwerkstatt-app-%s:latest" % project_id
            self._client.images.build(path=ctx, tag=tag, rm=True, forcerm=True)
        except Exception as e:
            _log("build failed for %s: %s" % (project_id, e))
            shutil.rmtree(ctx, ignore_errors=True)
            return False
        shutil.rmtree(ctx, ignore_errors=True)
        name = "aiwerkstatt-app-%s" % project_id
        try:
            self._client.containers.get(name).remove(force=True)
        except Exception:
            pass
        try:
            self._client.containers.run(
                tag, detach=True, name=name,
                ports={"8080/tcp": int(port)},
                restart_policy={"Name": "unless-stopped"},
                # Same lockdown as the agent-runner. The deployed app is the most
                # exposed container (it serves on a host port), so it gets no Linux
                # capabilities and cannot gain new privileges; it joins only the
                # default bridge, never the internal network, so it cannot reach the
                # control plane or the socket proxy.
                cap_drop=["ALL"], security_opt=["no-new-privileges"],
                mem_limit="1g", pids_limit=256)
            _log("deployed %s on host port %s" % (project_id, port))
            return True
        except Exception as e:
            _log("run failed for %s: %s" % (project_id, e))
            return False

    def purge_project(self, slug, thread_ids):
        """Remove everything a deleted project leaves behind so nothing survives on disk or in
        Docker: the deployed app container and its built image, the workspace tree, the
        persisted Claude session store, and every per-thread runtime file (inbox,
        live/sess/status/usage). Best-effort — a delete must fully clean up."""
        for op in (
            lambda: self._client.containers.get("aiwerkstatt-app-%s" % slug).remove(force=True),
            lambda: self._client.images.remove("aiwerkstatt-app-%s:latest" % slug, force=True),
        ):
            try:
                op()
            except Exception:
                pass
        for tree in ("%s/%s" % (WORKSPACES_BIND, slug), "%s/%s" % (AGENTHOME_BIND, slug)):
            shutil.rmtree(tree, ignore_errors=True)
        for tid in thread_ids or []:
            shutil.rmtree("%s/%s" % (INBOX_BIND, tid), ignore_errors=True)
            for p in ("%s/live-%s.jsonl" % (EVENTS_BIND, tid),
                      "%s/sess-%s.txt" % (EVENTS_BIND, tid),
                      "%s/status-%s.json" % (EVENTS_BIND, tid)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        try:
            udir = os.path.join(EVENTS_BIND, "usage")
            for f in os.listdir(udir):
                if f.startswith("%s-" % slug) and f.endswith(".json"):
                    try:
                        os.remove(os.path.join(udir, f))
                    except OSError:
                        pass
        except OSError:
            pass

    def run_loop(self, interval=1.0):
        if self._client is None:
            _log("docker SDK unavailable — orchestrator idle")
            return
        _log("started (image=%s, docker=%s)" % (config.AGENT_RUNNER_IMAGE, config.DOCKER_HOST))
        while True:
            try:
                self.poll_once()
            except Exception as e:
                _log("poll error: %s" % e)
            time.sleep(interval)
