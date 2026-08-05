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
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  echo "[updater] git pull --ff-only origin $BRANCH"
  # Explicit remote+branch so it works whether or not upstream tracking is set.
  git pull --ff-only origin "$BRANCH"
else
  echo "[updater] no git checkout — cannot pull; aborting" >&2
  exit 1
fi

export DOCKER_BUILDKIT=0
echo "[updater] building images (web + agent-runner)"
docker compose --profile build-only build web agent-runner

echo "[updater] recreating web"
# --no-deps: recreate ONLY web. Never touch dockerproxy — it's the updater's own
# lifeline to Docker; recreating it would sever this script mid-flight.
docker compose up -d --no-deps web

echo "[updater] $(date -u +%FT%TZ) done"
