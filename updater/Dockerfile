# Minimal in-app updater. It idles until the web app asks it to run the
# self-update script, then rebuilds and recreates the web container. It talks to
# Docker ONLY through the same hardened socket proxy the web app uses (DOCKER_HOST
# points at dockerproxy) — never the raw socket. See docs/security.md.
FROM docker:cli
RUN apk add --no-cache git docker-cli-compose
WORKDIR /project
# Idle; the web control plane execs scripts/self-update.sh here on demand.
ENTRYPOINT ["sleep", "infinity"]
