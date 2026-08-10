#!/usr/bin/env python3
"""AIWerkstatt session driver — one long-lived agent session per task, in a
container (provider-neutral).

Ported from the proven production driver. The valuable core is unchanged:
  * an ORDERED expectation queue maps each fed message to exactly one result,
  * mid-turn follow-ups folded by the provider are resolved together,
  * a context meter drives AUTO-COMPACTION so the session never dies on a full
    context, and the UI can trigger `/compact` manually,
  * usage (output tokens / cost) and rate-limit handling are tracked and
    reported, unanswered tasks are requeued on an unexpected exit.

Isolation is the CONTAINER, so this process is simply the direct parent of the
provider CLI and writes to its stdin pipe (no extra plumbing). Everything
provider-specific (argv, stdin encoding, stream parsing, compaction) lives
behind an ``AgentAdapter`` (see ``providers/``). Credentials arrive in the
environment (sourced by the entrypoint), never on the command line.
"""
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from providers import get_adapter, RunContext  # noqa: E402

# ---------------------------------------------------------------- env contract
SLUG        = os.environ["WK_SLUG"]
THREAD_ID   = int(os.environ["WK_THREAD_ID"])
REPO        = os.environ.get("WK_REPO", "")
PROVIDER    = os.environ.get("WK_PROVIDER", "claude")
MODEL       = os.environ.get("WK_MODEL", "")
EFFORT      = (os.environ.get("WK_EFFORT", "") or "").strip().lower()
ALLOWED     = os.environ.get("WK_ALLOWED",
                             "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,TodoWrite")
WT          = os.environ.get("WK_WT", "/workspace")
FIRST_MSG_F = os.environ["WK_FIRST_MSG_FILE"]
FIRST_TASK  = int(os.environ["WK_FIRST_TASK"])
RESUME_SID  = os.environ.get("WK_RESUME_SID", "").strip()

EVENTS      = os.environ.get("WK_EVENTS", "/events")
USAGE_DIR   = os.environ.get("WK_USAGE_DIR", os.path.join(EVENTS, "usage"))
STATUS_FILE = os.environ.get("WK_STATUS_FILE", os.path.join(EVENTS, "status-%d.json" % THREAD_ID))
SESS_STATE  = os.environ.get("WK_SESS_STATE", os.path.join(EVENTS, "sess-%d.txt" % THREAD_ID))
LIMITFILE   = os.environ.get("WK_LIMITFILE", os.path.join(EVENTS, "limit.json"))
LASTFILE    = os.environ.get("WK_LASTFILE", os.path.join(EVENTS, "limit-last.json"))
LIVEFILE    = os.environ.get("WK_LIVEFILE", os.path.join(EVENTS, "live-%d.jsonl" % THREAD_ID))
INBOX       = os.environ.get("WK_INBOX", "/inbox")
RUNLOG      = os.environ.get("WK_RUNLOG", "/tmp/agent-runlog.jsonl")

# How long a session stays open after "done" so a quick follow-up lands in the
# same live conversation. Kept short — once the agent is finished, the slot frees
# up fast; a later follow-up resumes the session (full context preserved). Raise
# via WK_IDLE_TIMEOUT if you want a longer warm window.
IDLE_TIMEOUT = int(os.environ.get("WK_IDLE_TIMEOUT", "30"))
MAX_SESSION  = int(os.environ.get("WK_MAX_SESSION", "7200"))
# Stall watchdog: if the agent produces NO output for this long while tasks are still
# pending (e.g. it hangs in an API-retry dead end / on a dead socket), treat it as stuck,
# terminate it and re-queue the tasks. Without this the session would stay open forever
# (the idle timeout only fires when nothing is pending) and block the project's slot.
STALL_TIMEOUT = int(os.environ.get("WK_STALL_TIMEOUT", "600"))
AUTO_PCT     = int(os.environ.get("WK_AUTO_COMPACT_PCT", "80"))
LIMIT_WAIT   = int(os.environ.get("WK_LIMIT_WAIT", "1800"))

