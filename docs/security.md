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

## Agent and app containers are unprivileged

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

**The app the agent builds gets the same lockdown.** When a project goes live it runs
as its own sibling container with `cap_drop: ALL`, `no-new-privileges`, and
memory/pid limits — the identical treatment. This matters because it is the most
exposed container (it serves HTTP on a host port): it holds **no** provider keys, mounts
**none** of the shared volumes, and sits on the default bridge only — never the internal
network — so a bug in the generated app cannot reach the control plane, the socket
proxy, your keys, or another project. A vulnerability is contained to that one
throwaway app container.

## Provider keys live in the app's private volume

Your provider API keys / tokens are stored in the **web container's private `data`
volume** with restrictive permissions. They are:

- **not in the repository**, and **not baked into any container image**;
- looked up at launch time and passed to an agent-runner **only as environment for
  that one ephemeral container**, never written to its command line;
- gone when that container is removed.

The GitHub token you connect for **Publish** follows the same rule: the push gets it as an
`http.extraheader` through the environment of that one `git` process (`GIT_CONFIG_COUNT` /
`GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0`). It is never part of a command line or a remote
URL — a process's command line is readable by every local user (container processes show up
in the host's process list), its environment only by the same user and root.

## The leak scanner guards what you publish — and what you deploy

`backend/scrub/scan.py` is a **deterministic** scanner that runs before anything is
pushed to a public repo (and in CI on every PR). It blocks on real secret patterns
— provider keys, GitHub/Slack/AWS tokens, private-key blocks, real `.env` and
credential files — and reports weaker signals for review without blocking.

The **same gate runs before a live deploy.** When the agent finishes and the
orchestrator is about to build the app into an image, it scans the workspace first; a
blocking secret **stops the deploy** — the project stays not-live and the reason
appears in the live feed — so a hard-coded key never ends up baked into a running
image. On by default; set `AIWERKSTATT_DEPLOY_SECRET_SCAN=0` to opt out.

The rules are **data, not code** (`rules.default.toml`, generic patterns only), and
a maintainer can layer a **private overlay** via
`AIWERKSTATT_SCRUB_RULES=/path/to/rules.local.toml` to enforce their own deny-list
(personal names, home addresses, internal hostnames) **without committing it** to
the public repo. A blocking finding fails the scan; fix the file rather than
weakening the rule.

## From "the agent wrote it" to "it's live"

Keep two failure modes apart:

- **A secret in the generated code** is caught by the scanner gate above: it runs
  before the image is built, so a real key / token / private-key block never reaches a
  live container. Same rules as publish, on by default.
- **A vulnerability in the generated code** is *not* caught by a scanner — AIWerkstatt
  does **no** static analysis of your app's logic, and it would be dishonest to imply
  otherwise. What bounds it is **containment**: the app runs in its own unprivileged,
  capability-stripped sibling container with no keys, no shared volumes, and no path to
  the control plane or other projects. So a flaw in a built app can compromise, at
  worst, that one throwaway container — not the host, your keys, or your other work.

If you want true vulnerability scanning, add it as a step (e.g. an image scan such as
Trivy, or a SAST pass) — the boundary here is deliberately *isolation-first*, not
verification.

## Service tokens

Signing in is the normal way in. Optionally, another local service (an assistant, a script,
an orchestrator) can call a small part of the API with `Authorization: Bearer <token>`.
No token exists by default.

**Storage.** Only the SHA-256 of a token is stored, in the database in the private data
volume. Manage tokens with the CLI inside the web container (run it as the app user so
the files in the data volume keep their owner):

```bash
docker compose exec -u app web python3 service_token.py list
docker compose exec -u app web python3 service_token.py create helper                  # restricted, prints the token once
docker compose exec -u app web python3 service_token.py create orchestrator --profile coordinator
docker compose exec -T -u app web python3 service_token.py register orchestrator --profile coordinator < token.txt
docker compose exec -u app web python3 service_token.py register orchestrator --profile coordinator --sha256 <hex digest>
docker compose exec -u app web python3 service_token.py remove helper
```

`register` is for tokens generated elsewhere: it takes the SHA-256 hex digest, or reads the
token from stdin (hidden on a terminal), hashes it and never prints it. Token names use
`a-z`, `0-9`, `.`, `_` and `-`.

**Profiles.**

