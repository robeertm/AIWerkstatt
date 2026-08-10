#!/usr/bin/env bash
# ── AIWerkstatt — one-click start for Linux ──────────────────────────────────
# Run:  ./AIWerkstatt.sh   (or double-click and choose "Run in Terminal").
# First run builds the images (a few minutes); every run after that is quick.
#   Stop later with:  docker compose down   (in this folder)
set -u
cd "$(dirname "$0")" || exit 1
echo "── AIWerkstatt ──"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker isn't installed. Install Docker Engine + the Compose plugin:"
  echo "  https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Can't talk to the Docker daemon. Start it and make sure your user may use Docker:"
  echo "  sudo systemctl start docker"
  echo "  sudo usermod -aG docker \"\$USER\"   # then log out and back in"
  exit 1
fi

echo "Building & starting AIWerkstatt (first run takes a few minutes)…"
if ! docker compose up -d --build; then
  echo "Something went wrong starting the containers. Scroll up for the error."
  exit 1
fi

echo "Waiting for the app to answer…"
for _ in $(seq 1 90); do curl -fsS http://localhost:8095 >/dev/null 2>&1 && break; sleep 2; done
xdg-open "http://localhost:8095" >/dev/null 2>&1 || echo "Open http://localhost:8095 in your browser."
echo ""
echo "✅ AIWerkstatt is running →  http://localhost:8095"
echo "   Stop it later with:  docker compose down"
