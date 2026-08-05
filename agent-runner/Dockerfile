# Agent-runner image — one ephemeral container per agent run. Ships every
# supported provider CLI plus the provider-neutral session driver. Launched by
# the orchestrator (never long-lived), runs UNPRIVILEGED with no Docker access.
FROM node:20-bookworm-slim

# Python (session driver) + git + a small toolbox so agents can build projects.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Provider CLIs. Package names are verified at build time; pin versions before
# a release. Each is optional at runtime — only the selected provider is used.
RUN npm install -g \
      @anthropic-ai/claude-code \
      @openai/codex \
      @google/gemini-cli \
    || echo "note: pin/adjust provider CLI packages before release"

WORKDIR /runner
COPY . /runner
RUN chmod +x /runner/entrypoint.sh

# Non-root: the run is contained by the container, not by host users.
RUN useradd -m -u 10001 runner \
    && mkdir -p /workspace /events /inbox \
    && chown -R runner:runner /runner /workspace /events /inbox
USER runner

ENTRYPOINT ["/runner/entrypoint.sh"]
