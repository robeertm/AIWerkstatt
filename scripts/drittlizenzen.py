#!/usr/bin/env python3
"""Writes THIRD-PARTY-NOTICES.md: every third-party component with its licence.

Sources:
  1. backend/requirements.txt + everything it pulls in (resolved in a throwaway
     virtual environment; licence and licence texts come from the installed
     package metadata),
  2. frontend/package-lock.json (production packages only - what the built
     frontend bundle contains; licence texts via `npm ci` in a work directory),
  3. a curated list of what the Docker images and the agent-runner add (base
     images, system packages, the three provider command-line tools).

Usage (never install into a shared environment):

    python3 -m venv /tmp/aiw-notices
    /tmp/aiw-notices/bin/pip install -r backend/requirements.txt
    /tmp/aiw-notices/bin/python -I scripts/drittlizenzen.py --work /tmp/aiw-notices-work > THIRD-PARTY-NOTICES.md

backend/tests/test_third_party_notices.py fails when a package of
backend/requirements.txt or a production package of frontend/package-lock.json is
missing in THIRD-PARTY-NOTICES.md, or when the Dockerfile stops shipping it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|UNLICENSE|COPYRIGHT)", re.I)

NORMALISIERT = {
    "bsd license": "BSD-3-Clause",
    "mit license": "MIT",
    "apache software license": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
}
# Packages whose metadata carries no usable licence expression.
LIZENZ_UEBERSCHREIBEN = {
    "flask": "BSD-3-Clause",
    "jinja2": "BSD-3-Clause",
    "itsdangerous": "BSD-3-Clause",
    "blinker": "MIT",
    "pip": "MIT",
}

# What the images add. (name, licence, note)
IMAGES = [
    ("node:20-alpine (build stage of the web image)", "Node.js: MIT; Alpine Linux: the licences of its packages", "used only to build the frontend; not part of the running image"),
    ("python:3.12-slim (web image)", "CPython: Python Software Foundation License 2.0; Debian GNU/Linux: the licences of its packages", ""),
    ("gosu (web image, apt)", "GPL-3.0-or-later", "drops root to the app user at start; separate program"),
    ("git (web image and updater, apt/apk)", "GPL-2.0-only", "separate program"),
    ("node:20-bookworm-slim (agent-runner image)", "Node.js: MIT; Debian GNU/Linux: the licences of its packages", ""),
    ("python3, ca-certificates, curl (agent-runner image, apt)", "Python Software Foundation License 2.0; MPL-2.0 / GPL-2.0-or-later (ca-certificates); curl license", ""),
    ("docker:cli with docker-cli-compose (updater image)", "Apache-2.0", "Docker CLI and Compose"),
    ("ghcr.io/tecnativa/docker-socket-proxy (docker-compose.yml)", "Apache-2.0 (proxy configuration); HAProxy: GPL-2.0-or-later and LGPL-2.1 components", "pulled by Docker Compose; not part of this repository"),
]
# The provider CLIs installed into the agent-runner image at build time (agent-runner/Dockerfile).
PROVIDER_CLIS = [
    ("@anthropic-ai/claude-code", "2.1.310", "Proprietary - (c) Anthropic PBC. All rights reserved. Use is subject to Anthropic's legal agreements (https://www.anthropic.com/legal)."),
    ("@openai/codex", "latest at build time", "Apache-2.0 - Copyright OpenAI"),
    ("@google/gemini-cli", "latest at build time", "Apache-2.0 - Copyright Google LLC"),
]


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace").strip()


def req_names(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            out.append(m.group(1))
    return out


def python_closure(names: list[str]) -> list[md.Distribution]:
    from packaging.markers import Marker
    from packaging.requirements import Requirement
    seen: dict[str, md.Distribution] = {}
    todo = [re.sub(r"[-_.]+", "-", n).lower() for n in names]
    while todo:
        n = todo.pop()
        if n in seen:
            continue
        try:
            dist = md.distribution(n)
        except md.PackageNotFoundError:
            sys.exit(f"{n} is not installed in this environment - see the usage note at the top")
        seen[n] = dist
        for r in dist.requires or []:
            req = Requirement(r)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            todo.append(re.sub(r"[-_.]+", "-", req.name).lower())
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def spdx(dist: md.Distribution) -> str:
    name = dist.metadata["Name"].lower()
    if name in LIZENZ_UEBERSCHREIBEN:
        return LIZENZ_UEBERSCHREIBEN[name]
    raw = (dist.metadata.get("License-Expression") or "").strip() or " ".join((dist.metadata.get("License") or "").split())
    if not raw:
        sys.exit(f"no licence in the metadata of {name} - add it to LIZENZ_UEBERSCHREIBEN")
    return NORMALISIERT.get(raw.lower(), raw)


def dist_texts(dist: md.Distribution) -> list[str]:
    out = []
    for f in dist.files or []:
        if LICENCE_FILE.match(f.name) or f.name.upper().startswith("AUTHORS"):
            try:
                t = f.read_binary().decode("utf-8", errors="replace").strip()
            except OSError:
                continue
            if t:
                out.append(t)
    return out


def npm_entries(work: Path) -> list[dict]:
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    dest = work / "frontend"
    if not (dest / "node_modules").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "frontend" / "package.json", dest / "package.json")
        shutil.copy(ROOT / "frontend" / "package-lock.json", dest / "package-lock.json")
        subprocess.run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
                       cwd=dest, check=True, stdout=subprocess.DEVNULL)
    out = []
    for key, meta in sorted(lock["packages"].items()):
        if not key or meta.get("dev"):
            continue
        folder = dest / key
        texts = [_read(f) for f in sorted(folder.iterdir()) if f.is_file() and LICENCE_FILE.match(f.name)] if folder.is_dir() else []
        out.append({"name": key.split("node_modules/")[-1], "version": meta["version"],
                    "license": meta.get("license") or "see licence file", "texts": texts})
    return out


HEAD = """# Third-party notices

