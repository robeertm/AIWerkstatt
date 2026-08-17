"""Runtime configuration — everything comes from the environment, nothing is
hard-coded. There is NO built-in project registry: projects are created at
runtime through the UI and stored in the app's private data volume (empty on
purpose — this is a personal, self-hosted install).
"""
import os

# Version of this build — read from pyproject.toml, the SINGLE source of truth
# (the release pipeline tags from the same file). Parsing it at runtime keeps the
# self-update check honest: a bumped pyproject can never drift from what the app
# reports, so the "update available" banner clears once a build actually ships.
# Falls back to the literal below only if pyproject isn't bundled (bump both on release).
def _read_version(fallback="0.10.1"):
    here = os.path.dirname(__file__)
    for p in (os.path.join(here, "pyproject.toml"),        # bundled next to backend (in the image)
              os.path.join(here, "..", "pyproject.toml")):  # repo root (running from source)
        try:
            import tomllib  # Python 3.11+
            with open(p, "rb") as f:
                v = tomllib.load(f).get("project", {}).get("version")
            if v:
                return v
        except (OSError, KeyError, ValueError, ImportError):
            continue
    return fallback

VERSION = _read_version()
# GitHub repository this build is distributed from (owner/name).
REPO_SLUG = os.environ.get("AIWERKSTATT_REPO", "robeertm/AIWerkstatt")

# Where SQLite, provider credentials and queues live (a Docker named volume).
DATA = os.environ.get("AIWERKSTATT_DATA", os.path.join(os.path.dirname(__file__), "..", "data"))

# Port the UI is served on (inside the container; compose maps it to the host).
PORT = int(os.environ.get("AIWERKSTATT_PORT", "8095"))

# Host port range handed out to the web apps that agents build & deploy live.
def _port_range():
    rng = os.environ.get("AIWERKSTATT_PORT_RANGE", "8100-8199")
    try:
        lo, hi = rng.split("-", 1)
        return int(lo), int(hi)
    except ValueError:
        return 8100, 8199

PORT_RANGE = _port_range()

# Host name the browser uses to open a deployed app (localhost for a personal
# install; a LAN/VPN name to reach it from other devices).
PUBLIC_HOST = os.environ.get("AIWERKSTATT_PUBLIC_HOST", "localhost")

# Docker Engine endpoint — ALWAYS the hardened socket proxy, never a raw socket.
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://dockerproxy:2375")

# Image the orchestrator starts for each agent run (built from ./agent-runner).
AGENT_RUNNER_IMAGE = os.environ.get("AGENT_RUNNER_IMAGE", "aiwerkstatt-agent-runner:latest")

# Auto-retry an aborted run this many times (0 = unlimited until success).
MAX_RETRIES = int(os.environ.get("AIWERKSTATT_MAX_RETRIES", "5"))

# First-run admin bootstrap (real PIN is set in the browser on first start).
ADMIN = os.environ.get("AIWERKSTATT_ADMIN", "admin")
ADMIN_PIN = os.environ.get("AIWERKSTATT_ADMIN_PIN", "1234")

# Empty by design — see module docstring.
PROJECTS: list[dict] = []
PROJECTS_BY_ID: dict[str, dict] = {}
