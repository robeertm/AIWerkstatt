<div align="center">

# 🛠️ AIWerkstatt

### Your own self-hosted AI app workshop.
**Talk to a coding agent in plain language — watch it build, deploy, and live-serve a real web app you can keep nudging.**

One `docker compose up`. Any AI provider. Runs on your machine — Windows, macOS, Linux.

[![License: Source-available](https://img.shields.io/badge/license-source--available-blue.svg)](LICENSE)
[![Runs on Docker](https://img.shields.io/badge/runs%20on-Docker-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Providers](https://img.shields.io/badge/providers-Claude%20·%20OpenAI%20·%20Gemini-6E56CF)](#-connect-your-ai-provider)
[![Free to self-host](https://img.shields.io/badge/free%20to-self--host-brightgreen.svg)](#-quickstart-1-minute)

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

> ### ⚠️ Please read before installing
>
> **This project is written with AI assistance.** Code, documentation and this
> README are largely AI-generated and reviewed by a human, not the other way
> round. Treat it as a hobby project, not as audited software.
>
> **It is provided "as is", with no warranty and no liability of any kind** —
> for errors, for lost or corrupted data, for what the agent does on your
> machine, for costs incurred at your AI provider, or for anything that follows
> from using it. **You use it at your own risk.**
>
> Three things deserve saying plainly, because this tool writes and runs code:
>
> - **The agent generates code and then runs it.** Nobody reviews it before it
>   executes. Treat every app it builds as untrusted until you have read it
>   yourself — especially before exposing one to a network.
> - **It spends your money at your AI provider.** Requests are billed to the
>   key you configure, and a long-running or looping task can spend more than
>   you expect. Set limits on the provider's side, not only here.
> - **Generated apps are not secure by default.** Authentication, input
>   validation and secret handling are only as good as what the agent happened
>   to write. Do not put anything that matters behind one without checking it.


## 📸 See it in action

<div align="center">

**Watch the agent work — every step it takes — and jump in any time. It builds, deploys, and keeps going as you chat. Stop or steer it whenever.**

<img src="docs/screenshots/14-live-agent.png" alt="A live task thread: a vital-signs line (command running for 1 min, last activity, task since), the agent's step-by-step activity feed with a timestamp on every line, a live session bar, and a Stop button" width="860">

</div>

**The agent at work, across the project**

<p align="center"><img src="docs/screenshots/13-activity.png" alt="Project view with a live activity ticker and an unread badge" width="640"></p>

**The finished conversation**

<p align="center"><img src="docs/screenshots/04-thread.png" alt="A completed task thread with an Open live link" width="640"></p>

**Your project gallery**

<p align="center"><img src="docs/screenshots/02-gallery.png" alt="Project gallery with the Agents at work strip: slot switch, remaining time per run, waiting orders and test runner load" width="640"></p>

**Priority in the queue** — urgent, next, up/down and drag and drop for waiting orders (running orders are never touched)

<p align="center"><img src="docs/screenshots/15-queue-priority.png" alt="The Queue list of the Agents at work strip: waiting orders in start order, one marked urgent, buttons for urgent, next, up and down" width="640"></p>

**Inside a project**

<p align="center"><img src="docs/screenshots/03-project.png" alt="Project view with per-project provider and model" width="640"></p>

**Connect any AI provider**

<p align="center"><img src="docs/screenshots/05-providers.png" alt="Connect Claude, OpenAI or Gemini — or use the zero-key demo" width="640"></p>

**The live app it built**

<p align="center"><img src="docs/screenshots/06-live-app.png" alt="A live app the agent built, served from its own container" width="640"></p>

**Publish — leak-scanned first**

<p align="center"><img src="docs/screenshots/10-publish.png" alt="Publish panel: scan for secrets, download a zip, or push to GitHub" width="640"></p>

**See what it costs**

<p align="center"><img src="docs/screenshots/08-usage.png" alt="Usage: tokens and cost, today and all-time, per provider" width="640"></p>

**Manage your household or team**

<p align="center"><img src="docs/screenshots/09-users.png" alt="Admin user manager" width="640"></p>

**Your account &amp; PIN**

<p align="center"><img src="docs/screenshots/11-account.png" alt="Change your PIN" width="640"></p>

**Pick a model for a single task**

<p align="center"><img src="docs/screenshots/12-task-model.png" alt="New task form with the optional Model field, set to Sonnet 5.5 for this one task" width="640"></p>

**A task in progress**

<p align="center"><img src="docs/screenshots/12-working.png" alt="A running task with the live feed, model and effort chips and a Stop button" width="640"></p>

**A short first-run guide**

<p align="center"><img src="docs/screenshots/07-onboarding.png" alt="Onboarding overlay" width="640"></p>

**Simple PIN sign-in**

<p align="center"><img src="docs/screenshots/01-login.png" alt="PIN login screen" width="640"></p>

## ✨ Why AIWerkstatt

- **🖱️ One-click install.** Download the release zip, unzip, and **double-click the launcher for your OS** — it builds everything, starts it, and opens your browser. No command line. (Prefer the terminal? `docker compose up -d --build` still works.) Windows, macOS, Linux.
- **🔌 Bring any AI provider.** Anthropic Claude, OpenAI, and Google Gemini out of the box — pick per project. Adding another is a small plug-in.
- **🤖 Auto mode — let it pick the model.** Set a project to **Auto** and the AI chooses the model and reasoning effort for every task: cheap models for tiny tweaks, the strongest for hard work. See exactly what it picked, why, and its plan — right in the live view. Admins can also pick a model for a single task when writing it, which beats both the project setting and Auto.
- **📚 It remembers your project.** Each project has a persistent knowledge base the agent reads at the start of every run and appends to as it learns — so the next run knows the architecture, decisions and gotchas instead of starting cold.
- **💬 Chat, don’t prompt-engineer.** Describe what you want like you’d tell a person. Follow up mid-run — the agent folds your note into what it’s already doing.
- **👀 Watch it work, step by step.** A live feed shows what the agent says, thinks, and does — every file it writes, every command it runs — as it happens, each line with its time. A vital-signs line tells you at a glance whether the agent is alive: last activity, how long the current command has been running, when the task and the current run began, an API-error pause with its retry time, and a yellow or red hint after 10 or 30 minutes without any sign of life. Jump in or hit Stop any time. On the home view, admins also see an **Agents at work** strip: how many worker slots are busy, how many orders wait, and what each running agent is doing — including how long it will probably take ("done in ~25 min", with phase and test progress) and when each waiting order will start. Admins can switch the pool down to N active slots (0 to max) with one click, and an optional second test machine shows its load.
- **📦 Isolated by design.** Every agent run and every app it builds runs in its **own container**. The agent can’t touch your machine, only its workspace.
- **🚀 Live apps, instantly.** Each project deploys to a local port and gets a “open live” link. Watch it come together.
- **🗂️ See everything.** Live activity ticker, full file browser, context meter with one-click compaction, stop button, unread markers — all the controls, none of the terminal.
- **🚀 Publish in a click.** Download a project as a zip, or push it straight to a GitHub repo you own — **leak-scanned first** so secrets and private data never ship.
- **📊 Know what it costs.** A usage view tracks output tokens and cost per provider, today and all-time. The zero-key demo is free.
- **👥 Bring your household or team.** PIN logins, an admin user manager, and per-user unread badges — everyone shares the same workshop.

## 🚀 Quickstart

**The one prerequisite:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS) or Docker Engine (Linux). Install it once — everything else is automatic.

### 🖱️ One-click (no command line)

1. **Download** the latest release zip from the [Releases page](https://github.com/robeertm/AIWerkstatt/releases/latest) → **Source code (zip)**.
2. **Unzip** it anywhere.
3. **Double-click the launcher for your system** — it builds everything and opens the app for you:
   - **macOS** → `AIWerkstatt.command`  *(first time only: right-click → Open, to get past Gatekeeper)*
   - **Windows** → `AIWerkstatt.bat`
   - **Linux** → `AIWerkstatt.sh`  *(or `./AIWerkstatt.sh` in a terminal)*

The first run builds the images (a few minutes); after that it starts in seconds and your browser opens at **http://localhost:8095**. Create your admin login, connect a provider (below), and send your first request. 🎈 *(Stop it later with `docker compose down` in that folder, or just quit Docker.)*

### ⌨️ Or from the command line

```bash
git clone https://github.com/robeertm/AIWerkstatt.git
cd AIWerkstatt
docker compose up -d --build      # build + start, then open http://localhost:8095
```

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

### 📚 Knowledge folder and memory folder

Every project has its own persistent `knowledge.md`. For **new** runs the web container inserts the relevant
part of it into the first message (a request to "please read" is not reliable). Two optional settings add more:

| Variable | Default | Meaning |
|---|---|---|
| `AIWERKSTATT_KNOWLEDGE_DIR` | empty (off) | Folder of markdown master documents (path as seen inside the web container). A file is used when its normalised name starts with the project slug, otherwise when its name matches keywords of the order title (at most 2). |
| `AIWERKSTATT_MEMORY_DIR` | empty (off) | Folder with `MEMORY.md` and `index_<topic>.md` files. `MEMORY.md` is never inserted; the `index_*.md` that best matches the project slug contributes its newest `- [` lines. |
| `AIW_API_RETRY_DELAYS` | `120,300,900` | Seconds to wait before each retry after an API error; empty turns the retry off. |
| `AIW_READING_PROOF` | empty (off) | `1` turns on the reading proof (see below). |
| `AIW_READING_MAX_NUDGES` | `1` | How many follow-ups a missing proof line may trigger before the reply goes out anyway. |

**Reading proof (opt-in, `AIW_READING_PROOF=1`).** A new run's first message then starts with a reading list: the
matching master document(s), the matching memory topic index and up to four memory notes that match words of the order
title (title words that appear in most notes do not count). The agent has to end its result with a line
`READ: <file1>, <file2>, ...`; `READ: none - <reason>` is accepted when no place was listed. Without a valid line (it
must name at least one listed place) the task stays open and one follow-up asks for it in the same session; after that the
reply goes out in any case, so nothing is aborted or lost. Pure release-train fixes and test orders only have to read the
project's master document. The run result carries `reading` (`required`, `entries`, `valid`, `nudges`, `missing`). Off
by default: nothing changes unless you set it. The listed files can only be opened by the agent if those folders are
reachable from the runner; the stems are checked by name either way.

Inserted from each document: the head, the three newest dated `##` sections, one pitfalls/gotchas section — each
with path and line number. The pack is capped at **12000 bytes** (UTF-8) and ends with a pointer to the rest when
it is cut. Sections that look like they contain a secret are left out with a note. When nothing matches, the agent
is told so and asked to name the note it used. Subagents get a short rules block that names the project's knowledge
folder (`AIW_KNOWLEDGE_DIR` = `/vault/<project>`; the memory folder stays in the web container and reaches the agent
only through the context pack), and each run ends with an evidence line: knowledge read, knowledge updated, number of
subagents.

More detail: [docs/architecture.md](docs/architecture.md) · [docs/providers.md](docs/providers.md) · [docs/security.md](docs/security.md)

## 🔐 Security

AIWerkstatt is built to run **on your own machine for you** — not as a multi-tenant SaaS.
The agent is contained, the Docker socket is proxied down to a minimal surface, and a
deterministic leak scanner guards anything you choose to publish. Read
[docs/security.md](docs/security.md) before exposing it beyond localhost.

**Service tokens (optional).** Another local service can use a bearer token for a narrow
set of endpoints. Each token has a **profile**:

- **restricted** (default): list and view projects, view and create tasks, create projects.
  Tasks it creates are **restricted**: the workspace is snapshotted first, nothing of that
  project is deployed, published or released while such a task is open, and a signed-in
  admin approves (deploy) or rejects (restore the snapshot) it in the task view.
- **coordinator**: for a trusted orchestrator. List and view projects, create tasks, send
  follow-ups and read task status and history in every project; its tasks run and deploy
  like those of a signed-in user. Writes are rate limited (30 per hour by default).
- **peer**: for parallel agents that hand each other a note. One route only: a note from
  one open task to a different open task (`POST /api/threads/<id>/peer-note`). It cannot
  create, stop, read or reprioritise anything. Own rate limit (`AIWERKSTATT_PEER_RATE`,
  30 per hour by default).
- **reporter**: for a service that files tasks on behalf of explicitly trusted reporters
  (for example an issue triage). It can only create a task in one fixed intake project
  (`AIWERKSTATT_REPORTER_PROJECT`; unset means every create is refused), and read and
  follow up on the tasks that same token created. Its tasks go through the same review
  gate as restricted ones. Own rate limit (`AIWERKSTATT_REPORTER_RATE`, 8 per hour by
  default). No file uploads, no project access, nothing else.
- **chain**: for the task-chain tool in [`scripts/chain`](docs/chain.md). It can only create
  tasks in one collecting project (`AIWERKSTATT_CHAIN_PROJECT`) whose title starts with a
  fixed prefix (`AIWERKSTATT_CHAIN_TITLE_PREFIX`), with `auto` or a standard model and no
  attachments, and read and follow up on its own tasks. Either variable unset means every
  create is refused. Review gate on by default, own rate limit (`AIWERKSTATT_CHAIN_RATE`,
  6 per hour by default), nothing else. Multi-project plans (`scripts/chain/projectplan.py`) add the
  projects in `AIWERKSTATT_CHAIN_PLAN_PROJECTS` with their own title prefix
  (`AIWERKSTATT_CHAIN_PLAN_PREFIX`); `AIWERKSTATT_CHAIN_PLAN_EXCLUDED` projects stay unreachable.

- **app**: for a self-hosted app instance, bound to one project and one instance. It can only submit
  a wish (`POST /api/app-feedback/<project>/wishes`) and read the state of its own instance's wishes,
  from allowed networks only, with an hourly limit. See [docs/app-feedback.md](docs/app-feedback.md).

No token can release, publish, create or delete projects, change settings, manage users or
tokens, or decide a review. Every task shows which token created it (**via &lt;name&gt;**),
every token request is written to an audit log, and a kill-switch file disables a token at
once. Details: [docs/security.md](docs/security.md#service-tokens).

### 🌐 Network connections

AIWerkstatt itself makes only these outbound connections:

- **Your AI provider.** Agents run in their own containers, with internet access (the provider and git/npm
  need it). The provider's command-line tool inside that container talks to the provider you chose for the
  project — Anthropic, OpenAI or Google. The AIWerkstatt backend itself does not call a model API.
- **Optional plan-usage readout.** Only if a user connects their Claude account for the usage display, the
  backend asks Anthropic's usage endpoint for those numbers.
- **GitHub, update check.** When an admin has the page open, the backend asks the GitHub releases API for the
  latest release (cached for an hour). Switch the automatic check off with `AIWERKSTATT_UPDATE_CHECK=off` in
  `.env` (the compose file passes it through); the "Check for updates" button still asks when an admin clicks
  it. The one-click update pulls from the repository's remote and rebuilds the images, so it downloads
  packages and base images while it runs.
- **GitHub, publish.** Only when you press Publish for a project: the backend talks to the GitHub API and
  pushes with the token you stored.

Nothing else is sent: no telemetry, no analytics, no advertising, and the web interface loads no script,
stylesheet or font from a third-party host. The agents' own tools are the providers' software and follow
their terms.

### 🖥️ Optional second test machine

Heavy test suites can run on a second machine through a forced-command SSH key, throttled and capped
at 2 runs at once, with the machine's load shown in the admin overview. Standard library only, off unless
configured. See [docs/test-runner.md](docs/test-runner.md).

### ✅ Release check

After each release, a standard-library script checks that the **published** release contains every deliverable the project
advertises: declared files and signatures, checksums, updater manifest, container image tags, version files, release notes and
a download page. Gaps are waited out, healed through declared ways, and only then reported through an operator hook. See
[docs/release-check.md](docs/release-check.md).

## 💬 Feedback

Bug reports and feature ideas are very welcome — please [open an issue](https://github.com/robeertm/AIWerkstatt/issues).
Missing an AI provider? That's one of the most useful things to ask for. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how the pieces fit and how to run your own local build.

## 📄 License

**Source-available — free to run, not for redistribution.** © 2026 Robert Manuwald.
You may download and run AIWerkstatt for your own personal or internal use. Modifying,
redistributing, or hosting it for others is not permitted without written permission.
The apps *you* build with it are yours. See [LICENSE](LICENSE). Third-party components and their
licences are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) (generated by
`scripts/drittlizenzen.py`, checked by a test, shipped inside the web image).

All product and company names mentioned in this project are trademarks or registered trademarks of
their respective owners. Their use here is for identification only and does not imply any
affiliation, endorsement or sponsorship.
