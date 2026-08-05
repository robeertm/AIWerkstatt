# Security model

Read this before you expose AIWerkstatt beyond `localhost`.

## It runs on your machine, for you

AIWerkstatt is a **single-operator, self-hosted** tool. It is **not** a
multi-tenant SaaS and is not designed to isolate mutually distrusting users. The
threat model is: *keep the agent contained on your own box, keep your provider keys
and private data from leaking out, and don't hand the daemon more power than the
job needs.* Everything below serves that goal.

## The Docker socket is never in the web container

The control plane (`web`) can start and stop containers, but it **never touches the
raw Docker socket**. It talks to the daemon only through a hardened
[`docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy), which is
the **only** container that mounts `/var/run/docker.sock` (read-only).

The proxy exposes a **minimal allowed API surface** — just what the orchestrator
needs to run agent and app containers:

| Enabled | Denied (default) |
|---|---|
| `CONTAINERS`, `IMAGES`, `VOLUMES`, `NETWORKS`, `EXEC`, `POST` | `INFO`, `SWARM`, `SECRETS`, `PLUGINS`, socket reconfiguration — and everything else |

So even if the web process were compromised, it cannot read Swarm secrets,
enumerate the host via `INFO`, load plugins, or reconfigure the socket. It can only
do container/image/volume/network lifecycle work.

## Agent-runner containers are unprivileged

Every task runs in a throwaway container the orchestrator launches with tight
limits:

- `cap_drop: ALL` — no Linux capabilities.
- `security_opt: no-new-privileges` — no privilege escalation.
- `pids_limit` and `mem_limit` — bounded process and memory footprint.
- **No Docker access.** The runner has no socket, no proxy address — it cannot
  start containers or reach the daemon at all.
- Runs as the shared non-root uid `10001`.

It gets network access (it needs the provider API, `git`, `npm`, etc.) and its own
`workspaces/<slug>` subtree — nothing more.

## Provider keys live in the app's private volume

Your provider API keys / tokens are stored in the **web container's private `data`
volume** with restrictive permissions. They are:

- **not in the repository**, and **not baked into any container image**;
- looked up at launch time and passed to an agent-runner **only as environment for
  that one ephemeral container**, never written to its command line;
- gone when that container is removed.

## The leak scanner guards what you publish

`backend/scrub/scan.py` is a **deterministic** scanner that runs before anything is
pushed to a public repo (and in CI on every PR). It blocks on real secret patterns
— provider keys, GitHub/Slack/AWS tokens, private-key blocks, real `.env` and
credential files — and reports weaker signals for review without blocking.

The rules are **data, not code** (`rules.default.toml`, generic patterns only), and
a maintainer can layer a **private overlay** via
`AIWERKSTATT_SCRUB_RULES=/path/to/rules.local.toml` to enforce their own deny-list
(personal names, home addresses, internal hostnames) **without committing it** to
the public repo. A blocking finding fails the scan; fix the file rather than
weakening the rule.

## Honest residual risk

Two things you should hold in mind:

1. **Whoever can start containers can mount volumes.** The proxy deliberately
   allows container and volume operations — that's the whole job. Anyone who can
   drive the web UI can therefore reach the shared volumes and, through them, the
   host resources those volumes expose. So the **web UI is admin-authenticated**,
   and you should **not expose it to untrusted networks**. Keep it on `localhost`
   or behind your own trusted access layer.

2. **An agent runs code you asked it to build.** By design the agent writes and
   executes real programs in its workspace and can reach the network. Treat your
   **provider keys and workspaces accordingly**: assume code the agent produces or
   runs could act with the access you've granted the run. Don't point AIWerkstatt
   at credentials or data you wouldn't hand to an autonomous build step.

Within that model — one trusted operator, on their own machine — AIWerkstatt keeps
the socket minimal, the runners unprivileged, the keys out of images and the repo,
and private data out of anything you publish.
