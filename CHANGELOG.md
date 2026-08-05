# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
