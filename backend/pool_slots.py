"""Runtime setting "active worker slots" (0.34.0).

The worker pool has ``AIWERKSTATT_MAX_PARALLEL_RUNS`` slots. An admin can switch the pool down to N active
slots (0..max) at runtime: the pool only starts a new run while the number of live runs is below N.
Runs that are already working are never aborted; they finish and the slot then stays empty.
0 = nothing new starts, every order waits in the queue.

The value lives in ``<data>/pool-slots.json`` (written atomically, survives restarts); every change adds one
line to ``<data>/pool-slots.jsonl`` (who, when, before -> after). A missing or broken file means "all slots
active" (the behaviour before 0.34.0). Only the web process writes; the orchestrator only reads.
"""
import datetime
import json
import os

try:
    import fcntl
except ImportError:      # pragma: no cover - not on Windows hosts running the backend
    fcntl = None

import config

FORMAT = "pool-slots/1"


def _file():
    return os.path.join(config.DATA, "pool-slots.json")


def _log_file():
    return os.path.join(config.DATA, "pool-slots.jsonl")


def stored():
    """The stored number of active slots, or None when nothing (valid) is stored."""
    try:
        with open(_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        n = d.get("active")
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            return n
    except (OSError, ValueError, AttributeError):
        pass
    return None


def limit(max_runs):
    """Effective cap on simultaneous runs: None = unlimited, else 0..max_runs.
    With an unlimited pool (max_runs <= 0) the setting does not apply."""
    if max_runs is None or max_runs <= 0:
        return None
    n = stored()
    return max_runs if n is None else min(n, max_runs)


def view(max_runs):
    """Additive snapshot fields: ``active`` (None = unlimited) and ``configurable``."""
    return {"active": limit(max_runs), "configurable": bool(max_runs and max_runs > 0)}


def parse(value, max_runs):
    """Validate a requested count: an int 0..max_runs (no bool, no float, no text)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("active must be a whole number")
    if not max_runs or max_runs <= 0:
        raise ValueError("the pool is unlimited; there is nothing to switch")
    if value < 0 or value > max_runs:
        raise ValueError("active must be between 0 and %d" % max_runs)
    return value


def set_active(value, max_runs, who):
    """The one write path. Returns {"before", "after", "max"}; raises ValueError on bad input."""
    n = parse(value, max_runs)
    os.makedirs(config.DATA, exist_ok=True)
    lock = open(os.path.join(config.DATA, ".pool-slots.lock"), "a")
    try:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        before = limit(max_runs)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        tmp = _file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": FORMAT, "active": n, "since": now, "by": str(who or "")[:80]}, f)
        os.replace(tmp, _file())
        try:
            with open(_log_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": now, "by": str(who or "")[:80], "before": before, "after": n,
                                    "max": max_runs}) + "\n")
        except OSError:
            pass
    finally:
        lock.close()
    return {"before": before, "after": n, "max": max_runs}


def waiting_reason(max_runs, waiting):
    """Text for the queue when slots are switched off (None when nothing is held back by the switch)."""
    cap = limit(max_runs)
    if cap is None or cap >= max_runs or waiting <= 0:
        return None
    if cap == 0:
        return "waiting because all slots are switched off"
    return "waiting because only %d of %d slots are active" % (cap, max_runs)
