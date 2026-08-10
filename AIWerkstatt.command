#!/usr/bin/env bash
# ── AIWerkstatt — one-click start for macOS ──────────────────────────────────
# Double-click this file in Finder. First run builds the images (a few minutes);
# every run after that is quick. Needs Docker Desktop installed once.
#   Stop later with:  docker compose down   (in this folder)
set -u
cd "$(dirname "$0")" || exit 1
echo "── AIWerkstatt ──"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker isn't installed yet."
  echo "Get Docker Desktop (free):  https://www.docker.com/products/docker-desktop"
  echo "Install it, then double-click AIWerkstatt.command again."
  read -r -p "Press Return to close… " _; exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop… (first launch can take a minute)"
  open -a Docker >/dev/null 2>&1 || true
  for _ in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 2; done
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker didn't come up. Open Docker Desktop manually, then run this again."
  read -r -p "Press Return to close… " _; exit 1
fi

echo "Building & starting AIWerkstatt (first run takes a few minutes)…"
if ! docker compose up -d --build; then
  echo "Something went wrong starting the containers. Scroll up for the error."
  read -r -p "Press Return to close… " _; exit 1
fi

echo "Waiting for the app to answer…"
for _ in $(seq 1 90); do curl -fsS http://localhost:8095 >/dev/null 2>&1 && break; sleep 2; done
open "http://localhost:8095" >/dev/null 2>&1 || true
echo ""
echo "✅ AIWerkstatt is running →  http://localhost:8095"
echo "   Stop it later with:  docker compose down   (in this folder)"
read -r -p "Press Return to close this window… " _
