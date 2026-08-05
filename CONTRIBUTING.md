# Contributing to AIWerkstatt

Thanks for being here! AIWerkstatt is a self-hosted AI app workshop, and it gets
better with every issue and pull request. **PRs welcome — new provider adapters
especially.** If you want to wire up a coding-agent CLI we don't ship yet, that's
one of the most valuable things you can add, and it's a small, well-defined change.

## Dev setup

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(Windows/macOS) or Docker Engine (Linux).

```bash
git clone https://github.com/OWNER/aiwerkstatt.git
cd aiwerkstatt
cp .env.example .env          # optional — sensible defaults, works as-is
docker compose build          # builds the web UI + the agent-runner image
docker compose up             # start it
```

Open **http://localhost:8095**, create your admin login, connect a provider, and
you're running your own local build. Rebuild after changing backend or runner code
with `docker compose build`.

## Project layout

| Path | What it is |
|---|---|
| `backend/` | Flask API + orchestrator + SQLite — the **control plane** (the "web" service). |
| `backend/providers/` | Provider **metadata** for the UI (`ProviderSpec`): models, auth modes, key help. |
| `backend/scrub/` | The deterministic **leak scanner** (`scan.py`) and its rule data. |
| `agent-runner/` | The image launched **per task**: the provider CLI + the provider-neutral session driver. |
| `agent-runner/providers/` | Provider **run adapters** (`AgentAdapter`): argv, stdin encoding, stream parsing. |
| `frontend/` | The React UI (built into the web image). |
| `proxy/` | Notes/config around the hardened Docker socket proxy. |
| `docs/` | Architecture, security, providers, releasing. |

See [docs/architecture.md](docs/architecture.md) for how these fit together.

## Adding a new AI provider

A provider is two small files — one on each side of the container boundary:

1. **Control-plane spec** — `backend/providers/<name>.py`. Register a `ProviderSpec`
   describing the provider for the UI: `id`, `label`, the `cli` binary, its
   `models`, `default_model`, `efforts`, `auth_modes` (`api_key` and/or `oauth`),
   the `key_env` variable the runner expects, an optional `key_prefix` for a cheap
   format check, and a `key_help_url` / `key_help`. Import it from
   `backend/providers/__init__.py`.

2. **Run adapter** — `agent-runner/providers/<name>.py`. Implement an
   `AgentAdapter` and `register(...)` it. The session driver is provider-neutral;
   everything provider-specific lives behind these four methods:

   - `build_argv(ctx) -> list[str]` — the CLI command line (model, effort, resume
     id, allowed tools, whatever streaming/JSON flags the CLI needs).
   - `encode_user_message(text) -> bytes` — how a user turn is written to the CLI's
     stdin.
   - `parse_line(raw) -> list[dict]` — turn **one** raw stdout line into
     **normalised events** the driver understands (`session`, `ctx`, `limit`,
     `result`). See `agent-runner/providers/base.py` for the exact event shapes.
     Exactly one `result` is expected per message fed in.
   - `compact_message() -> bytes | None` — the line that triggers context
     compaction, or `None` if the CLI has no equivalent.

That's it — nothing else in the driver changes. `claude_code.py` is the reference
implementation; read it alongside `base.py` when writing your own.

## Coding conventions

- **Python 3.11+**, standard library first. The control plane and runner avoid
  heavy dependencies on purpose — keep new ones minimal and justified.
- Match the surrounding style; keep modules small and single-purpose.
- Rules and config are **data, not code** where it already works that way (scrub
  rules, provider specs). Prefer extending the data.
- Keep the session driver provider-neutral. Provider quirks belong in the adapter.
- **English only** for all repository docs, comments, and identifiers.

## Every PR is scanned and tested

CI runs on every pull request and must be green to merge:

- **Leak scan** — `python backend/scrub/scan.py .` must report **0 blocking**
  findings. Run it locally before you push; it's fast and has no dependencies.
- **Tests** — the test suite must pass.

The scanner keeps secrets and private data out of a public repo. If it flags one
of your files, fix the file — don't weaken the rule.

## Opening a PR

- Keep changes focused; one concern per PR.
- Describe what you changed and how you verified it.
- If you added a provider, say which CLI it wraps and how you tested a real run.

Welcome aboard, and thank you for contributing. 🛠️
