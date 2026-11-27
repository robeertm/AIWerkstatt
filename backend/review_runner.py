"""AI-free review runner (build/review split, part 1) — runs the check commands of handed-over commits.

The private original runs check commands as a host service under a system user, with per-host limits. Here
everything runs in containers, started by the web app through the hardened Docker socket proxy (the same way
agent runs are started):

* per job, a fresh named volume holds the source of exactly the handed-over commit. A short *prepare*
  container (no network, unprivileged) mounts the project workspace read-only and extracts the commit with
  ``git archive`` into the job volume. git never runs inside the web container, so repository config written
  by an agent (filters, hooks) can never execute next to the provider credentials;
* every check step is its own container on the job volume: unprivileged (uid 10001, all capabilities dropped,
  no-new-privileges), no credentials, no Docker access, network only if its target allows it;
* limits per target (``local`` / ``online`` by default, configurable), never a total limit;
* a green step is remembered per (project, commit, command, target, image) and reused for 7 days;
* a step that prints nothing for 45 minutes or runs longer than 6 hours is ended and red with a reason.

The runner never decides green or rework; it only measures. A reviewer records the decision through the API
(``review_jobs.record_finding``). See docs/review-runner.md.
"""
from __future__ import annotations

import os
import shlex
import threading
import time

import config
import review_jobs as rj

TICK_S = 5
LOG_MAX_BYTES = 5 * 1024 * 1024
TAIL_CHARS = 1500


def _env_int(name, default, lo=1):
    try:
        return max(lo, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def silence_s():
    return _env_int("AIWERKSTATT_REVIEW_SILENCE_MIN", 45) * 60


def max_s():
    return _env_int("AIWERKSTATT_REVIEW_MAX_HOURS", 6) * 3600


PREPARE_MAX_S = 15 * 60


def _log(msg):
    print("[review-runner] %s" % msg, flush=True)


def _tail(path, n=TAIL_CHARS):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4 * n))
            return f.read().decode("utf-8", "replace")[-n:]
    except OSError:
        return ""


