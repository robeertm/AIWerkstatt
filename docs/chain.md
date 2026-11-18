# Task chain

`scripts/chain/chain.py` files the next task of a plan after each finished one, through the
AIWerkstatt HTTP API. It uses a service token of the `chain` profile
(see [security](security.md#task-chain)), needs only Python 3 (standard library) and runs with
`python3 -I`. Nothing in it is specific to one host.

## Setup

1. Create a collecting project in AIWerkstatt and note its id.
2. Create the token: `python3 service_token.py create chain-bot --profile chain`, and keep it
   in a file readable only by you (mode 0600).
3. Configure the web service: `AIWERKSTATT_CHAIN_PROJECT=<project id>`,
   `AIWERKSTATT_CHAIN_TITLE_PREFIX="[chain] "`. Optional: `AIWERKSTATT_CHAIN_RATE`,
   `AIWERKSTATT_CHAIN_REVIEW=off` (default: the review gate stays on).
4. Write a plan (see `scripts/chain/example-plan.json`), then try it: `chain.py --plan plan.json simulate`.

Paths and settings come from options or the environment: `--root` /
`AIWERKSTATT_CHAIN_ROOT` (default `<data>/chain`), `--plan` / `AIWERKSTATT_CHAIN_PLAN`
(default `<root>/plan.json`), `--token-file` / `AIWERKSTATT_CHAIN_TOKEN_FILE` (default
`<root>/token`), `--url` / `AIWERKSTATT_CHAIN_URL`, `--project` / `AIWERKSTATT_CHAIN_PROJECT`,
`--prefix` / `AIWERKSTATT_CHAIN_TITLE_PREFIX`.

## Plan

```json
{
  "tasks": [{"id": "api-schema", "track": "api", "title": "…", "model": "claude-sonnet-5-5",
             "effort": "medium", "depends_on": ["other-id", "ext:key"], "prompt": "…",
             "check": "…", "release": false, "optional": false, "hours": 1}],
  "order": ["api-schema"],
  "limits": {"per_day": 4, "per_track": 1},
  "external": {"key": "what a human or another system has to provide"},
  "extern_release": {"key": {"repo": "owner/lib", "version": "2.4.0"}},
  "release": {"repo": "owner/app", "image": "ghcr.io/owner/app", "queue_dir": "/path/to/release-queue",
              "clone": "/path/to/app-clone"},
  "human_reasons": [{"pattern": "hardware token", "reason": "the token is plugged in at one machine"}]
}
```

- `depends_on` names tasks or `ext:<key>` conditions from `external`. A dependency counts as
  met when its task finished green and all human steps it reported are confirmed.
- `order` is the start priority. `check` is appended to the prompt as a self-check.
- `release: true` marks a release task. With a `release` block the chain releases it itself (see
  [Gate green = release](#gate-green--release)); without one the step `release:<id>` goes to the
  coordinator. The chain never creates a tag.
- `optional: true` tasks start only after `approve <id>`.
- `limits` can only lower the fixed ceilings (4 new tasks per day, 1 running per track). There is
  no total ceiling (0.24.0): every ready task is created at once and waits in the normal FIFO
  queue of AIWerkstatt, whose slots regulate the load. One per track is a dependency, not a load
  lock; `status` shows such a task as "waiting for the previous task in track X". An old
  `"total"` entry is accepted without effect.
- `hours` is the expected duration; a task without progress for twice that long is stalled.

## Result file

Every task prompt ends with the path of a result file the agent writes
(`<root>/results/<id>.json`):

```json
{"id": "api-schema", "branch": "work/api-schema", "commit": "abc1234",
 "tests": "green", "gate": "green", "manual_steps": ["Confirm the new setting"]}
```

Only these fields are accepted. `tests` is `green` or `red`, `gate` is `green`, `open` or
`red`. Green plus green completes the task. `gate: open` waits without counting a failure.
Red sends one follow-up per attempt; the third red in a row blocks the task and shows a hint
in the status. Each entry of `manual_steps` becomes a pending human step `<id>:<n>`.

## Commands

| Command | Effect |
| --- | --- |
| `next [--done <id> --result <file>]` | record a result, then start every task that is ready |
| `status [--json]` | tasks, pending human steps and conditions, hints; `status.json` is also written atomically in the root |
| `pause` / `resume` | soft pause: results are still recorded, nothing new starts |
| `manual-done <step\|key\|id>` | confirm a human step, an external condition, or all steps of a task |
| `approve <id>` | allow an optional task |
| `release --id <id> --gate-log <name>` | verify a gate log, then file a release request with the broker |
| `verify [<version>]` | measure a release: tag, GitHub release, image digest, `latest`; without a version all open requests |
| `stand [--json]` | release stand: version on the branch against the newest tag, how long it has been unpublished |
| `proof <version> <file> <pattern>` | acceptance: exit 0 only if the pattern is in the file at the published tag `v<version>` |
| `check` | self-healing, run it from a timer (also measures open requests and external releases) |
| `simulate` | walk the whole plan on a fake clock without API or state |

Create the file `<root>/KILL` to stop everything at once: no command changes anything while
it exists (exit code 3). Create `<root>/RELEASE_OFF` to stop only releases: `release` then exits
with 3 and records the release as held, while building goes on. The hard `KILL` stops releases
too. Exit code 2 means an error.

## Gate green = release

A person at the main workstation only coordinates; the chain does the rest. After a green gate
run the build agent calls `release --id <id> --gate-log <name>`. The chain checks, fail closed:

- the log name is a bare file name, the file lies in the configured gate log folder, is a regular
  file (no symlink), at most 2 MB and at most 7 days old (`max_age_hours` can only lower this; the real binding is the commit check below);
- it contains the line `GATE: GREEN`, no `GATE: RED`, and exactly one distinct line
  `SUMMARY (version V, commit C, image I)`, where `I` is the published `<image>:V`;
- `V` and `C` match `origin/<branch>` of the clone (version read from `version_file`);
- the task is a release task, there is no pending request, and the version is not already out.

Only then it writes `{"mode": "release", "by": "<identity>", "gate": "green", "gate_log": "<name>", "commit": "<full sha>"}`
atomically to `<queue_dir>/<slug>.req`, the drop box of the release broker. The broker runs its
own checks and is the only one that creates tags and releases. `verify` (and `check`) measure the
outcome anonymously and read-only: tag, GitHub release (not a draft), image digest and whether
`latest` points to it. A refusal in the broker's status file (`broker_status`, JSON with
`version`, `state`, `ts`) turns the release into `failed` and raises an alert in the status.

The `release` block: `repo` (owner/name), `queue_dir` and `clone` are required; `image`
(default `ghcr.io/<repo>` in lower case), `gate_logs` (default `<root>/gate-logs`), `broker_status`,
`branch` (`main`), `version_file` (`VERSION`), `identity` (`chain`), `slug` (repo name) and
`max_age_hours` are optional. All paths must be absolute. Without a block the chain never releases.

An external condition that is a release (`extern_release`, the key must also be in `external`)
resolves itself once tag, release and image are provably out; the chain looks at most every
10 minutes. `manual-done <key>` stays available as an emergency.

## One commit per check (release train)

A check result is valid for exactly one commit. The request carries that commit (`commit`, full SHA) and a broker
should tag exactly it and verify that it is on the branch; it must not tag the branch head, which keeps moving while a
gate runs. Practical rules for a plan with a `release` block:

- Run one release gate at a time per repository, against a frozen commit; pushes that arrive meanwhile go out with the
  next run. Other tasks of the plan run no release gate of their own and put changelog entries under an `Unreleased`
  heading (their prompt says so); the release task assigns the version.
- **Release stand**: `stand` (and every `check`) compares `origin/<branch>` with the newest `vX.Y.Z` tag. Unpublished for
  `stale_after_h` (default 2) hours the level is `stale` and `check` prints a hint; after `alert_after_h` (default 6) it
  is `critical` and the status carries one alert (`alerts.stand`), removed when the release is out. `status.json` has
  the block `release_stand` (`level`, `main_version`, `published`, `age_h`, `unreleased_entries`, `line`). An unreadable
  clone yields `unknown`, never `ok`.
- A release task with a green gate but no published release shows the state `waiting_for_release` in `status`: built is
  not finished.
- Before anyone is told "fixed in <version>", run `proof <version> <file> <pattern>`: the evidence has to be in the
  published tag, not only on the branch.

The app's own GitHub release (`POST /api/projects/<id>/release`) takes the same optional `commit` and creates the tag on
it (`target_commitish`).

## Steps from results

Steps in `manual_steps` are sorted when a result is recorded:

- **waiting for the main workstation** only with a reason: signing keys, passkeys or
  biometrics, the real desktop browser (extend with `human_reasons`);
- **chain**: the release of a release task, done by the chain, resolved when a release is out;
- notes and documentation steps travel through the inbox and are only logged (`taken_over`);
- everything else is a **coordinator** decision.

`status` lists the three open lists separately (`waiting_for_human`, `waiting_for_coordinator`,
`chain_open`) next to `releases`, `alerts`, `external_auto` and `release_off`; `manual_pending`
stays with `kind` and `reason`. Entries from older state files are sorted on loading. The result
file may carry `"release": "published" | "requested" | "held" | "none"`.

## Self-healing

Run `chain.py check` every few minutes from a timer. It picks up result files that appeared,
sends exactly one follow-up to a task that finished without a result file, and exactly one to
a task whose thread shows no activity for more than twice its `hours`. A failed or stopped
task is shown as a hint for a human. Then it starts whatever is ready.

## Tests

`python3 -m unittest discover -s scripts/chain` (fake API and clock, includes a simulation of
the example plan).
