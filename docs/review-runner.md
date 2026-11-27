# Review runner (build/review split, part 1)

Agents that build should never wait for test suites: tests cost no tokens, only machine time. The build/review split
separates the two. A builder hands over *branch + commit + check commands* and is free at once; an **AI-free review
runner** executes the checks; a reviewer then decides **green** (merge, release) or **rework** (back to the builder).

This page covers part 1, which is in the app now:

- the **review runner**: a service inside the web app that runs the check commands of a handed-over commit in
  throwaway containers, with limits per target, a green cache per commit and clear job states;
- the **finding format**: how a reviewer records the decision (file, line, photo, expected, measured, severity).

Not yet part of the app (part 2): reviewer slots in the worker pool, starting the reviewer automatically when the runner
has finished, sending rework back to the *same* builder session with priority, and a flow API for dashboards. Until then a
signed-in admin or a coordinator token records the finding through the API.

The runner is **off by default**. Nothing changes for an install that does not switch it on.

## Flow

```
builder (task) ── hand-off: commit + check commands ──► review runner (no AI, containers)
                                                         │  prepare: extract exactly this commit
                                                         │  step 1, step 2, ... (one container each)
                                                         ▼
                                              waiting for review ──► finding: green | rework
```

## Switch: off, probe, on

| Mode | Hand-off | Runner |
|---|---|---|
| `off` (default) | refused (409) | finishes jobs that are still open, starts nothing new from new hand-offs |
| `probe` | accepted while fewer than `probe_max` jobs are open (default 2) | runs them |
| `on` | always accepted | runs them |

- Default from `AIWERKSTATT_REVIEW_MODE` (`off` when unset or invalid).
- An admin switches it in the running app: `POST /api/review/runner/mode` with `{"mode": "probe", "probe_max": 2,
  "reason": "..."}` (signed-in admins only, never a token). The setting is stored in `data/review-runner.json` (a broken
  file means the environment default); every change is one JSON line in `data/review/mode.jsonl` (who, when, from, to).
- Emergency stop: while the file `data/review/OFF` exists, the runner starts no new step; running steps finish.

## Hand-off

`POST /api/review/jobs` (signed-in admin or coordinator token):

```json
{"thread_id": 42,
 "commit": "<full commit hash, git rev-parse HEAD>",
 "branch": "feature/queue",
 "checks": ["python3 -m unittest discover -s backend/tests",
            {"cmd": "cd frontend && npm ci && npm run build", "target": "online"}],
 "hint": "the queue view changed", "release": "train"}
```

- The source is the project's workspace (the workspace of the task's project must be a git repository that contains the
  commit). Only **tracked files of exactly this commit** are checked; untracked files such as `.env` never reach a check.
- 1 to 20 check commands, each up to 2000 characters, run with `sh -c` in the order given. All steps run, also after a red
  one, so the reviewer sees the full picture. Steps share the job's source folder: a later step sees what an earlier one built.
- One job per task and commit: the same commit again (also concurrently) returns the existing job (HTTP 200) while it is open
  or green. A new commit, or the same commit after rework or cancel, is the next **round** (`j<task>-r<round>`). An older open
  job of the same task is cancelled as superseded, so the runner never spends time on a stale commit.
- Answer: `201` with the new job, `200` with the existing one, `400` for a bad request, `404` for an unknown task, `409` when the
  runner is off or the probe limit is reached.

Other routes (same access): `GET /api/review/runner` (status: mode, targets with limit and busy count, running steps, counts per
phase, cache size), `GET /api/review/jobs[?open=1][&thread_id=n]`, `GET /api/review/jobs/<job>` (steps with the end of each log),
`GET /api/review/jobs/<job>/steps/<n>/log` (last 64 KB as text; step 0 is the prepare step), `POST /api/review/jobs/<job>/cancel`
(`{"reason": "..."}`), `POST /api/review/jobs/<job>/finding` (below). Writes of a coordinator token count against its hourly write limit.

## How a job runs

Everything runs in containers that the web app starts through the Docker socket proxy, like agent runs:

1. **Prepare.** A fresh named volume `aiwerkstatt-review-<job>` is created. A short container mounts the workspaces volume
   **read-only** and the job volume, and runs `git archive <commit>` into the job volume. It has no network. git never runs
   inside the web container, so repository configuration written by an agent (filters, hooks) can never execute next to the
   provider credentials. An unknown commit makes every step red with the reason "source not available".
2. **Steps.** Each step is its own container on the job volume only (never the workspaces volume): uid 10001, all capabilities
   dropped, `no-new-privileges`, the run's PID and memory limits, **no provider credentials, no Docker access**, network only if
   its target allows it. Environment: `CI=1`, `HOME=/tmp`, `AIWERKSTATT_REVIEW_JOB`, `AIWERKSTATT_REVIEW_STEP`,
   `AIWERKSTATT_REVIEW_COMMIT`. Output goes to `data/review/logs/<job>/step-<n>.log` (capped at 5 MB).
