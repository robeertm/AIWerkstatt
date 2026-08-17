# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.10.1 — 2026-08-17

### Fixed — self-update no longer loops on a stale version
- The app reported its version from a hard-coded constant in `backend/config.py` that could drift from `pyproject.toml`. When it did, the update check compared the old constant against the latest release and kept offering an update that never “took” — and because installing recreates the web container mid-request, the button appeared to hang. **`config.py` now reads the version from `pyproject.toml`** (bundled into the image), the documented single source of truth, so a shipped build always reports its real version and the “update available” banner clears after updating.

## 0.10.0 — 2026-08-17

### Added — 🤖 Auto mode: the AI picks the model + intensity per task
- New model choice **🤖 Auto** (first option for the Claude provider). When a project is on Auto, the engine picks a concrete model + reasoning effort for **every** task and follow-up — cheap models for tiny tweaks, the strongest for hard work — resolved before the run container starts, so isolation/orchestrator/runner are unchanged.
- **Transparent, provider-aware heuristic** (`backend/automode.py`): scores request length, bug/analysis/refactor keywords, breadth, attachments and new-vs-follow-up, then maps onto whatever models the selected provider exposes (strongest → weakest). No second AI round — no latency, no tokens spent just to decide.
- **Effective switching across a thread:** the big first order gets the strongest model; a small "make the button blue" follow-up automatically drops to a cheaper one.
- **Visible in the live view:** a 🤖 Auto chip with the **reason** (💡 why this model) and an expandable **plan** (which model at which step). The 🧠 model / ⚡ intensity chips show the concrete pick.
- **Overview:** the Usage panel gains a **Model usage** breakdown — which model·intensity combo ran most, with the Auto share. New endpoint `GET /api/model-usage`.

### Added — 📚 Project vault: shared knowledge that survives across runs
- Every project now has a **persistent knowledge base** on a shared `vault` volume, mounted read/write into each agent run. The agent is told to read `/vault/<project>/knowledge.md` at the start of a run and append important facts, decisions and gotchas — so **future runs remember** the project instead of starting cold.
- New **📚 Project knowledge** panel in the project view shows the accumulated notes; new endpoint `GET /api/projects/<id>/vault`.
- The `web` container mounts the same volume to seed and serve it; the vault persists across redeploys.

## 0.9.0 — 2026-08-13

### Fixed — a project no longer shows as "working" after an old task is wrongly resurrected
- The self-healer measured a task's age from `COALESCE(finished_at, started_at, created_at)` —
  the **newest** timestamp, which moves forward with every retry. A long-abandoned task
  therefore looked "fresh" (its `finished_at` was today) and got resurrected forever, defeating
  the max-age window meant to stop zombie revivals — and lighting up a project as "in progress"
  when nothing real was running. Age is now taken from the **immutable `created_at`** (the
  original request time): once too old, it stays too old, so only genuinely recent tasks keep
  self-healing.

