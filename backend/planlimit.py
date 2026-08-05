"""Per-user real plan-limit status (session / weekly utilisation) for the cockpit.

Anthropic exposes the same numbers the Claude `/usage` dialog shows at
``GET https://api.anthropic.com/api/oauth/usage`` — but only to a token that carries the
``user:profile`` scope. The token a project uses for *inference* (from ``claude setup-token``)
does NOT have that scope, so each user optionally connects their own Claude account once via a
standard OAuth PKCE login (``user:profile``). The resulting token is stored per user, refreshed
on demand, and the usage endpoint is polled (short cache).

Entirely optional and defensive: if a user hasn't connected, or anything fails, the status is
simply unavailable — the workshop never breaks. Both endpoints require the CLI User-Agent,
otherwise Cloudflare answers 403 "error code: 1010".
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

import store

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"      # public Claude Code OAuth client
_UA = "claude-cli/2.1.220 (external, cli)"
_BETA = "oauth-2025-04-20"
_AUTHORIZE = "https://claude.com/cai/oauth/authorize"
_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_REDIRECT = "https://platform.claude.com/oauth/code/callback"
_SCOPE = ("org:create_api_key user:profile user:inference "
          "user:sessions:claude_code user:mcp_servers user:file_upload")

_TOK_KEY = "planlimit_token:%s"    # per-user stored token {access, refresh, expires_at}
_PKCE_KEY = "planlimit_pkce:%s"    # per-user transient {verifier, state} during connect
_TTL = 300                          # cache usage per user for 5 min (endpoint is throttled)

_lock = threading.Lock()
_cache = {}   # user -> (fetched_at, status_dict)


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _post(url, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": _UA,
        "anthropic-beta": _BETA, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _fetch_usage(access):
    req = urllib.request.Request(_USAGE_URL, headers={
        "Authorization": "Bearer " + access, "User-Agent": _UA, "anthropic-beta": _BETA})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


# ---------------------------------------------------------------- connect (PKCE)
def start_connect(user):
    """Return the authorize URL for this user; stash the PKCE verifier/state server-side."""
    verifier = _b64(secrets.token_bytes(32))
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    state = _b64(secrets.token_bytes(32))
    store.set_secret(_PKCE_KEY % user, {"verifier": verifier, "state": state})
    return _AUTHORIZE + "?" + urlencode({
        "code": "true", "client_id": CLIENT_ID, "response_type": "code",
        "redirect_uri": _REDIRECT, "scope": _SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def finish_connect(user, code_raw):
    """Exchange the pasted code (``code#state``) for tokens and store them for this user."""
    pk = store.get_secret(_PKCE_KEY % user)
    if not pk:
        return False, "No pending connection — start again."
    code, _, state = (code_raw or "").strip().partition("#")
    if not code:
        return False, "Empty code."
    if state and pk.get("state") and state != pk["state"]:
        return False, "State mismatch — start again."
    try:
        r = _post(_TOKEN_URL, {
            "grant_type": "authorization_code", "code": code,
            "state": state or pk.get("state"), "code_verifier": pk["verifier"],
            "client_id": CLIENT_ID, "redirect_uri": _REDIRECT})
    except Exception:
        return False, "Could not exchange the code — try connecting again."
    if not r.get("access_token"):
        return False, "No token returned."
    _save(user, r)
    store.delete_secret(_PKCE_KEY % user)
    _cache.pop(user, None)
    return True, None


def _save(user, r):
    store.set_secret(_TOK_KEY % user, {
        "access": r["access_token"], "refresh": r.get("refresh_token"),
        "expires_at": int(time.time()) + int(r.get("expires_in", 3600))})


def _refresh(user, tok):
    r = _post(_TOKEN_URL, {"grant_type": "refresh_token",
                           "refresh_token": tok.get("refresh"), "client_id": CLIENT_ID})
    tok["access"] = r["access_token"]
    if r.get("refresh_token"):
        tok["refresh"] = r["refresh_token"]
    tok["expires_at"] = int(time.time()) + int(r.get("expires_in", 3600))
    store.set_secret(_TOK_KEY % user, tok)
    return tok


def is_connected(user):
    return bool(user and store.get_secret(_TOK_KEY % user))


def disconnect(user):
    store.delete_secret(_TOK_KEY % user)
    _cache.pop(user, None)


# ---------------------------------------------------------------- status (cached, lazy)
def get_status(user):
    """Per-user plan usage, cached ``_TTL`` s. Never raises; returns a plain dict."""
    if not user:
        return {"connected": False}
    tok = store.get_secret(_TOK_KEY % user)
    if not tok:
        return {"connected": False}
    with _lock:
        c = _cache.get(user)
        if c and time.time() - c[0] < _TTL:
            return c[1]
    try:
        if time.time() > int(tok.get("expires_at", 0)) - 300:
            tok = _refresh(user, tok)
        u = _fetch_usage(tok["access"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            try:
                tok = _refresh(user, tok)
                u = _fetch_usage(tok["access"])
            except Exception:
                return _last_or(user, {"connected": True, "available": False})
        else:
            return _last_or(user, {"connected": True, "available": False})
    except Exception:
        return _last_or(user, {"connected": True, "available": False})
    fh = u.get("five_hour") or {}
    sd = u.get("seven_day") or {}
    out = {"connected": True, "available": True,
           "session": {"pct": fh.get("utilization"), "resets_at": fh.get("resets_at")},
           "weekly": {"pct": sd.get("utilization"), "resets_at": sd.get("resets_at")}}
    with _lock:
        _cache[user] = (time.time(), out)
    return out


def _last_or(user, fallback):
    """On a transient failure keep showing the last good value rather than blanking."""
    c = _cache.get(user)
    return c[1] if c else fallback
