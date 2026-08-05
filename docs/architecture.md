# Architecture

How AIWerkstatt is put together — and why.

## The shape of it

Two containers run all the time; one more is created and thrown away for every
task; and each app the agent builds runs as its own sibling container.

```
            ┌─────────────────────────────────────────────┐
   browser ─┤  web  (control plane: UI + API + orchestrator│
            │        thread + SQLite)                       │
            └───────┬───────────────────────────┬──────────┘
                    │ talks to Docker ONLY via   │ shared named volumes
                    ▼ the hardened socket proxy  ▼ (workspaces + events + inbox)
            ┌───────────────┐          ┌──────────────────────────┐
            │  dockerproxy  │  starts  │  agent-runner (per task)   │
            │ (min. surface)│─────────▶│  provider CLI + session    │
            │  sees the sock│          │  driver → streams events   │
            └───────────────┘          └──────────────────────────┘
                    │ starts
                    ▼
            ┌──────────────────────┐
            │  your live app        │  ← sibling container, http://localhost:<port>
            └──────────────────────┘
```

### 1. `web` — the control plane (long-lived)

A single container running **Flask + the built React UI + an orchestrator thread +
SQLite**. It serves the gallery, the chat-style task threads, the file browser and
the API. Its SQLite database and your provider credentials live in its private
`data` volume.

Crucially, the web container **never touches the raw Docker socket**. It reaches
the daemon only through `DOCKER_HOST=tcp://dockerproxy:2375` — the hardened proxy.

### 2. `dockerproxy` — the only thing on the socket (long-lived)

The **only** container that mounts `/var/run/docker.sock`, and it mounts it
read-only. It exposes a **minimal allowed API surface** — just enough to create,
start and stop the agent-runner and app containers and manage their volumes and
networks. Everything else stays denied. See [security.md](security.md) for the
exact surface and why it matters.

### 3. `agent-runner` — one per task (ephemeral)

For each task the orchestrator launches a **throwaway container** from the
`agent-runner` image. Inside it: the provider's CLI plus a **provider-neutral
session driver** (`agent-runner/session.py`). The driver owns the long-lived agent
session, the ordered turn queue, auto-compaction, usage tracking and limit
handling; everything provider-specific sits behind an `AgentAdapter`
(see [providers.md](providers.md)). The container is unprivileged and has no Docker
access of its own. When the task ends, the container is removed.

### 4. Your live apps — siblings on host ports

When the agent deploys an app it built, that app runs as its **own sibling
container** on a host port (from `AIWERKSTATT_PORT_RANGE`, default `8100-8199`),
and the UI gives you an "open live" link at
`http://<AIWERKSTATT_PUBLIC_HOST>:<port>`.

## The file-drop-box design

The engine and the runner are **decoupled through files on shared volumes** — no
RPC, no shared process. This is deliberately simple and robust:

1. The engine writes a **task descriptor** JSON into `data/task-queue/<id>.json`
   (`{id, slug, thread_id, repo, kind, text, provider, model, effort, session_id?}`).
2. The orchestrator polls the queue and either **launches** a new agent-runner or,
   if a session for that project is already live, **feeds** it by dropping a
   follow-up/compact/stop message into the thread's **inbox**.
3. The runner **streams typed events** — one JSON file per event — into the shared
   `events` volume.
4. The engine **ingests** those event files into SQLite, which drives the live UI.

Because the contract is just "files in a directory," either side can restart
without a handshake, and a project's work is naturally serialised: a "new" task for
a project that's already busy is simply requeued for later.

## Shared named volumes

Three named volumes are mounted into **both** the web container and every
agent-runner:

| Volume | Holds |
|---|---|
| `workspaces` | One subdirectory per project (`/workspaces/<slug>`) — the agent's working tree. |
| `events` | Typed event JSONs the web control plane ingests (plus status/usage/limit files). |
| `inbox` | Per-thread follow-ups, compact requests and stop signals fed to a live session. |

A fourth volume, `data`, is **private to the web container** — SQLite, provider
credentials, the task queue.

### Why named volumes, not bind mounts

Named volumes are **portable across Windows, macOS and Linux**. Bind mounts drag in
host-path and permission differences that make a "one `docker compose up`" promise
fragile across those platforms. Named volumes keep the experience identical
everywhere.

### Why a shared uid

The web container and the agent-runner both run as **uid 10001**. Because they
share that uid, files either side writes on the shared volumes are
**readable, writable and deletable by the other** — no permission tug-of-war when
the engine cleans up events the runner wrote, or the runner reads a first-message
file the engine dropped.

## The provider-adapter boundary

The session driver is **provider-neutral by design**. It never knows how a
particular CLI is invoked or how its output looks — it only speaks in normalised
events (`session`, `ctx`, `limit`, `result`). Each provider supplies an
`AgentAdapter` that translates: build the argv, encode a user message onto stdin,
parse one raw output line into normalised events, and (optionally) trigger
compaction. Adding a provider therefore touches only two small files and leaves the
driver untouched — see [providers.md](providers.md) and
[../CONTRIBUTING.md](../CONTRIBUTING.md).