### Added — the live "What the agent is doing" view now shows model + intensity
- New chip at the top of the live feed: **🧠 model · ⚡ intensity** (e.g. "Opus 4.8 · very
  high"), so you can see which model and reasoning effort the run is actually using. The runner
  writes both into its status file (`write_status`), `read_live` returns them, and the feed
  renders a label per value.

## 0.8.0 — 2026-08-11

### Added — "What the agent is doing" now shows every last bit (full text + full screen)
- The live activity feed used to summarise as it parsed: a tool result was collapsed to its
  first line plus `… (140 lines)`, thinking/output was flattened onto one line and clipped at
  300 chars. You could see *that* something happened, but not *what*.
- **Runner (`agent-runner/providers/claude_code.py`):** every live-activity event now also
  carries a `full` field — the unclipped content with newlines preserved (the whole tool
  result, the whole thought, the whole multi-line command / input JSON). Capped only against a
  pathological dump (`_FULL_MAX` = 20 000 chars per entry). The compact one-liner (`text`) is
  unchanged for the default view.
- **Backend (`engine.read_live`):** entry cap 400 → 1500 (the detail view scrolls far back),
  plus a 3 MB per-fetch full-text budget handed out newest-first, so a fresh load of a long run
  stays light on mobile while the most recent steps keep their full detail.
- **Frontend:** a persisted **"📜 Full text"** toggle (flip on once, stays on) renders every
  line ungated; in compact mode, rows with more behind them carry a **▸** and expand
  individually on click; a **⤢ full-screen** button opens the whole log in a large scroll area
  (Esc closes). Stable per-row ids instead of array index (no wrong row expanding on reload),
  history cap 250 → 2000.

## 0.7.0 — 2026-08-11

### Changed

- **Self-healing now retries a stalled run until it succeeds, instead of giving up.** When a
  run ends without completion (runner hard-killed, or a transient provider/API error mid-turn,
  leaving no terminal event), `engine.resurrect_stale_tasks` (replacing `reap_stale_tasks`)
  automatically re-queues it — with **no cap** — until it succeeds. Mars-grade watchdog
  behaviour: never give up, but retry with a **growing cooldown** (90 s → 180 → 360 → 720 →
  900 s cap; `AIWERKSTATT_RETRY_BASE_SEC` / `_CAP_SEC`) rather than hammering, and pause
  entirely while a usage limit is active. A task ends only on **success** (`reply`) or a
  **manual stop** (`cancelled`). A sane window (`AIWERKSTATT_RETRY_MAX_AGE_SEC`, default 12 h)
  bounds "until success" so neither ancient tasks resurrect nor a genuinely broken one hammers
  forever.
- New thread status **`retrying`** in the UI + a timeline event per attempt; `_thread_status`
  reports a stalled/failed run as `retrying` (self-healing) instead of `failed`/`done`, and the
  gallery counts it as busy.

## 0.6.3 — 2026-08-11

### Fixed

- **A project no longer hangs "in progress" forever after a runner dies mid-task.** If an
  agent container was hard-killed (OOM, host restart) or a transient provider/API error
  ended a turn before any terminal event was written, its task stayed `queued`/`running`
  and both the gallery and the thread status stuck on "working"/"queued" indefinitely.
  - New self-healing sweep `engine.reap_stale_tasks` (runs in the ingest loop): finalizes a
    task that is non-terminal while no runner container is alive, its queue descriptor has
    already been claimed, and its last progress is older than 20 min
    (`AIWERKSTATT_STALE_TASK_MIN`) → marks it `failed` and posts a terminal "please send the
    request again" event. Idempotent, no auto-retry, paused while a usage limit is active.
  - `_thread_status` now reports a genuinely failed task as `failed` instead of masking it
    as `done`.

## 0.6.2 — 2026-08-10

### Fixed

- **Deleting a project now really removes everything.** A delete previously dropped only the
  database rows (and even left the per-user read markers behind), so the built code, the
  conversation transcript, the persisted session store, the per-thread event/inbox files and the
  built container image all lingered on disk and in Docker — a "deleted" project was still
  recoverable and its data stayed around. A delete now also removes the workspace, the Claude
  session store, the inbox and event/usage files, the `thread_seen`/`pubmeta` rows, and the built
  app image (the running container was already removed). Nothing survives a delete.

## 0.6.1 — 2026-08-10

### Fixed

- **Follow-ups no longer dead-end after the agent's runner has closed.** Each agent run is a
  throwaway container, and Claude's session store (`~/.claude`) lived inside it — so once the
  short idle window passed and the runner exited, the next follow-up started a fresh container
  where `--resume <session>` couldn't find the conversation and failed instantly ("The run did
  not finish cleanly. It will be retried automatically."), which made the project look stuck and
  swallowed every further request. The session store is now persisted **per thread** on the
  internal events volume (`CLAUDE_CONFIG_DIR`), so a follow-up resumes with full context across
  containers. If the stored transcript for the requested session isn't there — a conversation
  started before this fix, or a lost store — the run starts a **fresh session on the existing
  workspace** instead of dead-ending; the files carry the state. The misleading "will be retried
  automatically" wording (nothing actually retried it) is replaced with an honest message.

## 0.6.0 — 2026-08-10

### Added

- **One-click launchers — no command line needed.** Download the release zip, unzip, and
  double-click `AIWerkstatt.command` (macOS), `AIWerkstatt.bat` (Windows) or `AIWerkstatt.sh`
  (Linux). The launcher checks Docker (starting Docker Desktop if needed), builds and starts
  everything, waits for the app, and opens your browser at http://localhost:8095. Docker is
  the only thing you install yourself.
- **Active "Check for updates" button.** The admin header now has a live update check that
  asks GitHub for the latest release on demand — a plain reload uses a 1-hour cache, so a
  freshly published release no longer stays hidden until it expires. `GET /api/update?force=1`
  bypasses the cache; the button shows "✓ You're on the latest" or the one-click **Update
  now** banner.

## 0.5.0 — 2026-08-10

### Security

- **Secret gate on live deploy.** The deterministic leak scanner that guards publishing
  now also runs before a project is built into a live image. A hard-coded secret
  (provider key, token, private-key block, real credential file) **blocks the deploy** —
  the project stays not-live and the reason shows up in the live activity feed — so a
  key can no longer be baked into a running app. On by default; opt out with
  `AIWERKSTATT_DEPLOY_SECRET_SCAN=0`.
- **Deployed app containers are hardened like the runner.** The container a project runs
  in now drops all Linux capabilities (`cap_drop: ALL`) and forbids privilege escalation
  (`no-new-privileges`), matching the agent-runner. It is the most exposed container
  (it serves on a host port), holds no keys, mounts no shared volumes, and stays on the
  default bridge only — so a vulnerability in a built app is contained to that one
  throwaway container.

## 0.4.3 — 2026-08-08

### Added

- **Real parallel subagents for the Claude provider.** An agent can now fan work out to
  parallel subagents via the **Agent tool** — the subagents run **synchronously within the
  same run** (same process tree) and return their results, so the agent can reassemble,
  verify and commit before it replies. Previously agents that tried to parallelise reached
  for detached background shell jobs (`nohup … &`), whose output was lost the moment the
  run ended. The Agent tool is now enabled for the Claude adapter, and a per-run rule
  (`--append-system-prompt`) steers agents to use it — and never to background work or
  promise to "report back later". The wait ceiling for subagents is raised to 30 minutes
  (`WK_BG_WAIT_CEILING_MS`); genuine hangs are still caught by the stall watchdog. Subagent
  token usage is included in the run's totals, so the usage view accounts for it. The
  context bar ignores subagent snapshots (`parent_tool_use_id`) so it stays accurate. Other
  providers are untouched.

## 0.4.2 — 2026-08-06

### Fixed

- **A project no longer shows as "in progress" forever after an agent dies unexpectedly.**
  The gallery marked a project busy whenever any of its threads had a `running` task.
  Normally the agent writes a terminal event (reply/failed) as it finishes, which clears
  that — but if the agent container dies *hard* (out-of-memory, killed, host restart)
  before it can, the task stays `running` with no live agent, and the project stayed
  flagged busy indefinitely (and never showed a finish). The busy indicator is now
  liveness-aware: a `queued` task always counts, but a `running` task counts only when
  its agent container is actually alive. A leftover `running` row from a vanished runner
  no longer pins the project to "in progress".

## 0.4.1 — 2026-08-06

### Fixed

- **No more made-up resume time when a usage limit has no known reset.** When an agent
  hit a usage limit and the provider did **not** report a real reset time, the pause
  message and the top banner still showed a concrete clock time — actually just the
  internal retry cadence (`now + 30 min`), which reads as a bogus value. Agents here run
  on the project's own credential (typically an API key, which has no plan "session
  reset"), so that time was never authoritative. Now a clock time is shown **only** when
  the provider gives a genuine reset; otherwise the message reads "resumes automatically
  once the limit resets" and the agent still retries in the background. When a real reset
  **is** reported, it is shown exactly as before.

## 0.4.0 — 2026-08-05

### Added

- **See your real Claude plan usage in the cockpit.** Each user can optionally
  connect their own Claude account (a standard OAuth login, read-only) from the
  **📊 Usage** panel. Once connected, the panel shows your live **session** and
  **weekly** utilisation with the session reset time — the same numbers the Claude
  `/usage` dialog reports. Disconnect anytime.
- **Why a separate connect:** the usage endpoint requires the `user:profile` scope,
  which a project's inference token doesn't carry — so it uses a per-user login
  token, kept only server-side and refreshed automatically. Everything degrades
  gracefully: not connected, or any hiccup, simply shows nothing. New backend
  `planlimit.py` (+ `/api/planlimit…` routes); no change to how agents run.

## 0.3.3 — 2026-08-05

### Fixed

- **A hung agent no longer blocks its project (stall watchdog).** If the agent
  process stops producing output while tasks are still pending — for example when
  it gets stuck in an API-retry dead end or on a dead socket — the session used to
  stay open indefinitely (the idle timeout only fires when nothing is pending),
  holding the project's slot so new requests were never picked up. Now the session
  terminates a silent agent after `WK_STALL_TIMEOUT` (default 600 s) and re-queues
  its tasks for automatic retry, so the workshop self-heals. The stop control
  remains an instant manual reset.

## 0.3.2 — 2026-08-05

### Fixed

- **A transient API error no longer triggers a bogus "usage limit" pause.** When a
  streaming response was interrupted (`server_error` / "Connection closed
  mid-response", reported as `terminal_reason: "api_error"`), the Claude adapter
  treated it as a usage limit — which pauses every project and shows a false
  "usage limit reached" banner. Now only real limits pause the workshop
  (`error == "rate_limit"` / HTTP 429, plus the `rate_limit_event` with status
  `rejected`); a transient failure falls through to the normal retry path, so the
  task is simply re-run without stopping anything else.

## 0.3.1 — 2026-08-05

### Changed

- **Sessions close quickly once the agent is done.** The window a task session
  stays open for a fast follow-up is now **30 s** (was 8 minutes). When the agent
  finishes, the slot frees up right away and the "session open" indicator clears;
  a later follow-up resumes the session with full context intact. Interjecting
  while the agent is still working is unaffected. Override with `WK_IDLE_TIMEOUT`.

## 0.3.0 — 2026-08-05

### Added

- **Running version always visible** — a version chip in the header and on the
  login screen, served from `/api/health`.
- **One-click in-app updater.** When a newer release is available, admins get an
  **Update now** button; a small `updater` sidecar (shipped in the compose file
  from first install) does `git pull`, rebuilds the images, and recreates the web
  container, then the UI reloads on the new version. It reaches Docker only through
  the hardened proxy — never the raw socket. The manual `git pull && docker compose
  up -d --build` remains as a fallback.

## 0.2.0 — 2026-08-05

### Added

- **Demo retires once you connect a real provider.** As soon as any credential-based
  provider (Claude/OpenAI/Gemini) is connected, the zero-key demo is removed from the
  provider choosers and the providers panel; it returns when no credential is left.

- **Live agent feed.** Watch what the agent is doing while it works — its narration,
  the files it writes and edits, and each tool result — streamed step by step into
  the task thread. Interject at any time; it folds your note into the running task.

- **Publish & release.** Push a project's app to a GitHub repo you own (connect a
  token once), or download it as a zip — always leak-scanned first. Tag a release
  on the published repo.
- **Usage view.** Output tokens and cost per provider, today and all-time.
- **User self-management.** Change your PIN from the app, with a guided first-run
  flow that replaces the default PIN.
- **Admin user manager.** Add and remove users and set roles from the UI.
- **Live activity strip** on a project — what each thread is doing right now.
- **Unread markers.** Per-user badges highlight new agent replies on projects and tasks.
- **Rate-limit banner** and a **self-update banner** when a newer release is available.
- **Onboarding overlay** with a short getting-started guide (reopen any time via ❔).

### Changed

- License is now **source-available** (© Robert Manuwald): free to run for your own
  use; modification and redistribution require permission. The apps you build are yours.

## 0.1.0 — 2026-08-05

Initial public release.

### Added

- **Self-hosted, one-command install.** `docker compose up` brings up the whole
  workshop — runs on Windows, macOS and Linux.
- **Multi-provider support** out of the box: Anthropic Claude, OpenAI (Codex), and
  Google Gemini. Pick the provider and model **per project**.
- **Chat-style task threads** with **live streaming** of the agent's progress,
  **follow-ups** folded into a running task, **auto-compaction** so a session never
  dies on a full context, and a **stop** control.
- **File browser** for inspecting each project's workspace.
- **Deterministic leak scanner** that blocks secrets and private data before
  anything is published, with rules externalisable for private overlays.
- **Contained execution:** each task runs in an unprivileged, throwaway container;
  the control plane reaches Docker only through a hardened socket proxy.