class ReviewRunner:
    """One tick at a time: collect finished steps, then start what the limits allow. A job runs its steps one
    after another (later steps often build on earlier ones); different jobs run in parallel up to the limit of
    each target."""

    def __init__(self, executor, data=None, clock=time.time):
        self.ex = executor
        self.data = data
        self.clock = clock
        self.active = {}            # (job id, step nr; 0 = prepare) -> {"h", "start", "target", "log"}
        self._recovered = False
        self.targets, self.targets_error = rj.load_targets()

    # ---- helpers --------------------------------------------------------
    def busy(self, target):
        return sum(1 for a in self.active.values() if a["target"] == target)

    def _job_active(self, jid):
        return any(k[0] == jid for k in self.active)

    def _log_path(self, jid, nr):
        d = os.path.join(rj.logs_dir(self.data), jid)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "step-0-prepare.log" if nr == 0 else "step-%d.log" % nr)

    # ---- the tick -------------------------------------------------------
    def tick(self):
        for d in (rj.base_dir(self.data), rj.jobs_dir(self.data), rj.logs_dir(self.data)):
            os.makedirs(d, exist_ok=True)
        if not self._recovered:
            self._recover()
            self._recovered = True
        self.collect()
        jobs = rj.runner_order(rj.list_jobs(self.data))
        for j in jobs:                       # cancelled jobs: end what still runs, drop the job volume
            if j["phase"] == "cancelled":
                self._end_cancelled(j)
        if os.path.exists(rj.stop_file(self.data)):
            return
        cache = rj.cache_load(self.data, self.clock())
        for j in jobs:
            if j["phase"] in ("waiting_runner", "running"):
                try:
                    self.advance(j, cache)
                except Exception as e:  # noqa: BLE001 — one job must never stop the runner
                    _log("job %s: %r" % (j.get("id"), e))

    def _recover(self):
        """After a restart nothing is running any more: steps marked running go back to waiting and a stale job
        volume is rebuilt from the commit (the result of a half-run step is never trusted)."""
        for j in rj.list_jobs(self.data):
            run = dict(j.get("run") or {})
            if j["phase"] != "running":
                continue
            changed = False
            for s in run.get("steps") or []:
                if s.get("status") == "running":
                    s.update(status="waiting", start=None)
                    changed = True
            if run.get("prepare") == "running" or changed:
                run["prepared"] = False
                run.pop("prepare", None)
                changed = True
            if changed:
                try:
                    self.ex.reset(j)
                except Exception as e:  # noqa: BLE001
                    _log("job %s: reset after restart failed: %r" % (j["id"], e))
                rj.write_run(j["id"], run, self.data)
                _log("job %s: steps that were running before the restart wait again" % j["id"])

    def _end_cancelled(self, j):
        jid = j["id"]
        for key in [k for k in self.active if k[0] == jid]:
            try:
                self.active[key]["h"].kill()
            except Exception:  # noqa: BLE001
                pass
        run = j.get("run") or {}
        if run.get("cleaned"):
            return
        if not self._job_active(jid):
            try:
                self.ex.cleanup(j)
            except Exception as e:  # noqa: BLE001
                _log("job %s: cleanup failed: %r" % (jid, e))
                return
            run = dict(run)
            run["cleaned"] = int(self.clock())
            rj.write_run(jid, run, self.data)

    def advance(self, j, cache):
        jid = j["id"]
        run = dict(j.get("run") or {})
        if not run.get("steps"):
            run = {"status": "running", "start": int(self.clock()), "prepared": False,
                   "steps": [dict(s, status="waiting") for s in j.get("checks") or []]}
            rj.write_run(jid, run, self.data)
            _log("job %s: review starts (%d steps)" % (jid, len(run["steps"])))
        if self._job_active(jid):
            return
        waiting = [s for s in run["steps"] if s.get("status") == "waiting"]
        hits = 0
        # steps already green for this commit come straight from the cache (no container, no source needed)
        while waiting:
            s = waiting[0]
            hit = cache.get(rj.cache_key(j, s, self.targets.get(s["target"])))
            if not hit:
                break
            s.update(status="green", rc=0, duration_s=0, from_cache=True, log=hit.get("log"), start=int(self.clock()))
            waiting.pop(0)
            hits += 1
        if not waiting:
            self._finish(j, run)
            return
        s = waiting[0]
        tcfg = self.targets.get(s["target"])
        if tcfg is None:
            s.update(status="red", rc=None, reason="target %r is no longer configured" % s["target"], start=int(self.clock()))
            rj.write_run(jid, run, self.data)
            return
        if self.busy(s["target"]) >= int(tcfg.get("limit") or 1):
            if hits:
                rj.write_run(jid, run, self.data)
            return
        if not run.get("prepared"):
            lp = self._log_path(jid, 0)
            h = self.ex.prepare(j, lp)
            run["prepare"] = "running"
            rj.write_run(jid, run, self.data)
            self.active[(jid, 0)] = {"h": h, "start": self.clock(), "target": s["target"], "log": lp}
            _log("job %s: preparing the source of %s" % (jid, j.get("commit", "")[:12]))
            return
        lp = self._log_path(jid, s["nr"])
        h = self.ex.start(j, s, tcfg, lp)
        s.update(status="running", start=int(self.clock()), log=lp)
        rj.write_run(jid, run, self.data)
        self.active[(jid, s["nr"])] = {"h": h, "start": self.clock(), "target": s["target"], "log": lp}
        _log("job %s: step %d [%s] started" % (jid, s["nr"], s["target"]))

    def _finish(self, j, run):
        run["status"] = "ran"
        run["end"] = int(self.clock())
        run["green"] = all(s.get("status") == "green" for s in run["steps"])
        rj.write_run(j["id"], run, self.data)
        try:
            self.ex.cleanup(j)
            run["cleaned"] = int(self.clock())
            rj.write_run(j["id"], run, self.data)
        except Exception as e:  # noqa: BLE001
            _log("job %s: cleanup failed: %r" % (j["id"], e))
        _log("job %s: runner finished (%s)" % (j["id"], "all green" if run["green"] else "with red steps"))

    def collect(self):
        now = self.clock()
        for key, a in list(self.active.items()):
            jid, nr = key
            h = a["h"]
            rc = h.poll()
            reason = ""
            if rc is None:
                quiet = now - max(a["start"], h.last_output() or a["start"])
                cap = PREPARE_MAX_S if nr == 0 else max_s()
                if now - a["start"] > cap:
                    reason = "emergency stop: running longer than %d min" % (cap // 60)
                elif nr and quiet > silence_s():
                    reason = "emergency stop: no output for %d min" % (silence_s() // 60)
                else:
                    continue
                try:
                    h.kill()
                except Exception:  # noqa: BLE001
                    pass
                rc = h.poll()
                if rc is None:
                    rc = -9
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
            del self.active[key]
            j = rj.read_job(jid, self.data)
            if not j:
                continue
            run = dict(j.get("run") or {})
            if nr == 0:
                run.pop("prepare", None)
                if rc == 0 and not reason:
                    run["prepared"] = True
                    _log("job %s: source ready" % jid)
                else:
                    err = (reason or "exit %s" % rc) + ": " + _tail(a["log"], 600).strip()
                    run["source_error"] = err[:800]
                    for s in run.get("steps") or []:
                        if s.get("status") == "waiting":
                            s.update(status="red", rc=None, reason="source not available", start=int(now), duration_s=0)
                    _log("job %s: source not available (%s)" % (jid, reason or "exit %s" % rc))
                rj.write_run(jid, run, self.data)
                continue
            for s in run.get("steps") or []:
                if s.get("nr") != nr:
                    continue
                ok = rc == 0 and not reason
                s.update(status="green" if ok else "red", rc=rc, duration_s=int(now - a["start"]),
                         reason=reason or None, tail=_tail(a["log"]))
                if ok and not run.get("cancelled"):
                    rj.cache_store(rj.cache_key(j, s, self.targets.get(s["target"])), jid, a["log"], self.data, now)
            rj.write_run(jid, run, self.data)
            _log("job %s: step %d -> %s (exit %s)%s" % (jid, nr, "green" if rc == 0 and not reason else "red", rc,
                                                        (" " + reason) if reason else ""))

    # ---- status for the API ------------------------------------------------
    def status(self):
        jobs = rj.list_jobs(self.data)
        mode = rj.load_mode(self.data)
        return {"format": "review-runner-status/1", "mode": mode["mode"], "probe_max": mode["probe_max"],
                "since": mode["since"], "by": mode["by"], "stopped": mode["stopped"],
                "available": bool(getattr(self.ex, "available", True)),
                "targets": {n: {"limit": t["limit"], "network": t["network"], "busy": self.busy(n)}
                            for n, t in sorted(self.targets.items())},
                "targets_error": self.targets_error,
                "running": [{"job": k[0], "step": k[1] or "prepare", "target": a["target"],
                             "since": int(a["start"])} for k, a in sorted(self.active.items())],
                "counts": rj.counts(jobs), "cache_entries": len(rj.cache_load(self.data))}

    def run_loop(self, interval=TICK_S):
        _log("started")
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                _log("tick: %r" % e)
            time.sleep(interval)


# ---------------------------------------------------------------- Docker executor

def extra_labels(raw):
    out = {}
    for part in str(raw or "").split(","):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip() and len(out) < 10 and all(ch.isalnum() or ch in "._-" for ch in k.strip()):
            out[k.strip()[:63]] = v.strip()[:200]
    return out


class _LogPump:
    """Follows the container's output into the step log (capped) and remembers when the last output came."""

    def __init__(self, container, path):
        self.container = container
        self.path = path
        self.last = time.time()
        self.written = 0
        self.t = threading.Thread(target=self._pump, daemon=True, name="review-log")
        self.t.start()

    def _pump(self):
        try:
            with open(self.path, "ab") as f:
                for chunk in self.container.logs(stream=True, follow=True):
                    self.last = time.time()
                    if self.written < LOG_MAX_BYTES:
                        part = chunk[:LOG_MAX_BYTES - self.written]
                        f.write(part)
                        f.flush()
                        self.written += len(part)
                        if self.written >= LOG_MAX_BYTES:
                            f.write(b"\n[review-runner] log capped at 5 MB, output continues\n")
        except Exception as e:  # noqa: BLE001
            try:
                with open(self.path, "ab") as f:
                    f.write(("\n[review-runner] log stream ended: %r\n" % e).encode())
            except OSError:
                pass


class _DockerHandle:
    def __init__(self, container, path):
        self.c = container
        self.pump = _LogPump(container, path)
        self.rc = None

    def poll(self):
        if self.rc is not None:
            return self.rc
        try:
            self.c.reload()
        except Exception:  # noqa: BLE001 — container gone: treat as failed
            self.rc = -1
            return self.rc
        st = self.c.attrs.get("State") or {}
        if st.get("Status") in ("exited", "dead"):
            self.rc = int(st.get("ExitCode", -1))
            self.pump.t.join(timeout=5)
        return self.rc

    def last_output(self):
        return self.pump.last

    def kill(self):
        try:
            self.c.kill()
        except Exception:  # noqa: BLE001
            pass
        for _ in range(20):
            if self.poll() is not None:
                return
            time.sleep(0.25)

    def close(self):
        try:
            self.c.remove(force=True)
        except Exception:  # noqa: BLE001
            pass


class DockerExecutor:
    """Starts prepare and step containers through the Docker API (in the web app: via the socket proxy)."""

    LABEL = "aiwerkstatt.review"

    def __init__(self, client, image=None, workspaces_volume=None, uid=None, workdir=None):
        self.client = client
        self.available = client is not None
        self.image = image or os.environ.get("AIWERKSTATT_REVIEW_IMAGE", "") or config.AGENT_RUNNER_IMAGE
        self.workspaces_volume = workspaces_volume or os.environ.get("AIWERKSTATT_VOL_WORKSPACES", "aiwerkstatt-workspaces")
        self.uid = str(uid or os.environ.get("AIWERKSTATT_APP_UID", "10001"))
        # A fresh named volume takes over owner and mode of the image folder it is mounted on; /workspace belongs to
        # uid 10001 in the runner image. A custom image needs a folder there that this uid may write.
        self.workdir = workdir or os.environ.get("AIWERKSTATT_REVIEW_WORKDIR", "/workspace")
        self.mem = os.environ.get("AIWERKSTATT_REVIEW_MEM", "") or os.environ.get("AIWERKSTATT_RUN_MEM", "2g")
        self.pids = _env_int("AIWERKSTATT_RUN_PIDS", 512)
        # Optional extra labels for every review container ("key=value,key2=value2"), e.g. for a host's cleanup tooling.
        self.extra_labels = extra_labels(os.environ.get("AIWERKSTATT_REVIEW_LABELS", ""))

    def volume_name(self, job):
        return "aiwerkstatt-review-%s" % job["id"]

    def _ensure_volume(self, job):
        name = self.volume_name(job)
        try:
            self.client.volumes.get(name)
        except Exception:  # noqa: BLE001
            self.client.volumes.create(name=name, labels={self.LABEL: job["id"]})
        return name

    def _common(self, job, nr):
        return dict(detach=True, user="%s:%s" % (self.uid, self.uid), cap_drop=["ALL"],
                    security_opt=["no-new-privileges"], pids_limit=self.pids,
                    labels=dict(self.extra_labels, **{self.LABEL: job["id"], self.LABEL + ".step": str(nr)}),
                    name="aiwerkstatt-review-%s-%d-%d" % (job["id"], nr, int(time.time())))

    def prepare(self, job, log_path):
        slug, commit = job["project"], job["commit"]
        if not rj.SLUG_RX.match(slug) or not rj.COMMIT_RX.match(commit):
            raise ValueError("invalid project or commit")
        vol = self._ensure_volume(job)
        repo = "/repo/" + slug
        git = "git -c safe.directory='*' -C %s" % shlex.quote(repo)
        script = ("set -e; %s cat-file -e %s^{commit}; %s archive --format=tar %s | tar -x -C %s -f -; "
                  "echo \"source of %s ready\"" % (git, commit, git, commit, shlex.quote(self.workdir), commit[:12]))
        c = self.client.containers.run(
            self.image, entrypoint=["sh", "-c"], command=[script], working_dir=self.workdir,
            volumes={self.workspaces_volume: {"bind": "/repo", "mode": "ro"}, vol: {"bind": self.workdir, "mode": "rw"}},
            network_mode="none", mem_limit=self.mem, environment={"HOME": "/tmp"}, **self._common(job, 0))
        return _DockerHandle(c, log_path)

    def start(self, job, step, target, log_path):
        vol = self._ensure_volume(job)
        extra = {}
        if target.get("cpus"):
            extra["nano_cpus"] = int(float(target["cpus"]) * 1e9)
        env = {"CI": "1", "HOME": "/tmp", "AIWERKSTATT_REVIEW_JOB": job["id"],
               "AIWERKSTATT_REVIEW_STEP": str(step["nr"]), "AIWERKSTATT_REVIEW_COMMIT": job["commit"]}
        c = self.client.containers.run(
            target.get("image") or self.image, entrypoint=["sh", "-c"], command=[step["cmd"]],
            working_dir=self.workdir, volumes={vol: {"bind": self.workdir, "mode": "rw"}},
            network_mode=target.get("network") or "none", mem_limit=target.get("memory") or self.mem,
            environment=env, **extra, **self._common(job, step["nr"]))
        return _DockerHandle(c, log_path)

    def _containers(self, job):
        try:
            return self.client.containers.list(all=True, filters={"label": "%s=%s" % (self.LABEL, job["id"])})
        except Exception:  # noqa: BLE001
            return []

    def cleanup(self, job):
        for c in self._containers(job):
            try:
                c.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.client.volumes.get(self.volume_name(job)).remove(force=True)
        except Exception as e:  # noqa: BLE001
            if "404" not in str(e) and "not found" not in str(e).lower() and "No such" not in str(e):
                raise

    def reset(self, job):
        self.cleanup(job)
