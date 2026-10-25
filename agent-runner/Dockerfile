# Agent-runner image — one ephemeral container per agent run. Ships every
# supported provider CLI plus the provider-neutral session driver. Launched by
# the orchestrator (never long-lived), runs UNPRIVILEGED with no Docker access.
FROM node:20-bookworm-slim

# Python (session driver) + git + a small toolbox so agents can build projects.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Provider CLIs. Each is optional at runtime — only the selected provider is used.
# The Claude CLI is pinned: the 5.5 model family needs claude-code >= 2.1.280.
# A failed install or a wrong version fails the build (no `|| echo` fallback).
RUN set -eu; \
    npm install -g \
      @anthropic-ai/claude-code@2.1.310 \
      @openai/codex \
      @google/gemini-cli; \
    got="$(claude --version | cut -d' ' -f1)"; \
    [ "$got" = "2.1.310" ] || { echo "claude CLI version mismatch: got '$got', expected 2.1.310" >&2; exit 1; }

WORKDIR /runner
COPY . /runner
RUN chmod +x /runner/entrypoint.sh

# Non-root: the run is contained by the container, not by host users.
RUN useradd -m -u 10001 runner \
    && mkdir -p /workspace /events /inbox \
    && chown -R runner:runner /runner /workspace /events /inbox
USER runner

ENTRYPOINT ["/runner/entrypoint.sh"]
