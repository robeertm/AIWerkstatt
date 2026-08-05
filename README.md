<div align="center">

# 🛠️ AIWerkstatt

### Your own self-hosted AI app workshop.
**Talk to a coding agent in plain language — watch it build, deploy, and live-serve a real web app you can keep nudging.**

One `docker compose up`. Any AI provider. Runs on your machine — Windows, macOS, Linux.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Runs on Docker](https://img.shields.io/badge/runs%20on-Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Providers](https://img.shields.io/badge/providers-Claude%20·%20OpenAI%20·%20Gemini-6E56CF)](#-connect-your-ai-provider)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

</div>

---

> **AIWerkstatt** turns a coding agent into a workshop you *own*. Instead of copy-pasting
> snippets from a chat window, you send a request — *"build me a habit tracker with a
> weekly chart"* — and AIWerkstatt spins up an isolated agent, lets it build a full web app,
> deploys it live on your machine, and shows you the result with a link you can open.
> Not happy? Just say so in the same thread; the agent keeps working on it.

It’s the friendly front-end you wish coding agents came with: a project gallery, chat-style
task threads with a live activity ticker, a file browser, and — critically — **it’s yours**.
No SaaS, no lock-in, no data leaving your box. Bring your own AI key.

## ✨ Why AIWerkstatt

- **🖱️ One-command install.** `docker compose up`. That’s the whole setup. Windows, macOS, Linux.
- **🔌 Bring any AI provider.** Anthropic Claude, OpenAI, and Google Gemini out of the box — pick per project. Adding another is a small plug-in.
- **💬 Chat, don’t prompt-engineer.** Describe what you want like you’d tell a person. Follow up mid-run — the agent folds your note into what it’s already doing.
- **📦 Isolated by design.** Every agent run and every app it builds runs in its **own container**. The agent can’t touch your machine, only its workspace.
- **🚀 Live apps, instantly.** Each project deploys to a local port and gets a “open live” link. Watch it come together.
- **🗂️ See everything.** Live thinking ticker, full file browser, context meter with one-click compaction, stop button — all the controls, none of the terminal.
- **🔒 Leak-safe sharing.** A built-in scanner blocks secrets and private data before anything is ever pushed to a public repo.

## 🚀 Quickstart (1 minute)

**Prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS) or Docker Engine (Linux).

```bash
git clone https://github.com/OWNER/aiwerkstatt.git
cd aiwerkstatt
cp .env.example .env          # optional — sensible defaults, works as-is
docker compose build          # builds the web UI + the agent-runner image
docker compose up             # start it
```

Then open **http://localhost:8095**, create your admin login, connect a provider (below),
and send your first request. 🎈

## 🔌 Connect your AI provider

On first run AIWerkstatt asks you to connect at least one provider. Your key is stored
**encrypted in the app’s private volume** — never in the repo, never sent anywhere but the
provider you chose. Pick per project which provider and model to use.

| Provider | How to connect | Get a key |
|---|---|---|
| **Anthropic Claude** | API key *or* a Claude subscription (`claude setup-token`) | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| **OpenAI** | API key *or* ChatGPT sign-in | [platform.openai.com](https://platform.openai.com/api-keys) |
| **Google Gemini** | API key *or* Google sign-in | [aistudio.google.com](https://aistudio.google.com/apikey) |

> Don’t have a key yet? Each connect screen links straight to the page where you create one,
> with a short step-by-step. It takes about a minute.

## 🧠 How it works

```
            ┌─────────────────────────────────────────────┐
   browser ─┤  web  (control plane: UI + API + orchestrator)│
            └───────┬───────────────────────────┬──────────┘
                    │ talks to Docker ONLY via   │ shared named volumes
                    ▼ the hardened socket proxy  ▼ (workspace + events)
            ┌───────────────┐          ┌──────────────────────┐
            │  dockerproxy  │  starts  │  agent-runner (per run)│
            │ (min. surface)│─────────▶│  provider CLI + stream │
            └───────────────┘          └──────────────────────┘
                    │ starts
                    ▼
            ┌──────────────────────┐
            │  your live app        │  ← opens at http://localhost:<port>
            └──────────────────────┘
```

- The **web** container never touches the raw Docker socket — only a hardened
  [socket proxy](docs/security.md) with a minimal allowed API surface.
- Each **agent run** gets its own throwaway container with the provider CLI; it works in an
  isolated **workspace volume** and streams progress back over a shared **events volume**.
- The **app the agent builds** runs as a sibling container and is served live locally.

More detail: [docs/architecture.md](docs/architecture.md) · [docs/providers.md](docs/providers.md) · [docs/security.md](docs/security.md)

## 🔐 Security

AIWerkstatt is built to run **on your own machine for you** — not as a multi-tenant SaaS.
The agent is contained, the Docker socket is proxied down to a minimal surface, and a
deterministic leak scanner guards anything you choose to publish. Read
[docs/security.md](docs/security.md) before exposing it beyond localhost.

## 🤝 Contributing

Issues and PRs welcome — new provider adapters especially. See [CONTRIBUTING.md](CONTRIBUTING.md).
Every PR runs a leak scan and the test suite in CI.

## 📄 License

MIT © contributors. See [LICENSE](LICENSE).