3. **Emergency stop per step.** No output for 45 minutes (`AIWERKSTATT_REVIEW_SILENCE_MIN`) or longer than 6 hours
   (`AIWERKSTATT_REVIEW_MAX_HOURS`): the container is killed and the step is red with the reason. The prepare step is capped at
   15 minutes. These catch hangs and outliers; they are not deadlines for normal work.
4. **End.** When all steps ran, the job waits for review; containers and the job volume are removed.

After a restart of the web app, steps that were running are not trusted: they wait again and the source is prepared anew.

### Targets and limits

Each step names a **target**; the runner starts a step only while fewer steps (including prepare steps) of that target run
than its limit. There is no total limit across targets. Defaults:

| Target | Limit | Network |
|---|---|---|
| `local` (default) | 1 | none (unit tests, linters, builds with vendored dependencies) |
| `online` | 1 | bridge (checks that must fetch dependencies) |

`AIWERKSTATT_REVIEW_TARGETS` replaces them as a whole (JSON object, 1-16 targets): name (`a-z`, digits, `_`, `-`), `limit` 1-16,
`network` `none|bridge`, optional `cpus` (0.1-64), `memory` (e.g. `3g`) and `image`. An invalid value keeps the defaults and is
shown as `targets_error` in the status. Rework rounds (round 2 and later) start before first rounds; otherwise first handed over,
first run.

### Images

Steps run in the agent-runner image by default (Node.js, Python, git: the toolchain the builder had), or in
`AIWERKSTATT_REVIEW_IMAGE`, or in the image of the step's target. The prepare step always uses the default image and needs `git`,
`sh` and `tar`. The job volume is mounted on `AIWERKSTATT_REVIEW_WORKDIR` (default `/workspace`): a fresh named volume takes
over owner and mode of that folder in the image, so the image must have it writable for uid 10001 (the runner image has).
`AIWERKSTATT_REVIEW_LABELS` (`key=value,...`) adds labels to every review container, e.g. for a host's cleanup tooling; every
review container and volume carries `aiwerkstatt.review=<job>`.

### Green cache

A green step is remembered per (project, commit, command, target, target image) for 7 days (`AIWERKSTATT_REVIEW_CACHE_DAYS`,
0 = off) in `data/review/green-cache.json`. A later job with the same commit and command takes it over without a container
(`from_cache: true`); a job whose steps all come from the cache needs no prepare step at all. Red is never cached.

## Job phases

| Phase | Meaning |
|---|---|
| `waiting_runner` | handed over, the runner has not started it |
| `running` | prepare or a step is running (or waits for a free place of its target) |
| `waiting_review` | all steps ran (`run.green` says whether all are green) |
| `in_review` | a reviewer took it (part 2) |
| `green` | the reviewer decided green |
| `rework` | the reviewer decided rework |
| `escalated` | rework, and the round limit is reached (part 2) |
| `cancelled` | cancelled or superseded by a newer round |

Files below `data/review/jobs/`: `<job>.json` (hand-off, written once), `<job>.run.json` (runner state), `<job>.finding.json`
(decision).

## Finding format

`POST /api/review/jobs/<job>/finding`, accepted once the runner finished (`waiting_review` or `in_review`, otherwise 409):

```json
{"decision": "rework",
 "summary": "The queue buttons are cut off on phones.",
 "layout": "LAYOUT: checked 390/1512 light/dark, findings 1",
 "findings": [{"file": "frontend/src/Pool.tsx", "line": 212, "photo": "shots/queue-390.png",
               "expected": "all four buttons fully visible at 390 px",
               "measured": "right edge of the last button at 402 px",
               "severity": "high"}]}
```

- `decision`: `green` or `rework`. Rework needs at least one finding.
- Per finding: `expected` is required; `file`, `line` (a number, 1 or more), `photo` (path or URL of a screenshot) and `measured`
  are optional; `severity` is `critical`, `high`, `medium` (default) or `low`. At most 50 findings; long texts are cut.
- **Green although a step is not green** needs `override_reason` (at least 15 characters), for example a flaky test that passed on
  a rerun. The runner measures; the reviewer decides, and says why.
- The stored finding carries `by`, `time`, `job` and `commit`. `review_jobs.rework_text()` renders it as plain text for the builder.

## Differences to the private original

| Private original | Here |
|---|---|
| host service as root, commands run as a system user with `nice` | containers through the socket proxy, unprivileged, no credentials |
| check in the builder's work tree or a detached git worktree on the host | `git archive` of the commit into a fresh volume, in a sandboxed prepare container |
| fixed places (second machine, this machine, release gate) with their limits | named targets with limits and network policy, configurable |
| switch file written by the app, plus places per department | `off/probe/on` switch with probe limit; slots per department follow in part 2 |
| reviewer agent and rework hand-back built in | part 2 |

Tests: `backend/tests/test_review_runner.py` (hand-off, phases, targets, cache, emergency stop, restart, finding format, API,
container settings).
