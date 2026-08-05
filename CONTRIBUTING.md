# Feedback & running your own build

Thanks for being here! AIWerkstatt is a self-hosted AI app workshop. It is
**source-available**: you're welcome to run it and read the code, but it isn't
open for redistribution or modified re-hosting (see [LICENSE](LICENSE)).

The best way to help is **[open an issue](https://github.com/robeertm/AIWerkstatt/issues)**:

- **Bugs** — what happened, what you expected, and how to reproduce it.
- **Feature ideas** — especially **new AI provider adapters**. If you'd like a
  coding-agent CLI that isn't shipped yet, say which one and how it streams; that's
  the single most valuable request.
- **Questions** — setup trouble, Docker quirks, anything unclear in the docs.

## Run it locally

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(Windows/macOS) or Docker Engine (Linux).

```bash
git clone https://github.com/robeertm/AIWerkstatt.git
cd AIWerkstatt
cp .env.example .env          # optional — sensible defaults, works as-is
docker compose build          # builds the web UI + the agent-runner image
docker compose up             # start it
```

Open **http://localhost:8095**, create your admin login, connect a provider, and
you're running your own local build.

## How the pieces fit

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

See [docs/architecture.md](docs/architecture.md) for how these fit together, and
[docs/providers.md](docs/providers.md) for how a provider is wired on each side of
the container boundary.

## House style (for reference)

- **Python 3.11+**, standard library first — the control plane and runner avoid
  heavy dependencies on purpose.
- The session driver stays provider-neutral; provider quirks live in the adapter.
- Rules and config are **data, not code** where it already works that way (scrub
  rules, provider specs).
- **English only** for all repository docs, comments, and identifiers.

Thanks for trying it, and for taking the time to send good feedback. 🛠️
