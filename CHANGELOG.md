# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
