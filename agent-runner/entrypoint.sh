#!/usr/bin/env sh
# Agent-runner entrypoint. The orchestrator starts this container with:
#   WK_SLUG, WK_THREAD_ID, PROVIDER, MODEL, EFFORT (optional)
#   /workspace  (named volume, the project working tree)
#   /events     (named volume, typed event JSONs read by the web ingester)
#   /inbox      (named volume, follow-ups / compact / stop from the UI)
#   /run/secrets/provider.env  (0600, provider credentials — sourced, never in argv)
# It sources the secret file into the environment and hands off to the
# provider-neutral session driver. Credentials never appear on the command line.
set -eu

SECRET_FILE="${WK_SECRET_FILE:-/run/secrets/provider.env}"
if [ -f "$SECRET_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SECRET_FILE"
  set +a
fi

cd /workspace 2>/dev/null || true
exec python3 /runner/session.py
