# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
