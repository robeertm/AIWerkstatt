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
from scrub.scan import scan_tree

try:
    import docker  # docker-py
except Exception:  # pragma: no cover - import guard for tooling
    docker = None

APP_UID = os.environ.get("AIWERKSTATT_APP_UID", "10001")
TASK_QUEUE = os.path.join(config.DATA, "task-queue")
FIRST_DIR = "/inbox"          # per-thread subdir created here
EVENTS_BIND = "/events"
WORKSPACES_BIND = "/workspaces"
INBOX_BIND = "/inbox"

# Actual Docker volume names (compose gives them explicit names). The runner and
# the web container must reference the SAME names on the daemon.
VOL_WORKSPACES = os.environ.get("AIWERKSTATT_VOL_WORKSPACES", "aiwerkstatt-workspaces")
VOL_EVENTS = os.environ.get("AIWERKSTATT_VOL_EVENTS", "aiwerkstatt-events")
VOL_INBOX = os.environ.get("AIWERKSTATT_VOL_INBOX", "aiwerkstatt-inbox")

MEM_LIMIT = os.environ.get("AIWERKSTATT_RUN_MEM", "2g")
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

    # ---- launch -----------------------------------------------------------
    def _launch(self, d):
        slug = d["slug"]
        thread_id = int(d["thread_id"])
        provider = d.get("provider") or "claude"
        # Ensure the project's workspace dir exists (shared volume, uid 10001) — the
        # runner uses it as cwd, which would fail if it were missing.
        os.makedirs("%s/%s" % (WORKSPACES_BIND, slug), exist_ok=True)
        first = self._write_first(thread_id, d.get("text", ""))
        env = {
            "WK_SLUG": slug,
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
            "WK_WT": "%s/%s" % (WORKSPACES_BIND, slug),
            "WK_FIRST_MSG_FILE": first,
            "WK_FIRST_TASK": str(int(d["id"])),
            "WK_EVENTS": EVENTS_BIND,
            "WK_INBOX": "%s/%s" % (INBOX_BIND, thread_id),
            "WK_USAGE_DIR": "%s/usage" % EVENTS_BIND,
            "WK_RESUME_SID": d.get("session_id", "") or "",
        }
        env.update(self._creds_fn(provider) or {})   # e.g. {"ANTHROPIC_API_KEY": "..."}
        volumes = {
            VOL_WORKSPACES: {"bind": WORKSPACES_BIND, "mode": "rw"},
            VOL_EVENTS: {"bind": EVENTS_BIND, "mode": "rw"},
            VOL_INBOX: {"bind": INBOX_BIND, "mode": "rw"},
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
            self._running[slug] = {"container": container, "thread_id": thread_id}
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
    def _handle(self, d):
        slug = d["slug"]
        kind = d.get("kind", "new")
        thread_id = int(d["thread_id"])
        alive = self._alive(slug)
        if kind in ("followup", "compact", "stop"):
            if alive and alive["thread_id"] == thread_id:
                self._drop_inbox(thread_id, {"kind": kind, "id": d.get("id"),
                                             "text": d.get("text", "")})
                return
            if kind == "followup" and not alive:
                self._launch(d)   # resume via session_id if present
            # compact/stop with no live session → nothing to do
            return
        # kind == new
        if alive:
            # project busy with another thread → requeue this descriptor for later
            self._requeue(d)
            return
        self._launch(d)

    def _requeue(self, d):
        try:
            p = os.path.join(TASK_QUEUE, "%d.json" % int(d["id"]))
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
        except OSError as e:
            _log("requeue failed: %s" % e)

    def poll_once(self):
        try:
            names = sorted(os.listdir(TASK_QUEUE))
        except OSError:
            return
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
                self._handle(d)
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
