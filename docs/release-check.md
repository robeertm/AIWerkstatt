# Release check — does the published release contain everything the project advertises?

A tag can be green in CI and still ship without the mobile package the README promises, without a signature, or with
`:latest` pointing at the previous container image. `scripts/release-check/release_check.py` declares per project what is
offered and checks the **published** result after each release. Standard library only (optional `cryptography` for
signature verification); `gh` runs as the current user with its own login. It never tags, never creates or deletes a release.

```
python3 scripts/release-check/release_check.py check [--project P ...] [--dry-run] [--no-heal]
python3 scripts/release-check/release_check.py status
python3 scripts/release-check/release_check.py history <project> [--count N]
python3 scripts/release-check/release_check.py assign <project> <version> <ticket> --reason TEXT
python3 scripts/release-check/release_check.py trigger <project> [<version>]
python3 scripts/release-check/release_check.py run-triggers
```

Common options: `--config FILE` (default `$RELEASE_CHECK_CONFIG`, else `deliverables.json` next to the script),
`--state-dir DIR` (default `$RELEASE_CHECK_STATE`, else `~/.local/state/release-check`), `--status FILE`
(default `<state-dir>/status.json`).

## Declaration: `deliverables.json`

Copy `scripts/release-check/deliverables.example.json` and edit it (format `deliverables/1`). `{v}` stands for the version
without the leading `v`. Per project (key = project id):

| key | meaning |
|---|---|
| `name`, `repo` (`owner/name`), `source` | display name, repository, where the promise comes from (free text) |
| `release` | `github` (default; a GitHub release per tag) or `tag` (the tag is the release) |
| `notes`, `latest` | release notes not empty (for `tag`: the changelog has a section for the version); the "latest release" marker points here |
| `grace_min` | grace period after publishing (default 90) |
| `assets[]` | `pattern`, `title`, optional `signature` (e.g. `.sig`): file present, state `uploaded`, size > 0, signature partner present |
| `checksums` | `file`, `all`: every listed entry equals the asset digest GitHub computed; with `all`, every asset is listed |
| `updater_manifest` | `file` (e.g. `latest.json`): version, every platform URL points to an asset of this release with a `.sig`, signature equals the `.sig` content |
| `minisign` | `repo_file`, `json_path`: public key read from the repo at the tag; `.sig` files are verified cryptographically (Ed25519, BLAKE2b prehash) |
| `images[]` | `name`, `tags`, `latest_equal`, `signed`, `sbom`: tag `:<version>` exists, `:latest` equals it, signature / SBOM attached |
| `version_files[]` | `repo`, `path`, `ref` (default `v{v}`), `pattern` (regex with `{v}`), `title` |
| `pages[]` | `probe`, `title`, `patterns`: a download page must mention every pattern |
| `heal[]` | `kind` = `command` / `rerun_ci` / `promote` (see below) |
| `report_only` | never heal, only run the hook |

Top level: `excluded` (project id -> reason, listed in the status), `on_red` (optional operator command, see below).
No asset with a foreign version in its name may be in a release. Page URLs are **not** in this file: they live in
`probes.json` in the state directory (`{"<probe>": "https://..."}`, https only). A probe without an address is a hint, not a gap.

Images work with any registry that speaks the OCI distribution API; the anonymous bearer token is fetched from the
`WWW-Authenticate` challenge. Only public images can be checked. A signature or SBOM is found through the referrers tag
scheme `sha256-<hex>`.

## Waiting, confirmation, healing, red

1. **Waiting.** A gap inside the grace period, or while a CI run of the tag is still running (cap 6 h), is `waiting`.
2. **Confirmation.** Every gap must persist 20 minutes before anything happens; transient states (an image tag that is
   ahead of its release for a few minutes) heal themselves.
3. **Healing**, each way once per version: `command` (runs `command` with `{v}` replaced, in a clone of the repo under
   `<state-dir>/clones`, optional `for` = asset patterns it fixes, `requires` = file that must exist, `clone: false` + `cwd`
   to skip the clone), `rerun_ci` (re-runs the failed runs of the tag), `promote` (dispatches `workflow`, default
   `promote.yml`, with `version`). `promote` is never used when a newer tag exists or a newer image tag is already
   published: `:latest` then belongs to the newer version and is checked there.
4. **Red.** Still open 45 minutes after a heal attempt, or no way exists: stage `red`. The optional `on_red` command
   (a list, for example `["/usr/local/bin/notify-gap"]`) runs once per project and version with the path of a JSON report
   (`<state-dir>/reports/<project>-<version>.json`, format `release-check/1`) as its last argument; a failing hook is retried
   next run. `assign` records a ticket for a running fix and suppresses the hook for that version.

A network error is stage `unknown`, never red. A new version starts with fresh state.

## Output and exit codes

The status file (`release-check/1`, 0644) holds `overall` (`green|waiting|gaps`), a one-line summary, and per project the
stage (`green|waiting|gap|red|unknown`), gaps (`kind`, `what`, `text`), checks run, hints, heal attempts and ticket. The
**last stdout line** is `RELEASE-CHECK: green|waiting|gaps (...)`.

| exit | meaning |
|---|---|
| 0 | green or waiting |
| 1 | gaps (red, or healing in progress) |
| 2 | bad usage or no valid declaration |

A file named `STOP` in the state directory turns healing and the hook off; checking and the status keep working.
`--dry-run` neither heals nor runs the hook and leaves no state behind.

## Units

Example systemd units in `scripts/release-check/`: `release-check.service` + `.timer` (every 15 minutes) and
`release-check-trigger.path` + `.service` (right after a release: a release script runs `release_check.py trigger <project>`,
the path unit starts `run-triggers`). Copy them, set `User=` and the paths.

## Tests

`python3 -m unittest scripts/release-check/test_release_check.py` (no network: a fake object plays GitHub, the registry and
the download page; real Ed25519 keys for the signature test).
