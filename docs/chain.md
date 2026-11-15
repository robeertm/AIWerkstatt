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
  "limits": {"per_day": 4, "per_track": 1, "total": 2},
  "external": {"key": "what a human or another system has to provide"}
}
```

- `depends_on` names tasks or `ext:<key>` conditions from `external`. A dependency counts as
  met when its task finished green and all human steps it reported are confirmed.
- `order` is the start priority. `check` is appended to the prompt as a self-check.
- `release: true` adds a human step `release:<id>`. The chain never tags or releases.
- `optional: true` tasks start only after `approve <id>`.
- `limits` can only lower the fixed ceilings (4 new tasks per day, 1 running per track, 2
  running in total).
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
| `check` | self-healing, run it from a timer |
| `simulate` | walk the whole plan on a fake clock without API or state |

Create the file `<root>/KILL` to stop everything at once: no command changes anything while
it exists (exit code 3). Exit code 2 means an error.

## Self-healing

Run `chain.py check` every few minutes from a timer. It picks up result files that appeared,
sends exactly one follow-up to a task that finished without a result file, and exactly one to
a task whose thread shows no activity for more than twice its `hours`. A failed or stopped
task is shown as a hint for a human. Then it starts whatever is ready.

## Tests

`python3 -m unittest discover -s scripts/chain` (fake API and clock, includes a simulation of
the example plan).
