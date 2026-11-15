# Remote test runner

`scripts/test-runner/` runs a heavy test suite on a **second machine** and shows that machine's load in the
admin overview. It is optional, needs only Python 3 (standard library) and Docker on the test machine, and does
nothing unless you configure it. Nothing in it is specific to one host or vendor.

| File | Runs on | Purpose |
|---|---|---|
| `remote_test.py` | client | packs a commit, sends it, throttles, shows the output, returns the exit code; `--status`, `--available`, `--collect` |
| `remote_test_receiver.py` | test machine | the forced command: `status`, `run`, `result` (Python 3.8+) |
| `profiles.example.json` | test machine | allow list of profiles, modes, base images and limits |
| `install.sh` | client | sets up the key, the receiver and the profiles over SSH, then proves the key is restricted |
| `remote-test-collector.service` | client | example unit for the load collector |

## Security model

The trust boundary is one SSH key that can only start the receiver (details in
[security](security.md#remote-test-runner)):

- `authorized_keys` line: `restrict,from="<client address>",command="/usr/bin/python3 -I <dir>/remote_test_receiver.py" <public key>`.
  `restrict` disables forwarding and the terminal, `from` pins the source address, `command` ignores whatever the
  caller asks for; the receiver reads the request from `SSH_ORIGINAL_COMMAND` and accepts three subcommands only.
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
- Mode keys: `command`, `targets` (default pytest targets), `read_only` (default true), `env` (fixed variables),
  `workdir`, `junit` (default true for pytest) and `extra`.
- **CPU limit:** `cpu_mode: "cpus"` uses `--cpus` (the kernel needs CFS quota; image builds get the equivalent
  `--cpu-quota`). `cpu_mode: "cpuset"` uses `--cpuset-cpus` with one set per slot from `cpusets`, for kernels
  without quota support. `max_runs` is capped at 2 by the receiver.
- `docker` is the command used to reach Docker (for example `["sudo", "-n", "/usr/bin/docker"]`).

## Throttle rules

1. The client reads `status` first. If the runner is busy (a test running, CPU pressure >= 40 %, CPU >= 70 % or load >=
   3/4 of the cores) it waits and checks every 30 seconds, at most `--wait-max-min` minutes (default 60), then starts
   anyway. Nothing is ever aborted to make room.
2. The receiver allows 2 runs at once (one lock file per slot, checked immediately). A third request gets `busy`; the
   client keeps waiting for a free slot for at most as long again, then gives up with exit 100.
3. A running test is never aborted, also not when the connection drops (the receiver ignores SIGHUP and SIGPIPE). The
   client fetches the result with `result <id>` until it is there (results stay for 2 days). The only limit is the
   emergency brake `max_run_seconds`.
4. A protocol per run is written on both sides: duration, build and test time, load and CPU samples of the runner and
   the unhealthy containers before, during and after (own `probe-*` containers excluded).

## Exit codes

- The exit code of the test run (pytest: 0 green, 1 red, 2 collection error, ...). Output goes to stdout unchanged,
  build and info lines to stderr.
- **100 = runner unavailable:** switched off, not configured, unreachable, request refused (for example an argument
  outside the allow list), no free slot in time, result lost, or a fault of the runner instead of the code. The caller
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
                "psi_io": null, "ram_free_gb": 5.5, "cpu_percent": 37.0, "test_runs": 1, "max_runs": 2}
```

`fresh` is true while the last successful sample is younger than 120 seconds, `stand` is its unix time. Without the
variable, or if the file is missing or unreadable, the field is absent. The "Agents at work" card shows one line, for
example "Test runner: 37 % CPU · load 3.4 / 8 cores · 1 of 2 runs", and a "stale" chip when the sample is not fresh.
Run details and ids stay out of the field.

## Tests

`python3 -m unittest discover -s scripts/test-runner` (stand-ins for ssh and docker, no real machine) and
`cd backend && python3 -m unittest tests.test_test_runner_status`. CI runs both.
