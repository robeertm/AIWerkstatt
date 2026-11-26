"""Priority in the task queue (0.48.0): one rule for the order in which waiting orders get the next free slot.

One file, two roles:

1. **Writer** = the Flask route ``POST /api/queue/<thread_id>/priority`` (admins only). ``change()`` works under a file
   lock, writes atomically (tmp + rename) and logs every change as one JSON line (who, when, what).
2. **Reader** = the orchestrator (who starts next) and the pool overview (order, position, start time). Both sort with
   ``sort_key()`` - exactly one rule, never two copies.

Rules:
  * Without any priority the queue stays strictly FIFO by task id (control messages stop/compact first).
  * ⚡ urgent always stands before normal; inside the class strictly FIFO (urgent/normal reset the manual rank).
  * ⏫ next (front of the own class) / ▲ up / ▼ down / to the end / to position n move an order only INSIDE its own
    class (urgent or normal). At the edge nothing happens (``unchanged``). An order with several waiting parts (the order
    plus follow-ups of the same thread) moves as one block.
  * Priority acts on waiting orders only: a running order is never touched, the priority only decides who gets the
    next free slot. Entries of orders that no longer wait are dropped by every change.
  * A manual rank is a number between the new neighbours (default rank = task id), so only the moved block gets a
    new rank and everybody else keeps their FIFO place.

File ``<data>/queue-priority.json``:
    {"format": "queue-priority/1", "stand": <unix>, "orders": {"<task_id>": {"urgent": bool,
     "rank": float|null, "by": "<name>", "at": "<iso>"}}}
"""
import fcntl
import json
import os
import time
from datetime import datetime, timezone

FORMAT = "queue-priority/1"
FILE_NAME = "queue-priority.json"
LOG_NAME = "queue-priority.jsonl"
ACTIONS = ("front", "up", "down", "urgent", "normal", "end", "position")
ACTION_TEXT = {"front": "moved to the front", "up": "moved up one place", "down": "moved down one place",
               "urgent": "marked urgent", "normal": "back to normal", "end": "moved to the end",
               "position": "moved to a fixed position"}
NO_ID = 10 ** 12
CONTROL = ("stop", "compact")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty():
    return {"format": FORMAT, "stand": 0, "orders": {}}


