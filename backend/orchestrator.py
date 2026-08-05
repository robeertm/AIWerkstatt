"""Orchestrator — launches one ephemeral agent-runner container per task.

This is the portable replacement for the private fork's root `dispatch-broker`
+ `runuser` + `/srv/wk` model. It runs INSIDE the web container and talks to the
Docker daemon ONLY through the hardened socket proxy (`DOCKER_HOST=tcp://
dockerproxy:2375`). It never touches the raw socket, and the containers it starts
are unprivileged (cap-drop ALL, no-new-privileges, no docker access).

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
import threading
import time

import config

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
        first = self._write_first(thread_id, d.get("text", ""))
        env = {
            "WK_SLUG": slug,
            "WK_THREAD_ID": str(thread_id),
            "WK_REPO": d.get("repo", ""),
            "WK_PROVIDER": provider,
            "WK_MODEL": d.get("model", ""),
            "WK_EFFORT": d.get("effort", ""),
            "WK_ALLOWED": d.get("allowed_tools", ""),
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
