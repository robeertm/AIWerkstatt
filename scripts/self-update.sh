#!/bin/sh
# In-app update: pull the latest source, rebuild the images, and recreate the web
# container. Run INSIDE the `updater` sidecar (not web) so recreating web can't
# kill the process doing the work. Triggered by POST /api/update/install.
#
# Talks to Docker via the hardened proxy (DOCKER_HOST=tcp://dockerproxy:2375).
# The classic builder is forced (DOCKER_BUILDKIT=0) because BuildKit's session
# endpoints aren't exposed through the proxy.
set -e
cd /project

echo "[updater] $(date -u +%FT%TZ) starting self-update"

# The mounted project dir is owned by the host user; allow git to operate on it.
git config --global --add safe.directory /project 2>/dev/null || true

if [ -d .git ]; then
  echo "[updater] git pull --ff-only"
  git pull --ff-only
else
  echo "[updater] no git checkout — cannot pull; aborting" >&2
  exit 1
fi

export DOCKER_BUILDKIT=0
echo "[updater] building images (web + agent-runner)"
docker compose --profile build-only build web agent-runner

echo "[updater] recreating web"
docker compose up -d web

echo "[updater] $(date -u +%FT%TZ) done"
