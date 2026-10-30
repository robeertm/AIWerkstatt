"""Service tokens: audit log, write rate limit and a small admin CLI.

Tokens themselves live in the database as a SHA-256 digest only (see store.py). This
module adds what the web process needs around them, plus a command line for the operator:

    python3 service_token.py list
    python3 service_token.py create <name> [--profile restricted|coordinator]
    python3 service_token.py register <name> [--profile ...] [--sha256 <hex>]
    python3 service_token.py disable <name>      # kill switch on (401 at once)
    python3 service_token.py enable <name>       # kill switch off
    python3 service_token.py remove <name>

``create`` prints the new token exactly once. ``register`` never sees or prints a plain
token it did not get: it stores a digest passed with ``--sha256``, or hashes the token it
reads from stdin (without echo on a terminal) and forgets it. In Docker, run it as the app
user inside the web container, e.g. ``docker compose exec -u app web python3 service_token.py list``.

Audit: every request that presents a known token (also refused or switched-off ones) is
appended to ``<DATA>/service-token-audit.jsonl`` as one JSON line: time, token name,
profile, method, route, path, project, HTTP status. Never the token, never a header.
"""
import argparse
import collections
import getpass
import hashlib
import json
import os
import sys
import threading
import time

import config
import store

AUDIT_LOG = os.path.join(config.DATA, "service-token-audit.jsonl")
_audit_lock = threading.Lock()


def audit(entry):
    """Append one entry to the audit log (append-only, owner read/write only). A failed
    write is reported on stderr and never breaks the request."""
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with _audit_lock:
            os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
            fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
    except OSError as e:
        print("service-token audit: could not write: %s" % e, file=sys.stderr)


def _write_limit():
    try:
        return max(0, int(os.environ.get("AIWERKSTATT_API_WRITE_LIMIT", "30")))
    except ValueError:
        return 30


class WriteLimiter:
    """Sliding one-hour window per token name. ``limit`` 0 means unlimited. In memory: a
    restart starts every window afresh."""

    WINDOW = 3600

    def __init__(self, limit=None):
        self.limit = _write_limit() if limit is None else limit
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, name, now=None):
        """Count one write for ``name``. Returns (allowed, retry_after_seconds)."""
        if not self.limit:
            return True, 0
        now = time.time() if now is None else now
        with self._lock:
            q = self._hits[name]
            while q and q[0] <= now - self.WINDOW:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(1, int(q[0] + self.WINDOW - now) + 1)
            q.append(now)
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


# ---------- command line ----------

def _read_plain_token():
    if sys.stdin.isatty():
        return getpass.getpass("Token (input hidden): ")
    return sys.stdin.readline()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="service_token.py", description="Manage service tokens.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="names, profiles and kill-switch state (never a hash)")
    for cmd in ("create", "register"):
        p = sub.add_parser(cmd)
        p.add_argument("name")
        p.add_argument("--profile", default="restricted", choices=store.TOKEN_PROFILES)
        if cmd == "register":
            p.add_argument("--sha256", help="SHA-256 hex digest of the token (else: token on stdin)")
    for cmd in ("disable", "enable", "remove"):
        sub.add_parser(cmd).add_argument("name")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "list":
            for t in store.api_token_list():
                print("%-24s %-12s %s" % (t["name"], t["profile"], "DISABLED" if t["disabled"] else "active"))
        elif a.cmd == "create":
            token = store.api_token_create(a.name, a.profile)
            print("Token for %s (%s), shown once:" % (a.name.strip().lower(), a.profile), file=sys.stderr)
            print(token)
        elif a.cmd == "register":
            digest = a.sha256
            if not digest:
                plain = _read_plain_token().strip()
                if not plain:
                    raise ValueError("No token on stdin.")
                digest = hashlib.sha256(plain.encode("utf-8")).hexdigest()
                del plain
            name = store.api_token_register(a.name, digest, a.profile)
            print("Registered %s (%s)." % (name, a.profile))
        elif a.cmd == "disable":
            store.api_token_set_disabled(a.name, True)
            print("Disabled %s (kill switch on)." % a.name.strip().lower())
        elif a.cmd == "enable":
            store.api_token_set_disabled(a.name, False)
            print("Enabled %s (kill switch off)." % a.name.strip().lower())
        elif a.cmd == "remove":
            store.api_token_delete(a.name)
            print("Removed %s." % a.name.strip().lower())
    except ValueError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