| | restricted (default) | coordinator |
|---|---|---|
| List / view projects | ✅ (not the blocklisted ones) | ✅ all projects |
| View a task (status, history) | ✅ | ✅ |
| Create a task | ✅, restricted (review gate) | ✅, runs and deploys normally |
| Send a follow-up | ❌ | ✅ |
| Stop a task with a reason (`POST /api/coordinator/threads/<id>/stop`) | ❌ | ✅ |
| Create a project | ✅ | ❌ |
| Choose model / effort per task | ❌ (member rights) | ✅ |
| Write rate limit | — | 30 per hour per token |
| Release, publish, delete, settings, users, tokens, review decision | ❌ | ❌ |

Every other route treats a token as no user at all and answers **401**. Tokens created
before profiles existed are restricted and behave exactly as before.

- **Restricted tasks** never go live by themselves: the project workspace is snapshotted,
  the project is held (no deploy, publish or release) and a signed-in admin approves or
  rejects the task in the task view. `AIWERKSTATT_API_BLOCKLIST` (comma-separated project
  ids) hides projects from restricted tokens (403).
- **Coordinator tasks** are meant for a trusted orchestrator. They skip the review gate and
  the blocklist. A follow-up on a restricted task stays restricted, whoever sends it.

- **Reporter tasks** are restricted like the default profile (review gate, snapshot, hold).
  The profile exists for a service that files tasks for trusted reporters; see
  [Trusted reporters](#trusted-reporters).

- **Chain tasks** are restricted too, unless the operator sets `AIWERKSTATT_CHAIN_REVIEW=off`
  (then they run like coordinator tasks). See [Task chain](#task-chain).

**Stopping.** The coordinator can stop a task with `POST /api/coordinator/threads/<id>/stop` and
a body `{"reason": "<4-500 characters>", "replacement": <thread id>}` (`replacement` optional). The reason is
required (400 without), an unknown replacement answers 404. The thread gets a note with the reason, and each
stop is appended to `<data>/coordinator-stops.jsonl`. No other token profile can use the route; the plain
stop route stays sign-in only.

**Origin.** A task created with a token carries `origin = "api:<token name>"`, set on the
server from the verified token and never from the request body. Follow-ups record the
sending token too. The task list and the task view show it as a **via &lt;name&gt;** badge.

**Rate limit.** New tasks and follow-ups of a coordinator token are limited per token to
`AIWERKSTATT_API_WRITE_LIMIT` per hour (default 30, `0` = unlimited). Beyond that the API
answers **429** with `Retry-After`. The window is kept in memory, so a restart resets it.

**Kill switch.** Create the file `<data>/service-token-disabled/<token name>` (in Docker:
`/data/service-token-disabled/<token name>`) and the token answers **401** from the next
request on, without a restart. Delete the file to switch it back on. The CLI does the same
with `service_token.py disable <name>` / `enable <name>`.

**Audit log.** Every request that presents a known token, including refused and switched-off
ones, is appended to `<data>/service-token-audit.jsonl` (file mode 0600): time, token name,
profile, method, route, path, project and HTTP status. The token itself, its hash and the
request headers are never written.

### Trusted reporters

The `reporter` profile is deliberately the narrowest one. It allows exactly three routes:
create a task, read a task, send a follow-up.

- **One intake project.** Tasks can only be created in the project named by
  `AIWERKSTATT_REPORTER_PROJECT`. Without that variable, or for any other project, the API
  answers **403**.
- **Own tasks only.** Reading and follow-ups work only for tasks whose origin is the calling
  token (also **403** for unknown ids, so ids cannot be probed). There are no file uploads and
  no project listing.
- **Review gate always on.** Reporter tasks are restricted tasks: snapshot, hold, and an
  admin decision before anything goes live. There is no way to skip it, fail closed.
- **Own rate limit.** `AIWERKSTATT_REPORTER_RATE` writes per hour per token (default 8; `0`
  or an invalid value falls back to 8, never to unlimited), then **429** with `Retry-After`.
- Kill switch, audit log and origin badge work as for every token.

Keep the token on the host only. Deciding **who** counts as a trusted reporter belongs to the
calling service, not to AIWerkstatt: identify the reporter by an immutable numeric account id
rather than a display name or login, which can be renamed or reused. The service should not
process automatically content that looks suspicious. Hand these to a human instead: prompt
injection attempts, requests for credentials or privileges, security reports, legal or money
questions, contact outside the platform, and pull requests containing code.

### Task chain

The `chain` profile belongs to [`scripts/chain/chain.py`](chain.md), which files the next task
of a plan after each finished one. It allows the same three routes as `reporter`, with
tighter limits on creating:

- **One collecting project and a fixed title prefix.** `AIWERKSTATT_CHAIN_PROJECT` and
  `AIWERKSTATT_CHAIN_TITLE_PREFIX`. If either is unset, empty, or the request does not match,
  the API answers **403**.
- **Standard models only.** `model` must be empty, `auto`, or a model of the project's
  provider that the Auto ladder may pick. Special-purpose and legacy models, unknown
  models and unknown effort steps are refused with **403**. There is no per-task working
  folder in AIWerkstatt, so there is no folder rule.
- **No attachments.** A request carrying `files`, `attachments` or `attachment` is refused.
- **Own tasks only.** Reading and follow-ups only for tasks created by the calling token
  (**403** also for unknown ids and for tasks of other tokens or people).
- **Review gate on by default.** Chain tasks are restricted tasks. Only an operator who sets
  `AIWERKSTATT_CHAIN_REVIEW=off` lets them run unreviewed; the profile never gets wider routes.
- **Own rate limit.** `AIWERKSTATT_CHAIN_RATE` writes per hour per token (default 6; `0` or
  an invalid value falls back to 6), then **429** with `Retry-After`.
- The tool itself adds ceilings of 4 new tasks per day, 1 running per track and 2 running in
  total, a kill-switch file and a pause file. It never tags, releases or publishes.

Keep the token file at mode 0600; the tool refuses a token file readable by others and never
passes the token on a command line.

## Remote test runner

The optional tool in [`scripts/test-runner`](test-runner.md) runs a test suite on a second machine. It adds
no route and no permission to AIWerkstatt itself; the trust boundary is the SSH key of that machine:

- **Forced command.** The key is installed as `restrict,from="<client address>",command="python3 -I <dir>/remote_test_receiver.py" <key>`.
  The caller can only issue `status`, `run <request>` and `result <id>`; every other command is refused,
  and the install script proves this by trying `id` with the new key. `restrict` removes port, agent and X11
  forwarding and the terminal.
- **Allow list.** `run` only works for profiles and modes listed in `profiles.json` next to the receiver. The
  file must belong to root or to the receiver's user and must not be writable by others, otherwise the
  receiver refuses to start a run. Every request field is validated (ids, commit, repository name, `n`, arguments
  with a deny list for options that would redirect output or configuration, environment variables only from a
  per-profile allow list, pins in `package==version` form).
- **Package check.** The uploaded archive may contain only `source/` (and `extra/` where a mode allows it); absolute
  paths, `..`, hard links, links pointing outside, entries below links and special files are refused before anything
  is unpacked.
- **Contained containers.** `--rm --network none --cap-drop ALL --security-opt no-new-privileges --no-healthcheck`,
  a memory limit, a low `--cpu-shares`, a CPU limit (`--cpus` or `--cpuset-cpus`), a read-only root file system and
  a read-only source mount. No Docker socket, no other mount, never more than 2 runs at once.
- **Read-only status.** `status` returns load, cores, pressure, free RAM, CPU and the runner's own runs, and nothing
  about other containers or services of that machine.
- **No secrets in the package.** Only tracked files of one commit are packed (`git archive`), so untracked files such
  as `.env` never leave the client. The client configuration (target and key path) lives in a file or the
  environment, never in the code.

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

## The updater sidecar

One-click update is done by a small `updater` container, not by the web app
recreating itself (a process can't cleanly replace its own container). On
**⤴️ Update now**, the web app asks the updater to run `scripts/self-update.sh`,
which does `git pull`, rebuilds the images, and recreates the `web` container.

- The updater reaches Docker **only through the same hardened proxy** as the web
  app (`DOCKER_HOST=tcp://dockerproxy:2375`) — it never mounts the raw socket.
- It mounts the **project directory** (the compose file and git checkout) so it can
  pull source and run compose. That directory is your own repo clone.
- It runs the classic builder (`DOCKER_BUILDKIT=0`) for proxy compatibility.
- It is admin-only (the endpoint requires an admin) and only ever runs that one
  fixed script.

If you'd rather not have it, remove the `updater` service from `docker-compose.yml`
and update by hand with `git pull && docker compose up -d --build`.
