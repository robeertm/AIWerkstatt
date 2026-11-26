# Parity inventory — what the private control plane has that this one lacks

AIWerkstatt is the public, platform-independent sibling of a private, single-site control plane. The two codebases are
separate (container-per-agent here, host broker there), so features are mirrored by hand. This table is the checkpoint
for that work. Status as of 0.46.0 (wave 2 of the private side, 0.205.0 - 0.212.0, bundled in one release); the private side was compared by its changelog, entry by entry.

Legend: **mirrored** = present here · **by design** = the architecture makes it unnecessary · **gap** = generic and missing ·
**private** = tied to one site's hardware, accounts, houses or tooling and deliberately not mirrored.

## Generic features

| Area | Feature | Status | Note |
| --- | --- | --- | --- |
| Pool | Fixed worker pool, strict order, subagent guard | mirrored | 0.15.0 |
| Pool | Switch the pool down ("only N of M slots") | mirrored | 0.34.0 |
| Pool | Idle slots are refilled, only running tasks count | mirrored | 0.38.0 |
| Pool | Ghost slot released when its session is gone | by design | a run is busy only while its container is alive |
| Pool | CPU cap per agent run | **mirrored in 0.40.0** | `AIWERKSTATT_RUN_CPUS`, default no cap |
| Pool | **Build/review split** — builder slots hand off branch + commit + test plan and are freed at once; AI-free test runners execute suites and gates; reviewer slots decide green (merge, release) or rework, which goes back to the same parked builder session with priority; switch off/probe/on | **gap** | private side since 0.229.0; port as its own task (runner service, reviewer slot class, finding format, flow API for dashboards) |
| Queue | Remaining time per run and per task | mirrored | 0.25.0 |
| Queue | Queues instead of wait locks | mirrored | 0.24.0 |
| Queue | **Priority in the queue** (urgent, next, move up/down, drag) | **mirrored in 0.48.0** | one rule for orchestrator and overview; deviations: no issue-watch class and no plan-chain insertion (`before` action), those belong to private services; see [queue priority](queue-priority.md) |
| Queue | Stopped task never stays in the queue | mirrored | 0.27.0 |
| Agent | Live feed, timestamps, vital signs, API-error state | mirrored | 0.16.0 – 0.18.0 |
| Agent | API errors heal themselves, bounded retries (3, 5/15/45 min) | mirrored | 0.14.3, 0.17.0 |
| Agent | Open background work is not a finished task | mirrored | 0.23.0 |
| Agent | **Stall guard**: hung runs detected on several axes, three stages, re-queue at the front | mirrored | 0.44.0, on by default (`WK_STALL_GUARD=0` turns it off); see [stall guard](stall-guard.md) for the deviations |
| Queue | Remaining time says "overdue since ..." instead of a frozen estimate | mirrored | 0.44.0 |
| Agent | Context pack with knowledge and rule block | mirrored | 0.17.0 |
| Agent | Reading proof | mirrored | 0.29.0, opt-in |
| Agent | One working tree per project, follow-ups never lost | mirrored | 0.18.1 |
| Agent | Notes between parallel tasks | mirrored | 0.28.0 |
| Access | Service tokens (restricted, coordinator, reporter, chain, peer) | mirrored | 0.13.0 – 0.28.0 |
| Access | App feedback intake (wishes from app instances, state read-back) | mirrored | 0.42.0 |
| Access | Stop with a reason via coordinator token | mirrored | 0.27.0 |
| Release | Release train, one result per commit | mirrored | 0.26.0 |
| Release | Branch check ("waiting for release" really on main?) | mirrored | 0.31.0, 0.36.0 |
| Release | Roll out and measure after each release (plans) | mirrored | 0.37.0 |
| Release | Release only a commit the gate has seen | mirrored | `scripts/chain`, release commit field |
| Tests | Remote test runner, green stays green, image environment | mirrored | 0.22.0 – 0.33.0 |
| Plans | Task chain and multi-project plans, credit limit | mirrored | 0.20.0 – 0.38.0 |
| Plans | **Early warning "the plans run dry"** (plan hours x pace against the slots, one event below 6 h) | **mirrored in 0.46.0** | `AIWERKSTATT_PLAN_PACE_FACTOR`, optional `AIWERKSTATT_PLAN_NOTIFY`; see [chain](chain.md) |
| Plans | **Dependency on an entry of another plan** (`<plan>:<project>/<ID>`; private: 0.227.0) | **mirrored in 0.47.0** | form checked on save, state read at run time, simulation treats it as met; see [chain](chain.md) |
| Tests | **Queue classes**: release gate runs before probe runs, aged probe runs catch up (private: gate traffic light, 0.205.0) | **mirrored in 0.46.0** | `--queue-class`, `AIWERKSTATT_TEST_RUNNER_CLASS`; no deadline, nothing aborted; see [test runner](test-runner.md) |
| Tests | Gate traffic light with a protocol and weekly wait report | private | the report reads one site's gate service; the generic part (order of the queue) is the row above |
| Tests | Browser-test virtual environment tool (shared browsers, pinned test tools) | private | tooling of one host; the containerised test runner here brings its own images |
| Release | One delivery way for small services without a release manifest, commit checker without release target | by design | every repo here is released by the release train or has no release target at all; the branch check (0.31.0, 0.36.0) already covers "is it on main" |
| Overlap guard | Reply draft for an already finished issue case (follow-up draft) | private | belongs to the issue watcher, which is private (see the overlap-guard row below) |
| Wishes | Wish list status derived from the claim book and published tags | private | reads the claim book of the private issue watcher; the public intake (`app_feedback.py`) has no such list |
| Ops | Load fingerprint (top consumers per slot/service when the load spikes) | private | reads the host's per-slot accounting and the night watch; a container-per-run setup has neither |
| Security | Dependency audit and history secret scan | mirrored | 0.39.0 |
| Security | No secret on any command line | mirrored | 0.14.1 |
| Ops | Docker health check | mirrored | 0.14.4 |
| UI | Agents-at-work strip, self-reload after update | mirrored | 0.15.1 |
| UI | Mobile and desktop visual tour over every tab | **gap** (process, not code) | re-run the browser tour against the current UI before each release |
| Registry | Service register (every service announces itself) | private | depends on a host-level service manager |
| Project book | Per-project status API for a site map | private | feeds one site's dashboard; see `docs/architecture.md` for the public pool API |
| Overlap guard | At most one open task per issue / duplicate protection | gap (needs an issue watcher) | meaningless without the issue intake, which is private |

## Deliberately private

Mac bridge and its waiting list, notification gateway (messaging agent), claim book and decision register, issue watcher
and trusted-reporter care, night watch and morning report, update watch, firewall/UniFi/Home Assistant brokers, mail-order
service, license tooling, site-map collectors and their signing, per-site token profiles (`oberboss`, `pi-slots`),
Pi/NAS load cards and load guard, host systemd units, vault bridge. They depend on one operator's houses, hardware and
accounts; mirroring them would add personal infrastructure to a public, generic product.

## How to keep this current

On every change to the private side: add or update the row, mirror the generic part (translate the architecture, do not
copy), and run `python3 backend/scrub/scan.py .` before pushing.
