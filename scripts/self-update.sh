#!/bin/sh
# In-app update: check first, keep the old version, swap, verify, and roll back if the
# new version does not come up. Run INSIDE the `updater` sidecar (not web) so recreating
# web can't kill the process doing the work. Triggered by POST /api/update/install.
#
# Steps
#   1. Pre-check (nothing is touched): clean checkout, remote reachable, the remote is
#      strictly ahead (fast-forward) and its version is newer than the running one.
#   2. Backup: the current commit is recorded as a git ref (refs/aiwerkstatt/pre-update)
#      and in data-free marker file .update-previous; the images keep their old tag.
#   3. Swap: fast-forward pull, build web + agent-runner, recreate web only.
#   4. Verify: the health URL must answer with the new version within the timeout.
#   5. Failure in 3 or 4 -> checkout of the recorded commit, rebuild, recreate web.
# Data and configuration (volumes, .env, data/) are never touched by any step.
#
# Talks to Docker via the hardened proxy (DOCKER_HOST=tcp://dockerproxy:2375).
# The classic builder is forced (DOCKER_BUILDKIT=0) because BuildKit's session
# endpoints aren't exposed through the proxy.
#
# Test hooks (environment): DOCKER_BIN, HEALTH_CMD, HEALTH_TRIES, HEALTH_WAIT,
# UPDATE_PROJECT (default /project), UPDATE_REMOTE (default origin).
set -u
PROJECT="${UPDATE_PROJECT:-/project}"
REMOTE="${UPDATE_REMOTE:-origin}"
DOCKER="${DOCKER_BIN:-docker}"
HEALTH_TRIES="${HEALTH_TRIES:-40}"
HEALTH_WAIT="${HEALTH_WAIT:-3}"
HEALTH_CMD="${HEALTH_CMD:-wget -q -O - http://web:8095/api/health}"
cd "$PROJECT" || exit 1

say() { echo "[updater] $(date -u +%FT%TZ) $*"; }
die() { say "$*" >&2; exit "${2:-1}"; }

version_of() {  # version_of <git-rev>  -> x.y.z from pyproject.toml at that revision
  git show "$1:pyproject.toml" 2>/dev/null | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -n 1
}

# true when $1 is strictly newer than $2 (numeric dotted compare)
newer() {
  [ "$1" = "$2" ] && return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)" = "$1" ]
}

deploy() {  # deploy: build both images, recreate web only
  export DOCKER_BUILDKIT=0
  say "building images (web + agent-runner)"
  "$DOCKER" compose --profile build-only build web agent-runner || return 1
  say "recreating web"
  # --no-deps: recreate ONLY web. Never touch dockerproxy — it's the updater's own
  # lifeline to Docker; recreating it would sever this script mid-flight.
  "$DOCKER" compose up -d --no-deps web || return 1
}

healthy() {  # healthy <expected-version>
  n=0
  while [ "$n" -lt "$HEALTH_TRIES" ]; do
    out=$(sh -c "$HEALTH_CMD" 2>/dev/null) && case "$out" in *"$1"*) return 0 ;; esac
    n=$((n + 1)); sleep "$HEALTH_WAIT"
  done
  return 1
}

say "starting self-update"
# The mounted project dir is owned by the host user; allow git to operate on it.
git config --global --add safe.directory "$PROJECT" 2>/dev/null || true

# ---- 1. pre-check ----
[ -d .git ] || die "no git checkout — cannot pull; aborting" 1
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  die "pre-check failed: local changes in tracked files; nothing changed" 2
fi
BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != "HEAD" ] || die "pre-check failed: detached HEAD; nothing changed" 2
git fetch "$REMOTE" "$BRANCH" || die "pre-check failed: remote not reachable; nothing changed" 3
TARGET=$(git rev-parse FETCH_HEAD)
PREV=$(git rev-parse HEAD)
[ "$TARGET" != "$PREV" ] || die "already up to date; nothing changed" 0
git merge-base --is-ancestor "$PREV" "$TARGET" || die "pre-check failed: remote is not a fast-forward of this checkout; nothing changed" 4
OLDV=$(version_of "$PREV"); NEWV=$(version_of "$TARGET")
[ -n "$NEWV" ] || die "pre-check failed: new version has no readable version; nothing changed" 5
if [ -n "$OLDV" ] && ! newer "$NEWV" "$OLDV"; then
  die "pre-check failed: remote version $NEWV is not newer than $OLDV; nothing changed" 5
fi
say "update $OLDV -> $NEWV ($PREV -> $TARGET)"

# ---- 2. backup of the old version ----
git update-ref refs/aiwerkstatt/pre-update "$PREV"
echo "$PREV" > .git/aiwerkstatt-update-previous

rollback() {
  say "ROLLBACK to $OLDV ($PREV): $1" >&2
  git reset --hard "$PREV" >/dev/null 2>&1 || git checkout -f "$PREV" >/dev/null 2>&1
  if deploy; then say "rolled back, old version $OLDV restored" >&2
  else say "rollback rebuild failed — old source is restored, run: docker compose up -d --build" >&2; fi
  exit 10
}

# ---- 3. swap ----
say "git merge --ff-only $TARGET"
git merge --ff-only "$TARGET" || rollback "fast-forward failed"
deploy || rollback "build or recreate failed"

# ---- 4. verify ----
healthy "$NEWV" || rollback "new version did not answer on the health URL"
say "done, now running $NEWV"