AIWerkstatt is built on the work of others. This file lists the third-party
components that are part of AIWerkstatt - the Python packages of the backend, the
JavaScript packages in the built frontend, and what the Docker images add - with
their licences. AIWerkstatt's own licence is in [LICENSE](LICENSE); it does not limit
the rights these licences grant for their components.

The file is generated by `scripts/drittlizenzen.py` and checked by
`backend/tests/test_third_party_notices.py`.

## Backend (Python packages)

`backend/requirements.txt` plus everything it pulls in. The four direct
dependencies are pinned; the versions of the others are those resolved when this
file was generated. Packages under the MPL-2.0 (`certifi`) are used unmodified;
their source code is available from the project page and from PyPI.

"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default="/tmp/aiwerkstatt-notices", help="throwaway work directory")
    work = Path(ap.parse_args().work)
    work.mkdir(parents=True, exist_ok=True)

    dists = python_closure(req_names(ROOT / "backend" / "requirements.txt"))
    # pip/setuptools are tooling of the venv, not of the backend.
    dists = [d for d in dists if d.metadata["Name"].lower() not in ("setuptools", "wheel")]
    texts: dict[str, tuple[str, list[str]]] = {}

    def add(text: str, who: str) -> None:
        texts.setdefault(hashlib.sha256(text.encode()).hexdigest(), (text, []))[1].append(who)

    out = [HEAD]
    for d in dists:
        name, version = d.metadata["Name"], d.version
        out.append(f"- `{name}` {version} - {spdx(d)}\n")
        for t in dist_texts(d):
            add(t, f"{name} {version}")

    npm = npm_entries(work)
    out.append("\n## Frontend (JavaScript packages in the built bundle)\n\n"
               "Production packages of `frontend/package-lock.json`. Build tools (Vite, TypeScript and the "
               "like) are not part of the delivered files; the production build embeds a small "
               "module-preload helper from Vite (MIT).\n\n")
    for e in npm:
        out.append(f"- `{e['name']}` {e['version']} - {e['license']}\n")
        for t in e["texts"]:
            add(t, f"{e['name']} {e['version']}")

    out.append("\n## Docker images\n\nThe images add the following software on top of the AIWerkstatt code. It is "
               "installed from the publishers' archives when the images are built or pulled and keeps its own "
               "licence; the texts are inside the images (for Debian packages under "
               "`/usr/share/doc/<package>/copyright`). They are separate programs, not linked into AIWerkstatt.\n\n")
    for name, lic, note in IMAGES:
        out.append(f"- {name} - {lic}" + (f" ({note})" if note else "") + "\n")

    out.append("\n## Provider command-line tools in the agent-runner image\n\nInstalled with `npm install -g` while "
               "the agent-runner image is built on your machine (`agent-runner/Dockerfile`). AIWerkstatt does not "
               "contain or redistribute them; they are the providers' own tools and keep their own licences and "
               "terms.\n\n")
    for name, version, lic in PROVIDER_CLIS:
        out.append(f"- `{name}` {version} - {lic}\n")

    out.append("\n## Licence texts\n")
    for text, who in sorted(texts.values(), key=lambda t: (sorted(t[1])[0], t[0])):
        out.append("\n---\n\nUsed by: " + ", ".join(sorted(set(who))) + "\n\n```text\n" + text + "\n```\n")
    sys.stdout.write("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