def load(path):
    """Read the priority file; missing or broken -> empty (= plain FIFO). Never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return empty()
    if not isinstance(d, dict) or not isinstance(d.get("orders"), dict):
        return empty()
    return d


def _tid(d):
    t = str((d or {}).get("id", ""))
    return int(t) if t.isdigit() else NO_ID


def entry(d, prio):
    """-> (urgent, rank) of a descriptor. The file entry beats the descriptor field ``urgent``."""
    e = ((prio or {}).get("orders") or {}).get(str((d or {}).get("id", "")))
    e = e if isinstance(e, dict) else {}
    urgent = bool(e["urgent"]) if "urgent" in e else bool((d or {}).get("urgent"))
    rank = e.get("rank")
    if not isinstance(rank, (int, float)) or isinstance(rank, bool):
        rank = _tid(d)
    return urgent, float(rank)


def is_urgent(d, prio):
    return entry(d, prio)[0]


def sort_key(d, prio, mtime=0.0):
    """Sort key for ONE descriptor: 0 control messages, then 1 urgent / 2 normal by rank, then task id."""
    if (d or {}).get("kind", "new") in CONTROL:
        return (0, 0, float(mtime), 0)
    urgent, rank = entry(d, prio)
    return (1, 0 if urgent else 1, rank, _tid(d))


def order(descs, prio):
    """Descriptors without control messages in start order."""
    return sorted([d for d in descs if (d or {}).get("kind", "new") not in CONTROL],
                  key=lambda d: sort_key(d, prio))


def read_queue(queue_dir):
    out = []
    try:
        names = [n for n in os.listdir(queue_dir) if n.endswith(".json")]
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(queue_dir, n), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _write(path, data):
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _log(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _position(rows, ids):
    for i, d in enumerate(rows, 1):
        if _tid(d) in ids:
            return i
    return None


def orders_view(descs, prio):
    """One row per ORDER (thread) in start order, with what the overview needs to draw the controls.
    -> [{"thread_id", "task_ids", "slug", "urgent", "position", "followups", "can_up", "can_down", "class_position",
         "class_size"}]. ``position`` counts orders (1 = starts next); ``class_position`` counts inside the own class."""
    rows, idx = [], {}
    for d in order(descs, prio):
        th = str(d.get("thread_id") or "")
        if th and th in idx:
            r = rows[idx[th]]
            r["task_ids"].append(_tid(d))
            r["followups"] += 1 if d.get("kind") == "followup" else 0
            r["urgent"] = r["urgent"] or is_urgent(d, prio)
            continue
        if th:
            idx[th] = len(rows)
        rows.append({"thread_id": d.get("thread_id"), "task_ids": [_tid(d)], "slug": d.get("slug"),
                     "urgent": is_urgent(d, prio), "followups": 1 if d.get("kind") == "followup" else 0})
    for cls in (True, False):
        members = [r for r in rows if r["urgent"] is cls]
        for i, r in enumerate(members):
            r["class_position"], r["class_size"] = i + 1, len(members)
            r["can_up"], r["can_down"] = i > 0, i < len(members) - 1
    for i, r in enumerate(rows, 1):
        r["position"] = i
    return rows


def change(path, queue_dir, thread_id, action, by, slug=None, log=None, position=None):
    """Change the priority of a waiting order. -> dict {ok, unchanged, before, after, urgent, task_ids, ...}.
    Raises LookupError when the order does not wait (any more), ValueError for an unknown action or position."""
    if action not in ACTIONS:
        raise ValueError("Unknown action %r (allowed: %s)." % (action, ", ".join(ACTIONS)))
    if action == "position":
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise ValueError("position: whole number from 1 (1 = first waiting order of the own class).")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prio = load(path)
        alle = read_queue(queue_dir)
        rows = order(alle, prio)
        waiting = {_tid(d) for d in rows}
        target = [d for d in rows if str(d.get("thread_id")) == str(thread_id)]
        if not target:
            raise LookupError("The order is not waiting (it is running, finished or unknown) - priority only acts on "
                              "waiting orders.")
        ids = {_tid(d) for d in target}
        before = _position(rows, ids)
        orders = {k: v for k, v in (prio.get("orders") or {}).items()
                  if str(k).isdigit() and int(k) in waiting and isinstance(v, dict)}
        now = _now_iso()
        urgent_before = is_urgent(target[0], prio)
        if action in ("urgent", "normal"):
            for t in ids:
                orders[str(t)] = {"urgent": action == "urgent", "rank": None, "by": by, "at": now}
        else:
            # move only inside the own class (same urgency)
            cls = is_urgent(target[0], prio)
            group = [d for d in rows if is_urgent(d, prio) == cls]
            block = [d for d in group if _tid(d) in ids]
            rest = [d for d in group if _tid(d) not in ids]
            first, last = group.index(block[0]), group.index(block[-1])
            if action == "front":
                new = block + rest
            elif action == "end":
                new = rest + block
            elif action == "position":
                i = min(position - 1, len(rest))
                new = rest[:i] + block + rest[i:]
            elif action == "up":
                before_me = [d for d in group[:first] if _tid(d) not in ids]
                if not before_me:
                    new = group
                else:
                    i = rest.index(before_me[-1])
                    new = rest[:i] + block + rest[i:]
            else:   # down
                after_me = [d for d in group[last + 1:] if _tid(d) not in ids]
                if not after_me:
                    new = group
                else:
                    i = rest.index(after_me[0])
                    new = rest[:i + 1] + block + rest[i + 1:]
            if [_tid(d) for d in new] != [_tid(d) for d in group]:
                i = [_tid(d) for d in new].index(_tid(block[0]))
                r_before = entry(new[i - 1], prio)[1] if i > 0 else None
                j = i + len(block)
                r_after = entry(new[j], prio)[1] if j < len(new) else None
                if r_before is None and r_after is None:
                    r_before, r_after = 0.0, 1.0
                elif r_before is None:
                    r_before = r_after - 1.0
                elif r_after is None:
                    r_after = r_before + 1.0
                step = (r_after - r_before) / (len(block) + 1)
                for k, d in enumerate(block, 1):
                    orders[str(_tid(d))] = {"urgent": entry(d, prio)[0], "rank": r_before + k * step,
                                            "by": by, "at": now}
        new_prio = {"format": FORMAT, "stand": int(time.time()), "orders": orders}
        rows_new = order(alle, new_prio)
        after = _position(rows_new, ids)
        unchanged = (after == before and is_urgent(target[0], new_prio) == urgent_before)
        if not unchanged:
            _write(path, new_prio)
        result = {"ok": True, "unchanged": unchanged, "before": before, "after": after,
                  "urgent": is_urgent(target[0], new_prio), "task_ids": sorted(ids), "action": action,
                  "length": len(rows_new)}
        if action == "position":
            result["position"] = position
        _log(log or os.path.join(os.path.dirname(path), LOG_NAME),
             {"at": now, "by": by, "action": action, "slug": slug, "thread_id": thread_id,
              "task_ids": sorted(ids), "before": before, "after": after,
              "position": position if action == "position" else None, "urgent": result["urgent"],
              "unchanged": unchanged})
        return result
