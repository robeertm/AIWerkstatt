#!/usr/bin/env sh
# Web control-plane entrypoint. Runs briefly as root to make the shared volumes
# writable by the app user (uid 10001) — the SAME uid the agent-runner uses, so
# events/inbox/workspaces are read/write/deletable from both sides — then drops
# privileges and never runs the app as root.
set -e
for d in /data /events /inbox /workspaces /vault; do
  mkdir -p "$d" 2>/dev/null || true
  chown -R 10001:10001 "$d" 2>/dev/null || true
done
exec gosu 10001:10001 "$@"
