# Connecting AI providers

AIWerkstatt drives a coding-agent **CLI** behind each provider. On first run it
asks you to connect at least one. Your key is stored **encrypted in the app's
private volume** — never in the repo, never in a container image, and only ever
sent to the provider you chose. You pick **which provider and model per project**.

## Bundled providers

### Anthropic Claude

- **CLI:** `claude` (Claude Code).
- **Auth:** an Anthropic **API key** (`ANTHROPIC_API_KEY`) **or** a Claude
  **subscription token** via `claude setup-token` (OAuth).
- **Get a key:** [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
- **Models:** Opus (most capable), Sonnet (balanced), Haiku (fast & cheap), with
  selectable reasoning effort.

Claude is the **reference implementation** — the adapter the others are modelled
on. If you're writing a new provider, read it first.

### OpenAI (Codex)

- **CLI:** `codex`.
- **Auth:** an OpenAI **API key** (`OPENAI_API_KEY`) **or** a **ChatGPT sign-in**
  (OAuth).
- **Get a key:** [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- **Models:** GPT-5.1 Codex (coding), GPT-5.1 (general), o4-mini (fast reasoning),
  with selectable reasoning effort.

### Google Gemini

- **CLI:** `gemini`.
- **Auth:** a Google AI Studio **API key** (`GEMINI_API_KEY`) **or** a **Google
  sign-in** (OAuth).
- **Get a key:** [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- **Models:** Gemini 2.5 Pro (most capable), Gemini 2.5 Flash (fast & cheap).

> The Claude adapter is validated end to end. The **Codex** and **Gemini** adapters
> are **validated on first connect** — when you connect one, AIWerkstatt exercises
> the CLI so you find out immediately if the tool or auth needs attention.

## How model / effort selection works

Each project stores its own **provider + model** choice (and, where the provider
supports it, a **reasoning effort** step such as low / medium / high). When a task
runs, the orchestrator passes that choice through to the run adapter, which turns it
into the right CLI flags. This is the main cost/quality lever: output tokens
dominate spend, so a heavier model or higher effort costs more per turn. Set it per
project to match the work — a quick script and a large refactor don't need the same
setting.

Not every provider exposes an effort control; when it doesn't, only the model
choice applies.

## Adding your own provider

Bringing another coding-agent CLI is a small, well-defined change — a `ProviderSpec`
on the control-plane side and an `AgentAdapter` on the runner side. Step-by-step
instructions are in [../CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-new-ai-provider).
**PRs for new provider adapters are especially welcome.**
