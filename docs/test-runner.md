# Remote test runner

`scripts/test-runner/` runs a heavy test suite on a **second machine** and shows that machine's load in the
admin overview. It is optional, needs only Python 3 (standard library) and Docker on the test machine, and does
nothing unless you configure it. Nothing in it is specific to one host or vendor.

| File | Runs on | Purpose |
|---|---|---|
| `remote_test.py` | client | packs a commit (or the work tree with `--worktree`), sends it, queues when both slots are taken, shows the output, returns the exit code; `--status`, `--available`, `--images`, `--meta`, `--collect` |
| `remote_test_receiver.py` | test machine | the forced command: `status`, `run`, `result`, `images` (Python 3.8+) |
| `profiles.example.json` | test machine | allow list of profiles, modes, base images and limits |
| `install.sh` | client | sets up the key, the receiver and the profiles over SSH, then proves the key is restricted |
| `result_store.py` | client | per-test result store: plan, ingest, flaky, report, full runs (used by `--only-needed`) |
| `remote-test-collector.service` | client | example unit for the load collector |

## Security model

The trust boundary is one SSH key that can only start the receiver (details in
[security](security.md#remote-test-runner)):

- `authorized_keys` line: `restrict,from="<client address>",command="/usr/bin/python3 -I <dir>/remote_test_receiver.py" <public key>`.
  `restrict` disables forwarding and the terminal, `from` pins the source address, `command` ignores whatever the
  caller asks for; the receiver reads the request from `SSH_ORIGINAL_COMMAND` and accepts four subcommands only (`status`, `run`, `result`, and the read-only `images`).
- `run` works only for profiles and modes in `profiles.json` (owned by root or the runner user, not writable by
  others). Arguments are checked against a deny list (`--junitxml`, `--basetemp`, `--rootdir`, `-c`, `-o`, ... and
  absolute or `..` paths), environment variables only from the profile's allow list.
- The uploaded tar may contain only `source/` (and `extra/` if the mode allows it): no absolute paths, `..`, hard
  links, links pointing outside, entries below links or special files.
- The test container runs with `--rm --name probe-<repo>-<time> --network none --cap-drop ALL
  --security-opt no-new-privileges --no-healthcheck --memory <limit> --cpu-shares <low>`, a CPU limit, a read-only
  root file system (per mode, with a writable `/tmp`) and the source mounted read-only. It never gets the Docker
  socket. Only tracked files of the commit are packed, so untracked secrets stay on the client.

## Profiles

`profiles.json` has three parts (copy `profiles.example.json`):

```json
{
  "settings": {"max_runs": 2, "memory": "4g", "cpu_mode": "cpus", "cpus": "2", "cpusets": ["0-1", "2-3"],
               "cpu_shares": 256, "shm_size": "512m", "max_run_seconds": 14400, "docker": ["docker"]},
  "base_images": {"python312": {"dockerfile": ["FROM python:3.12-slim-bookworm", "COPY requirements.txt pins.txt /base/", "RUN pip install ..."]}},
  "profiles": {"name": {"build": "repo|base", "modes": {"mode": {"command": ["python", "-m", "pytest", "..."]}}, "env_allowed": {}}}
}
```

- `build: "repo"` builds the image from the Dockerfile of the package (like an update gate); the source is in the image
  and nothing is mounted.
- `build: "base"` builds a test image from a fixed Dockerfile template of `base_images`, the package's
  `requirements.txt` and the profile's `pins` (the client may send other pins in the same `package==version` form).
  The image is cached per content hash (the 2 newest states are kept); the package is mounted read-only at `/source`
  (and `extra/` at `/extra` for modes with `"extra": true`).
  Order the template by how often things change: system packages and browsers depend only on `pins.txt`
  (`COPY pins.txt` + install first), `COPY requirements.txt` + install last. A changed requirements file then
  rebuilds only the last layer; browser installs on a slow machine can otherwise take an hour per change.
- Mode keys: `command`, `targets` (default pytest targets), `targets_required` (default false; when true a request needs at
  least one test target, for example `tests/test_x.py[::test_y]` - for modes meant to run a few chosen tests, never a whole
  suite by accident), `read_only` (default true), `env` (fixed variables), `workdir`, `junit` (default true for pytest) and
  `extra`.
- **CPU limit:** `cpu_mode: "cpus"` uses `--cpus` (the kernel needs CFS quota; image builds get the equivalent
  `--cpu-quota`). `cpu_mode: "cpuset"` uses `--cpuset-cpus` with one set per slot from `cpusets`, for kernels
  without quota support. `max_runs` is capped at 2 by the receiver.
- `docker` is the command used to reach Docker (for example `["sudo", "-n", "/usr/bin/docker"]`).

## Throttle and queue rules (0.24.0: no wait locks, a queue instead)

1. A free slot means the run starts at once - also when the runner is loaded (high CPU, load or pressure). Throttling
   happens only on the test machine: a low cgroup weight (`cpu_shares`) and the CPU limit of `profiles.json` (`--cpus` or
   a cpuset). Production services keep priority; the test gets slower but never waits for load.
2. The receiver allows 2 runs at once (one lock file per slot, checked immediately); a third request gets `busy`. When
   both slots are taken the client joins a FIFO queue on its own machine (`<STATUS_DIR>/queue/` or
   `AIWERKSTATT_TEST_RUNNER_QUEUE`, one file per waiting run, named by arrival time, holding PID and process start time)
   and prints `queue position N, ahead of you: <run>, expected free in ~X min` (estimate = median duration of the own
   protocols per repo/profile/mode). First come, first served inside a class: no deadline, no abort; a run that already
   holds a slot over there does not count as waiting. Entries whose process is gone (or whose PID was reused) are removed
   by the next reader. If several users share the queue, make the folder group-writable without sticky bit so anyone can
   remove orphans. If the receiver still answers `busy` (a run from an older client without the queue came first), the run
   keeps its queue position. `--wait-max-min` is still accepted but has no effect.
   **Queue classes (0.46.0):** a run is `release` (a release gate: `--queue-class release` or
   `AIWERKSTATT_TEST_RUNNER_CLASS=release`) or `task` (a probe run of a working task, the default). Waiting `release` runs go
   before waiting `task` runs, first come, first served inside a class. A `task` run that has waited longer than 45 minutes
   (`AIWERKSTATT_TEST_RUNNER_QUEUE_AGE_S`) ranks with the releases by arrival, so a stream of releases never starves it. A
   run that already holds a slot is never displaced; nothing is aborted and nothing gets a deadline - only the order of the
   waiting entries changes. The collector shows the class per waiting run (`queue.waiting[].class`, additive).
3. A running test is never aborted, also not when the connection drops (the receiver ignores SIGHUP and SIGPIPE). The
   client fetches the result with `result <id>` until it is there (results stay for 2 days). The only limit is the
   emergency brake `max_run_seconds`.
4. A protocol per run is written on both sides: duration, build and test time, load and CPU samples of the runner and
   the unhealthy containers before, during and after (own `probe-*` containers excluded).

## Exit codes

- The exit code of the test run (pytest: 0 green, 1 red, 2 collection error, ...). Output goes to stdout unchanged,
  build and info lines to stderr.
- **100 = runner unavailable:** switched off, not configured, unreachable, request refused (for example an argument
  outside the allow list), 3 status requests in a row unanswered while waiting, result lost, or a fault of the runner
  instead of the code - never because of waiting time. The caller
  must then **run the tests locally**; the tool never decides the result of a test it did not run.
- 2 = wrong use of `remote_test.py` itself.

## Setup

1. On the test machine: Docker and Python 3.8+, an account that may run Docker (or passwordless `sudo` for it).
2. Copy `profiles.example.json` to `profiles.json` and edit it.
3. From the client: `RTR_TARGET=user@host RTR_FROM=<client address as the test machine sees it> bash install.sh`.
   It creates a dedicated key, installs the receiver, `profiles.json` and the one restricted `authorized_keys` line
   (a backup of the file is made first), and probes that `status` works while `id` does not.
4. Run: `AIWERKSTATT_TEST_RUNNER_CONFIG=<config.json> python3 remote_test.py <repo> --profile example-app --mode image -n 2 -- tests/test_x.py`.
   The config is `{"target": "user@host", "key": "/path/to/key"}`; the variables `AIWERKSTATT_TEST_RUNNER_TARGET` and
   `AIWERKSTATT_TEST_RUNNER_KEY` override it. There are no default hosts. Further settings: `..._LOGS` (protocols),
   `..._STATUS_DIR` (collector output), `..._OFF=1` (switch off, exit 100), `..._SLOT` and `..._REF` (numbers shown in the
   runner's list of runs).
5. Load display: run `remote_test.py --collect` as a service (see `remote-test-collector.service`).

## Status in AIWerkstatt

Set `AIWERKSTATT_TEST_RUNNER_STATUS` for the web service to the collector's `status.json`. `GET /api/pool` (admins
only, like the rest of the route) then carries an additive field:

```json
"test_runner": {"fresh": true, "stand": 1790000000, "load": [3.4, 2.0, 1.0], "cores": 8, "psi_cpu": null,
                "psi_io": null, "ram_free_gb": 5.5, "cpu_percent": 37.0, "test_runs": 1, "max_runs": 2,
                "queue_waiting": 0}
```

`fresh` is true while the last successful sample is younger than 120 seconds, `stand` is its unix time. Without the
variable, or if the file is missing or unreadable, the field is absent. The "Agents at work" card shows one line, for
example "Test runner: 37 % CPU · load 3.4 / 8 cores · 1 of 2 runs" (plus "N waiting in the queue" while runs wait), and a "stale" chip when the sample is not fresh.
Run details and ids stay out of the field.

## Remaining time (0.25.0)

`remote_test.py` writes a progress file per run (phases `queue` -> `test`; while queued with the expected wait from the
estimate above) into the folder `AIWERKSTATT_PROGRESS_DIR` (default `~/.local/state/aiwerkstatt/progress`; point the web
service at the same folder). At the end one line with the measured phase durations goes to `history-<user>.jsonl`; the
moving median of the last 10 runs of the same kind feeds the estimate (`backend/eta.py`). A run started by another
script that writes its own progress file sets `AIWERKSTATT_PROGRESS_PARENT=1`, then `remote_test.py` writes none.
`AIWERKSTATT_PROGRESS_THREAD` (a number) links a run to the task thread it belongs to. If `eta.py` is missing or the
folder is not writable, everything runs as before.

`GET /api/pool` carries these additive fields (nothing existing changes; errors never break the view):

- per busy slot: `eta_s`, `eta_text` ("done in ~25 min (15-40 min)" or "unknown (first task of this kind)"), `eta_spanne`
  (`[low, high]` seconds), `phase`, `run` (phase `i/n`, tests done/total, `after`, reliability), `eta_line`;
- `waiting_list`: per waiting order `project`, `start_in_s`, `start_text` ("starts in ~35 min"; FIFO onto the slots that
  free up first);
- `eta_runs`: all active progress runs, also those without a slot.

The "Agents at work" card shows the remaining time in bold per slot, below it `phase · phase i/n · tests · then ...`, a slim
progress bar and "starts in ~X" per waiting order; without the fields it shows nothing extra. Format: "~12 min",
"~2 h 10 min", "~1 day 4 h", from 48 h "~3 days"; without any history "unknown (first run of this kind)".

## Result store: green stays green (0.30.0)

`result_store.py` keeps one result per test in a small SQLite file (standard library only) so a repeated or resumed run
only executes what can have changed. A result is stored with the project, the environment (for example `profile/mode`),
the test id, the commit, the outcome, the duration, the run id and the kind of attempt (`parallel` or `serial`). Storage:
`$AIWERKSTATT_RESULT_STORE_DIR`, default `~/.local/share/aiwerkstatt-results/<project>.sqlite`.

**How it decides.** A green result is reused only if nothing it depends on changed between its commit and the current one:

- same commit: valid;
- only files below the tests folder changed: valid, except the changed test files and the tests that import a changed
  test helper (a changed `conftest.py` invalidates nobody);
- app-side files changed: the affected test files run again. With `--selector "<command>"` (or
  `AIWERKSTATT_RESULT_STORE_SELECTOR`) the command gets the changed app files as arguments and prints the affected test
  files, one per line, or the word `FULL`. Without a selector the default rule follows Python imports transitively; any
  non-Python app file means everything runs again;
- results older than 7 days, red, aborted and never-run tests always run. Environments are never mixed.

The test ids of a file are learned from the last run that executed the whole file, so partial collections do not count.
Every result is committed the moment it is seen, so an aborted run loses nothing; the next plan deselects the tests that are
already green on the current commit (`--deselect`).

**Flaky tests.** A test that is red in the run and green in the serial retry is stored green and gets a row in the `flaky`
table. Flakiness is always visible, never silent.

```bash
python3 result_store.py ingest --project app --env image/full --commit <sha> --run r1 --junit report.xml --repo . --whole
python3 result_store.py plan   --project app --env image/full --repo . --json [--selector "python3 pick_tests.py"]
python3 result_store.py status --project app --red
python3 result_store.py flaky  --project app --hours 24
python3 result_store.py report --hours 24      # JSON for a nightly summary
```

**Opt-in in the client.** `remote_test.py <repo> --profile P --mode M --only-needed [--selector CMD] [--tests-dir DIR]`
plans through the store for a full-suite request (no arguments after `--`; otherwise it runs as usual), sends only the
needed targets plus `-vv` and the `--deselect` arguments, records results from the streamed `-v` lines and the JUnit report,
retries red tests serially (`-n 1`) up to twice and prints a pytest-style summary such as
`=== 2 passed (1 reused, 1 new), 1 flaky (red, then green serially) in 12.0s ===`. Default is off; existing behaviour is
unchanged.

**Limits.** The dependency rule is conservative and static: dynamic imports, data files read by tests and external services
are not seen, so use a project selector (or leave the 7-day expiry) where that matters. A changed environment variable or
base image is not a code change; put such inputs into the environment name. Targets collapse to files when more than 380
would be sent.

## Image environment and full-run rule (0.32.0)

A green result is only as good as the environment it ran in. The code of a test run is decided by the commit and the
dependency rule above; the rest is the **image environment**: the base image and the dependency layers that were built before
the source code was copied in. If a base image is updated or a dependency layer is rebuilt, green results of the old
environment must not count.

**What the receiver reports.** After every build it sends a control line `{"kind": "image", "id": <image id>, "fp": <fingerprint>,
"fp_type": ..., "base": ..., "base_id": ..., "layers": N, "env_layers": M}` and puts the same object into the end record
(the field is `fp_type`, not `kind`, because `kind` is the control line type). The environment is the base image (the last
`FROM`) plus all layers before the first copy of a directory (`COPY dir:` / `ADD dir:` / `multi:` in `docker history`, i.e. the
source code). `fp_type` is `layers` (boundary found), `base_only` (history and layers do not match, so only the base image
counts - the dependencies are then covered by the code rule), `base` (base profile: the code is only mounted, the whole image
is the environment) or `unknown` (no usable base information, `fp` is null). The mapping assumes the classic builder
(`DOCKER_BUILDKIT=0`, which the receiver uses): `RUN`, `COPY`, `ADD` and `WORKDIR` create a layer. Failing to find the identity
never fails a run.

**What the result store does with it.** Every result carries `image` and `env_fp` (older stores are migrated in place with
`ALTER TABLE ADD COLUMN`; results without a fingerprint behave as before). `plan(..., env_fp=...)` never reuses a green result
with a known, different fingerprint; `foreign_env(green, env_fp)` answers after the run, when the fingerprint of the run is
known, which reused results came from another environment - `remote_test.py --only-needed` runs exactly those again. The
fingerprint it plans with is the one seen last for the same environment family (`profile/mode`).

**Full-run rule.** At least one full green run (nothing reused) must exist per minor version (`X.Y`) and per image
environment. `full_run_reason(store, family, version, env_fp)` returns `None` or the reason; with a reason the plan runs
everything (`plan(..., everything=True)`). The first run on a new minor version or after an environment change is therefore a
full run, recorded with `record_full_run()`. Transition: full runs without a fingerprint count as long as no other environment
is known. Repositories without a `VERSION` file are not subject to the rule. Runs recorded before this version are taken over
with `backfill-full-runs`.

```bash
python3 result_store.py full-runs --project app [--family image/full]
python3 result_store.py backfill-full-runs --project app --repo . --family image/full   # green runs with >= 200 new tests, none reused
```

## Variants (0.32.0)

Two environments that differ only by one environment variable (for example `image+FLAG=0` and `image+FLAG=1`) are siblings.
`plan(..., variants=[(environment, excluded_files)])` and `--variant ENV:NAME[,NAME]` (result store CLI and `--only-needed`)
let results of a sibling count too: per test the **newest** result wins (red stays red), except for the excluded test files.
`variant_exclusions(repo, commit, names)` finds them with `git grep`: if a name appears only in test files, those files are
excluded (they read the variable themselves); if it appears anywhere else (application code, `conftest.py`, a test helper) every
test may be affected and the result is `None`, meaning nothing is taken over. A git error also gives `None`.

## Work tree, meta file and image identity (0.32.0)

- `remote_test.py <repo> --profile P --mode M --worktree -- tests/test_x.py` tests the work tree **including uncommitted
  changes and new, not ignored files**. It builds a throw-away commit from a private index (`GIT_INDEX_FILE` = temp file,
  `read-tree HEAD`, `add -A`, `write-tree`, `commit-tree -p HEAD`): the real index, branches and stash stay untouched, and a
  clean tree simply means `HEAD`. The commit is unreferenced and disappears with the next `git gc`.
- `--meta FILE` writes the result of the run as JSON (outcome, `rc`, durations, `commit`, `image`), also for red runs. A caller
  that keeps its own result store can read the image identity from it.
- `--images` prints what the receiver's read-only `images` command answers: per base profile the checksum of the Dockerfile
  template, the `FROM` reference and its image id (`null` if it is not on the test machine) and a checksum of the pins. Use it
  to put the base identity into a result key before anything runs. Exit 100 if the runner is unavailable.

## Tests

`python3 -m unittest discover -s scripts/test-runner` (stand-ins for ssh and docker, no real machine) and
`cd backend && python3 -m unittest tests.test_test_runner_status tests.test_eta tests.test_pool_snapshot`. CI runs both.
