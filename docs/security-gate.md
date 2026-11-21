# Security gate — dependency audit and secrets in history

`scripts/security-gate/security_gate.py` checks a repository before it ships. It runs in CI (job `security-gate`) and can be
run by any release gate. Standard library only; the audit tools are optional and found on `PATH`.

```
python3 scripts/security-gate/security_gate.py check <repo> [--commit SHA] [--exceptions FILE]... \
        [--json OUT] [--mode enforce|advisory] [--only dependencies|secrets]
```

The last stdout line is `SECURITY: green|red|advisory (...)`. Exit 0 = green (or advisory mode), 1 = red, 2 = bad usage.

## Dependencies

| ecosystem | source | tool |
|---|---|---|
| Python | pinned (`==`) lines of `requirements*.txt` (dev/test/lint/docs files skipped) and `pyproject.toml` `[project].dependencies` | `pip-audit --no-deps --disable-pip` (OSV) |
| Node | every `package-lock.json` outside `node_modules`/`tests`/`examples` | `npm audit --omit=dev --package-lock-only` |
| Rust | every `Cargo.lock`, only if `cargo-audit` is installed | `cargo audit` |

- Level comes from npm, the OSV entry (GHSA level) or the CVSS 3.x base score. **critical/high = red**; medium/low/unknown are
  advisory notes. Unpinned requirements and a `package.json` without lock file are listed as "not checked", never silently ignored.
- **Offline / fail closed:** each successful result is cached by file content (`~/.cache/aiwerkstatt-security-gate`,
  `AIWERKSTATT_GATE_CACHE`, 7 days). If a tool or the network fails and no cached result exists for exactly that content, the gate is red.
- Tool paths: `AIWERKSTATT_PIP_AUDIT`, `AIWERKSTATT_NPM`, `AIWERKSTATT_CARGO_AUDIT`.

## Secrets in history

Every line ever **added** and every file ever **created** in the history of the commit (`git log -p`, so a secret that was
removed later is still found) is checked against the `block` rules of `backend/scrub/scan.py` (`rules.default.toml` plus the optional private
overlay). There is no second pattern set. Findings are printed masked with a 16-hex fingerprint, never the match.
CI therefore checks out with `fetch-depth: 0`.

## Exceptions

`security-exceptions.json` in the repo root (or `--exceptions FILE`):

```json
{"exceptions": [
  {"kind": "dependency", "id": "GHSA-xxxx-xxxx-xxxx", "package": "example", "reason": "code path is not reachable", "until": "2027-03-01"},
  {"kind": "secret", "finding": "0123456789abcdef", "reason": "documented test fixture", "until": "2027-03-01"}
]}
```

`reason` (at least 10 characters) and `until` (at most 366 days ahead) are mandatory. An invalid list is red, an expired exception
no longer covers, an unused one is reported as removable. Dependency exceptions match the id or any alias (CVE).

## Load

One CPU core (`sched_setaffinity`) and every tool under `nice -n 19`.

Tests: `python3 -m unittest discover -s scripts/security-gate` (no network, fake tools, throw-away git repos).
