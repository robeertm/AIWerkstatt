# Stall guard

A run that is stuck but not silent in the way the old watchdog expects can occupy a slot for hours: for example an agent that
waits in a polling loop (`ps | grep <gate>; sleep 30`) for a process that belongs to **another** run. The loop keeps starting new
tool calls, so the output-silence watchdog never fires, while nothing really happens. The stall guard measures real activity on
several axes and heals such a run without asking anyone. It is **on by default**; `WK_STALL_GUARD=0` turns it off (the orchestrator passes the setting to every run).

## What is measured

Once a minute, only while a turn is open (an answer is pending). Waiting for background work and API-error pauses have their own
rules; the clock does not run there.

| Axis | Active when ... |
|---|---|
| output | the agent (or a subagent) streams output: API replies, tool results |
| cpu | the tool processes under the agent use together at least 2 % of one core (the provider CLI itself is excluded) |
| io | a tool process reads/writes at least 16 KiB of characters or 256 KiB of real disk I/O per interval (ssh to a remote runner, `tail -f`) |
| files | a file named on a tool command line, or an output file of a background task of this session, grows or changes |
| git | `HEAD`, index, reflog or `ORIG_HEAD` of the working directory change |
| gate | a progress file of this run (`aiwerkstatt-progress/1`, see `backend/eta.py`; folder `AIW_STALL_GUARD_PROGRESS_DIR` or `AIWERKSTATT_PROGRESS_DIR`) is not ended and its heartbeat is younger than 3 minutes |

A polling loop costs about 0.3 % of a core and moves no axis: it is silence. A long gate run with a heartbeat, a growing log and
git activity are never a stall. The module only looks at the process tree and files, so it works with every provider adapter.

## Stages

Silence on **all** axes, thresholds `WK_STALL_GUARD_STAGES` (seconds, default `900,1500,2400`):

| Stage | Effect |
|---|---|
| 1 (15 min) | a message into the session: check the real state instead of waiting in a loop |
| 2 (25 min) | the hung tool processes are ended (SIGTERM, after 3 s SIGKILL) and the agent is asked to check the state and continue |
| 3 (40 min) | the run is ended cleanly and reported as `hung` |

Each stage and each healing ("activity again after N min, healed after stage 2/3") is an event in the task history; the pool
overview shows the guard state per run.

**Never ended:** the provider CLI itself, and release, gate and deploy scripts (`release_check.py`, `security_gate.py`,
`branch_check.py`, `chain.py`, `projectplan.py`, `remote_test.py`, `remote_test_receiver.py`, `check_commit_identity.py`,
`self-update.sh`, `install.sh`, `release.sh`, `deploy.sh`) together with their whole tool subtree - ending the `tail` at the end of a
pipe would take the gate down with it. They are recognized by program or script name, never by a word inside a shell line, so a
`grep release_check` inside a polling loop does not protect the loop. While a protected step is alive, stage 3 waits.

With the guard on, the plain output-silence watchdog (`WK_STALL_TIMEOUT`) is only a backstop (stage 3 threshold plus one hour) and
never ends a run on which the guard sees activity.

## Re-queue at the front

The runner reports stage 3 as an event of type `hung` (with a short context: last action, branch, last commit, number of
uncommitted files) and ends the run **without** a `failed` event and without a retry request, so the normal self-healing retry does
not queue the task a second time. The engine puts the task back under the **same task id**. The queue is strictly ordered by task
id, so the task sorts in front of everything newer - that is the "front of the queue" here; no extra queue interface is needed.

- First hang: the same session continues (resume) with the context hint and the request.
- Second hang: a fresh session with the original request plus the context hint.
- After `AIWERKSTATT_STALL_GUARD_MAX` (default 2) hangs of one task the old path takes over: the task is marked failed without a final
  error and the self-healing retry with its growing pause picks it up. A stall-guard re-queue does not count as an attempt.

## Remaining time

A task far over its estimate shows "overdue since HH:MM" instead of a frozen figure (`eta.overdue_s`):

- experience estimate: runtime > max(1.5 x the experience value, experience value + 15 min);
- step estimate: all steps done and the state unchanged for more than 15 minutes, or the current step stands longer than
  max(3 x the task's pace per step, 30 minutes) (45 minutes without a pace);
- a running gate or test run: never (it estimates itself and its heartbeat shows progress).

The runner keeps `todo.stand_since` (since when the counter and the current step are unchanged). The pool overview adds
`eta_overdue_since` and `stall_guard` per run; both are additive. An overdue run counts as free in ten minutes at the earliest.

## Differences to the private original

The measuring logic and the stages are the same. Here the runner is the direct parent of the provider CLI inside one container, so
the process tree is simply the container's; there are no host-level queues to watch, so the original's "queue" sub-axes are
replaced by the generic gate-progress axis. The front re-queue relies on the strict FIFO-by-id order instead of a separate priority
interface (see the parity inventory).
