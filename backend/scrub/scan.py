"""Deterministic leak scanner — run before anything goes to a public repo.

Rules are DATA, not code: loaded from ``rules.default.toml`` (generic secret
patterns only, safe to ship publicly). A private overlay can be layered on via
``AIWERKSTATT_SCRUB_RULES=/path/to/rules.local.toml`` — that is how a maintainer
keeps their own deny-list (personal names, home IPs, internal hostnames) OUT of
the public repo while still enforcing it at release time.

Severity ``block`` fails the scan (``ok == blocking == 0``); ``review`` is
counted and shown but never blocks (false-positive-prone). Matches are masked.

Standalone (CI): ``python backend/scrub/scan.py <dir>`` → JSON on stdout,
exit code 1 if any blocking hit. No third-party dependencies (uses tomllib).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_RULES = os.path.join(_HERE, "rules.default.toml")

_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv",
              "__pycache__", ".next", ".svelte-kit", "vendor", ".cache"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
               ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
               ".so", ".dylib", ".dll", ".class", ".jar", ".wasm", ".lock"}
_MAX_BYTES = 2 * 1024 * 1024


def _mask(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[0] + "***" if s else ""
    return s[:4] + "***" + s[-2:]


def load_rules(overlay: str | None = None) -> dict:
    """Load default rules, then layer an optional local overlay on top."""
    def _read(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    data = _read(_DEFAULT_RULES)
    rules = list(data.get("rule", []))
    risky = list(data.get("risky_file", []))
    overlay = overlay or os.environ.get("AIWERKSTATT_SCRUB_RULES")
    if overlay and os.path.isfile(overlay):
        ov = _read(overlay)
        rules += list(ov.get("rule", []))
        risky += list(ov.get("risky_file", []))
    compiled = [(r["name"], r.get("severity", "review"),
                 re.compile(r["pattern"]))
                for r in rules if r.get("pattern")]
    risky_c = [(r.get("severity", "review"), re.compile(r["pattern"], re.I))
               for r in risky if r.get("pattern")]
    return {"rules": compiled, "risky": risky_c}


def scan_tree(root: str, overlay: str | None = None) -> dict:
    ruleset = load_rules(overlay)
    findings: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            ext = os.path.splitext(fn)[1].lower()
            # Risky-filename rules match on the path itself.
            for severity, rx in ruleset["risky"]:
                if rx.search(rel.replace("\\", "/")):
                    findings.append({"file": rel, "line": 0, "severity": severity,
                                     "rule": "risky-file", "snippet": fn})
            if ext in _BINARY_EXT:
                continue
            try:
                if os.path.getsize(full) > _MAX_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        for name, severity, rx in ruleset["rules"]:
                            m = rx.search(line)
                            if m:
                                findings.append({"file": rel, "line": i,
                                                 "severity": severity, "rule": name,
                                                 "snippet": _mask(m.group(0))})
            except OSError:
                continue
    blocking = sum(1 for f in findings if f["severity"] == "block")
    review = sum(1 for f in findings if f["severity"] == "review")
    return {"ok": blocking == 0, "blocking": blocking, "review": review,
            "findings": findings}


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    res = scan_tree(root)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    if res["blocking"]:
        print(f"\nLEAK SCAN FAILED: {res['blocking']} blocking finding(s).",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