_seq = 0
adapter = get_adapter(PROVIDER)
MODEL = MODEL or (adapter.model if adapter else "")
CTX_WINDOW = adapter.default_context_window(MODEL) if adapter else 200_000


def log(msg):
    sys.stderr.write("[session %s/%s] %s\n" % (SLUG, THREAD_ID, msg))
    sys.stderr.flush()


def _atomic_write(path, data, mode=0o644):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        log("write %s failed: %s" % (path, e))


def emit(task_id, etype, text, payload=None):
    """Typed event into the shared events volume (the web control plane ingests it)."""
    global _seq
    _seq += 1
    d = {"task_id": (int(task_id) if task_id is not None else None),
         "thread_id": THREAD_ID, "repo": REPO, "type": etype, "text": text, "author": "Agent"}
    if payload is not None:
        d["payload"] = payload
    name = "%s.%d%03d.json" % (task_id if task_id is not None else "t", time.time_ns(), _seq % 1000)
    _atomic_write(os.path.join(EVENTS, name), json.dumps(d, ensure_ascii=False))


_live_seq = 0


def emit_activity(act, text):
    """Append one line to the shared live-activity feed (the web tails it so the
    user can watch what the agent is doing, step by step). Append-only; the web
    reads it incrementally by byte offset."""
    global _live_seq
    _live_seq += 1
    rec = {"seq": _live_seq, "act": act, "text": text, "at": int(time.time())}
    try:
        os.makedirs(os.path.dirname(LIVEFILE), exist_ok=True)
        with open(LIVEFILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log("live write failed: %s" % e)


def write_status(ctx_pct, alive, pending, compacting=False):
    _atomic_write(STATUS_FILE, json.dumps(
        {"alive": bool(alive), "ctx_pct": int(ctx_pct), "pending": int(pending),
         "compacting": bool(compacting), "thread_id": THREAD_ID, "slug": SLUG,
         "updated": int(time.time())}, ensure_ascii=False))


def write_limit(until, since, known=False):
    # `known` = the resume time is a REAL provider reset (not a retry-cadence guess); the
    # banner only shows a clock time when it's true.
    for p in (LIMITFILE, LASTFILE):
        _atomic_write(p, json.dumps({"until": int(until), "since": int(since), "known": bool(known)}))


def save_session_id(sid):
    _atomic_write(SESS_STATE, sid)


def start_agent():
    """Start the provider CLI as a child process: stdin = pipe (we feed messages),
    stdout+stderr → runlog file (we tail it). The container is the isolation
    boundary."""
    argv = adapter.build_argv(RunContext(model=MODEL, effort=EFFORT,
                                         resume_id=RESUME_SID, allowed_tools=ALLOWED))
    log("exec: %s" % " ".join(argv))
    runlog_fh = open(RUNLOG, "wb")
    proc = subprocess.Popen(argv, cwd=WT, stdin=subprocess.PIPE,
                            stdout=runlog_fh, stderr=subprocess.STDOUT)
    return proc


def main():
    if adapter is None:
        emit(FIRST_TASK, "failed", "Unknown provider '%s'." % PROVIDER)
        print(json.dumps({"ok": False, "error": "unknown provider", "requeue": [FIRST_TASK]}))
        return
    log("start (provider=%s model=%s effort=%s resume=%s)"
        % (PROVIDER, MODEL, EFFORT or "default", RESUME_SID or "no"))
    proc = start_agent()

    def feed(text):
        try:
            proc.stdin.write(adapter.encode_user_message(text))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            log("feed failed: %s" % e)

    with open(FIRST_MSG_F, "r", encoding="utf-8") as f:
        first = f.read()
    emit(FIRST_TASK, "ack", "Received — the agent is starting.")
    feed(first)
    expected = deque()
    expected.append(("task", FIRST_TASK))

    # tail the runlog
    rlf = None
    for _ in range(200):
        if os.path.exists(RUNLOG):
            rlf = open(RUNLOG, "r", encoding="utf-8", errors="replace")
            break
        time.sleep(0.05)
    if rlf is None:
        rlf = open(RUNLOG, "a+", encoding="utf-8", errors="replace"); rlf.seek(0)

    start = time.time()
    last_activity = start
    last_output = start        # stall watchdog: when did the agent last produce ANY output?
    ctx_pct = 0
    ctx_window = CTX_WINDOW
    sid = None
    sess_out = 0
    sess_cost = 0.0
    sess_turns = 0
    stopped = False
    controls_since_compact = 0
    should_close = False
    limited_close = None   # (task_id, reset)
    processed_inbox = set()
    can_compact = adapter.compact_message() is not None

    def handle_result(ev):
        nonlocal ctx_pct, last_activity, should_close, limited_close, controls_since_compact
        nonlocal ctx_window, sess_out, sess_cost, sess_turns
        sess_out += int(ev.get("out_tokens", 0) or 0)
        sess_cost = max(sess_cost, float(ev.get("cost", 0) or 0))
        sess_turns += 1
        if ev.get("window"):
            ctx_window = ev["window"]
        head = expected.popleft() if expected else ("task", None)
        if ev.get("limited"):
            reset = int(ev.get("reset", 0) or 0)
            tid = head[1] if head[0] == "task" else None
            limited_close = (tid, reset)
            should_close = True
            return
        if head[0] == "control":
            log("compact done (ctx now %d%%)" % ctx_pct)
            last_activity = time.time()
            return
        tid = head[1]
        text = ev.get("text") or ""
        if ev.get("error") or not text:
            emit(tid, "failed", "The run ended without a reply. Your workspace is unchanged — "
                                "send your message again to try once more.")
        else:
            emit(tid, "reply", text[:6000])
        # One result = the agent processed everything fed so far. A mid-turn
        # follow-up folded into this turn → resolve the remaining expected tasks.
        while expected:
            k2, t2 = expected.popleft()
            if k2 == "task" and t2 is not None:
                emit(t2, "reply", "↳ Handled together with the previous answer (folded follow-up).")
        controls_since_compact = 0
        last_activity = time.time()

    while True:
        # 1) new runlog lines → normalised events
        for line in rlf:
            last_output = time.time()   # any byte from the agent → it's alive (stall watchdog)
            for ev in adapter.parse_line(line):
                kind = ev.get("kind")
                if kind == "session":
                    if not sid:
                        sid = ev["id"]; save_session_id(sid)
                elif kind == "ctx":
                    if ev.get("window"):
                        ctx_window = ev["window"]
                    c = int(ev.get("tokens", 0) or 0)
                    if c:
                        ctx_pct = min(100, round(100 * c / ctx_window)) if ctx_window else 0
                elif kind == "limit":
                    head = expected.popleft() if expected else ("task", None)
                    limited_close = (head[1] if head[0] == "task" else None,
                                     int(ev.get("reset", 0) or 0))
                    should_close = True
                elif kind == "activity":
                    emit_activity(ev.get("act", "say"), ev.get("text", ""))
                    last_activity = time.time()
                elif kind == "result":
                    handle_result(ev)

        # 2) inbox: follow-ups / compact / stop
        try:
            names = sorted(os.listdir(INBOX), key=lambda n: (len(n), n))
        except OSError:
            names = []
        for n in names:
            if not n.endswith(".json") or n in processed_inbox:
                continue
            p = os.path.join(INBOX, n)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                processed_inbox.add(n)
                try: os.remove(p)
                except OSError: pass
                continue
            processed_inbox.add(n)
            try: os.remove(p)
            except OSError: pass
            kind = d.get("kind", "followup")
            if kind == "stop":
                log("STOP received — terminating agent")
                try: proc.terminate()
                except Exception: pass
                for k2, t2 in list(expected):
                    if k2 == "task" and t2 is not None:
                        emit(t2, "stopped", "⏹️ Task stopped.")
                expected.clear()
                stopped = True
                should_close = True
                break
            if kind == "compact":
                if can_compact:
                    feed("/compact")
                    expected.append(("control",))
                    controls_since_compact = 0
                    write_status(ctx_pct, 1, len(expected), compacting=True)
                    log("manual compact fed")
            else:  # followup
                tid = int(d.get("id"))
                txt = d.get("text") or ""
                emit(tid, "ack", "Follow-up received — the agent folds it into the running task.")
                feed(txt)
                expected.append(("task", tid))
                last_activity = time.time()
                log("follow-up task %d fed live" % tid)

        # 3) auto-compact (deterministic safety net)
        if can_compact and ctx_pct >= AUTO_PCT and not expected and controls_since_compact == 0:
            feed("/compact")
            expected.append(("control",))
            controls_since_compact = 1
            write_status(ctx_pct, 1, len(expected), compacting=True)
            log("AUTO-COMPACT at %d%%" % ctx_pct)

        write_status(ctx_pct, 1, len(expected))

        # 4) exit conditions
        if should_close:
            break
        if proc.poll() is not None:
            log("agent process ended (rc=%s) — closing session" % proc.returncode)
            break
        now = time.time()
        # Stall watchdog: tasks are pending but the agent has gone silent for too long → it's
        # hung (API-retry dead end / dead socket). Terminate it; the pending tasks are re-queued
        # in teardown (auto-retry up to MAX_RETRIES). Keeps a hung agent from blocking the project.
        if expected and now - last_output > STALL_TIMEOUT:
            log("stall watchdog: no output for %ds with %d task(s) pending — agent hung, terminating + re-queueing"
                % (int(now - last_output), len(expected)))
            try:
                proc.terminate()
            except Exception:
                pass
            break
        idle = not expected
        if idle and now - last_activity > IDLE_TIMEOUT:
            log("idle timeout — closing session"); break
        if now - start > MAX_SESSION:
            log("max session reached — closing"); break
        time.sleep(0.4)

    # ---------------------------------------------------------------- teardown
    result = {"session_id": sid, "limited": None, "reset": 0, "requeue": [], "ok": True}

    if limited_close is not None:
        tid, reset = limited_close
        now = int(time.time())
        # Only show a concrete resume time when the provider gave a REAL reset time.
        # Agents here run on the project's own credential (usually an API key), which has
        # no plan "session reset" — so if the event carries none, we retry after LIMIT_WAIT
        # but must NOT invent a clock time (that reads as a bogus value). Word it honestly.
        real = bool(reset and reset > now)
        if not real:
            reset = now + LIMIT_WAIT   # internal retry cadence only — never shown as a time
        write_limit(reset, now, real)
        if real:
            msg = ("⏸️ Usage limit reached — the agent resumes automatically at %s."
                   % time.strftime("%H:%M", time.localtime(reset)))
        else:
            msg = "⏸️ Usage limit reached — the agent resumes automatically once the limit resets."
        if tid is not None:
            emit(tid, "limited", msg, {"reset": reset, "reset_known": real})
            result["requeue"].append(tid)
        result["limited"] = reset
        result["reset"] = reset

    for kind, tid in expected:
        if kind == "task" and tid is not None and tid not in result["requeue"]:
            emit(tid, "failed", "Session ended before answering — the task will be retried.")
            result["requeue"].append(tid)

    try:
        if proc.stdin:
            proc.stdin.close()   # EOF → CLI ends the session cleanly
    except OSError:
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill()
    write_status(ctx_pct, 0, 0)

    if sess_out or sess_cost:
        _atomic_write(os.path.join(USAGE_DIR, "%s-%d-%d.json" % (SLUG, THREAD_ID, int(time.time()))),
                      json.dumps({"slug": SLUG, "thread_id": THREAD_ID, "provider": PROVIDER,
                                  "model": MODEL, "output_tokens": sess_out,
                                  "cost_usd": round(sess_cost, 4), "turns": sess_turns,
                                  "at": int(time.time()), "stopped": stopped}, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))
    log("session ended")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never die silently
        log("FATAL: %r" % e)
        try:
            print(json.dumps({"ok": False, "error": str(e), "requeue": [int(os.environ.get("WK_FIRST_TASK", 0))]}))
        except Exception:
            pass
        sys.exit(1)
