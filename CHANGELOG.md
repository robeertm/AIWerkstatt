# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
