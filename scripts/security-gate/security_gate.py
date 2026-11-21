#!/usr/bin/env python3
"""Security gate: dependency audit + secret scan over the whole git history.

    security_gate.py check <repo> [--commit SHA] [--exceptions FILE]... [--json OUT]
                           [--mode enforce|advisory] [--only dependencies|secrets]

Two parts, one verdict:
  1. Dependencies - Python (pip-audit over pinned lines of requirements*.txt and
     pyproject [project].dependencies), Node (npm audit --omit=dev --package-lock-only per
     lock file), Rust (cargo audit per Cargo.lock, only if installed). Critical/high = red,
     medium/low/unknown = advisory. Results are cached by file content, so an offline run
     reuses a result for exactly the same content for up to CACHE_DAYS days; no cache and no
     network = red (fail closed).
  2. Secrets - every line ever added and every file ever created in the history of the
     commit, checked against the BLOCK rules of backend/scrub/scan.py (rules.default.toml
     plus the optional private overlay). No second pattern set. Findings are printed masked
     with a fingerprint, never the match itself.

Exceptions live in security-exceptions.json (see docs/security-gate.md): reason (>= 10
characters) and expiry date (<= 366 days ahead) are mandatory.

Output: JSON (format `security-gate/1`) and, as the LAST stdout line,
    SECURITY: green|red|advisory (...)
Exit 0 = green (or advisory mode), 1 = red, 2 = bad usage.
Load: one CPU core (sched_setaffinity), every tool runs under `nice -n 19`.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

FORMAT = "security-gate/1"
VERSION = "1.0.0"
CACHE = os.environ.get("AIWERKSTATT_GATE_CACHE", os.path.join(os.path.expanduser("~"), ".cache", "aiwerkstatt-security-gate"))
CACHE_DAYS = int(os.environ.get("AIWERKSTATT_GATE_CACHE_DAYS", "7"))
MAX_EXCEPTION_DAYS = 366
PIP_AUDIT = os.environ.get("AIWERKSTATT_PIP_AUDIT", "pip-audit")
NPM = os.environ.get("AIWERKSTATT_NPM", "npm")
CARGO_AUDIT = os.environ.get("AIWERKSTATT_CARGO_AUDIT", "cargo-audit")
OSV_URL = os.environ.get("AIWERKSTATT_GATE_OSV_URL", "https://api.osv.dev/v1/vulns/{id}")
TOOL_SECONDS = int(os.environ.get("AIWERKSTATT_GATE_TOOL_SECONDS", "600"))
RED_LEVELS = ("critical", "high")
LEVELS = ("critical", "high", "medium", "low", "unknown")
NOT_SHIPPED = re.compile(r"(^|/)(node_modules|tests?|fixtures?|examples?|\.venv|venv|vendor)(/|$)")
DEV_REQ = re.compile(r"(?i)(^|[-_.])(dev|test|tests|lint|docs?)([-_.]|$)")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "backend", "scrub"))
import scan as scrub  # noqa: E402  - the repo's own leak scanner (rules only there)


# ------------------------------------------------------------------ helpers
def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _today():
    d = os.environ.get("AIWERKSTATT_GATE_TODAY")      # tests: fixed day
    return datetime.date.fromisoformat(d) if d else datetime.date.today()


def one_core():
    """Pin the run to one core (children inherit). Never fatal."""
    try:
        allowed = sorted(os.sched_getaffinity(0))
        if len(allowed) > 1:
            os.sched_setaffinity(0, {allowed[-1]})
    except (AttributeError, OSError):
        pass


def _tool(name):
    if os.path.isabs(name):
        return name if os.access(name, os.X_OK) else ""
    return shutil.which(name) or ""


def _run(args, cwd):
    """Run a tool under nice -n 19 -> (rc, stdout, stderr); rc None = could not start / timed out."""
    try:
        p = subprocess.run(["nice", "-n", "19", *args], cwd=cwd, capture_output=True, text=True, timeout=TOOL_SECONDS)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", "timed out after %ss" % TOOL_SECONDS
    except OSError as e:
        return None, "", type(e).__name__


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def cache_read(kind, key):
    path = os.path.join(CACHE, "results", "%s-%s.json" % (kind, key[:32]))
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        when = datetime.datetime.fromisoformat(d["time"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    age = (_now() - when).total_seconds() / 86400
    return (d.get("data"), age) if age <= CACHE_DAYS else (None, age)


def cache_write(kind, key, data):
    folder = os.path.join(CACHE, "results")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "%s-%s.json" % (kind, key[:32]))
        tmp = path + ".tmp%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"time": _now().isoformat(timespec="seconds"), "data": data}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def tracked_files(repo):
    p = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, text=True)
    if p.returncode == 0:
        return [f for f in p.stdout.split("\0") if f]
    out = []
    for d, ds, fs in os.walk(repo):
        ds[:] = [x for x in ds if x != ".git"]
        out += [os.path.relpath(os.path.join(d, f), repo) for f in fs]
    return sorted(out)


# ------------------------------------------------------------------ levels (CVSS 3.x, OSV)
_CVSS = {"AV": {"N": .85, "A": .62, "L": .55, "P": .2}, "AC": {"L": .77, "H": .44},
         "UI": {"N": .85, "R": .62}, "CIA": {"H": .56, "L": .22, "N": 0.0}}


def _roundup(x):
    i = int(round(x * 100000))
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss3_score(vector):
    """Base score of a CVSS:3.x vector (spec 3.1), else None."""
    if not isinstance(vector, str) or not vector.startswith("CVSS:3"):
        return None
    m = dict(t.split(":", 1) for t in vector.split("/")[1:] if ":" in t)
    try:
        changed = m["S"] == "C"
        pr = {"N": .85, "L": .68 if changed else .62, "H": .5 if changed else .27}[m["PR"]]
        iss = 1 - (1 - _CVSS["CIA"][m["C"]]) * (1 - _CVSS["CIA"][m["I"]]) * (1 - _CVSS["CIA"][m["A"]])
        imp = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        expl = 8.22 * _CVSS["AV"][m["AV"]] * _CVSS["AC"][m["AC"]] * pr * _CVSS["UI"][m["UI"]]
    except KeyError:
        return None
    if imp <= 0:
        return 0.0
    return _roundup(min(1.08 * (imp + expl) if changed else imp + expl, 10))


def level_from_score(w):
    if w is None:
        return "unknown"
    return "critical" if w >= 9 else "high" if w >= 7 else "medium" if w >= 4 else "low"


_TEXT_LEVEL = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low", "INFO": "low"}


def level_from_osv(entry):
    """Level from an OSV entry: database_specific.severity (GHSA), else CVSS-3 vector, else unknown."""
    if not isinstance(entry, dict):
        return "unknown"
    t = str((entry.get("database_specific") or {}).get("severity") or "").upper()
    if t in _TEXT_LEVEL:
        return _TEXT_LEVEL[t]
    for s in entry.get("severity") or []:
        if isinstance(s, dict) and s.get("type") == "CVSS_V3":
            w = cvss3_score(s.get("score"))
            if w is not None:
                return level_from_score(w)
    return "unknown"


def osv_fetch(vid):
    """OSV entry (cached CACHE_DAYS; offline also older) -> (entry|None, error)."""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{3,80}", vid or ""):
        return None, "invalid id"
    path = os.path.join(CACHE, "osv", vid + ".json")
    old = None
    try:
        age = (_now().timestamp() - os.path.getmtime(path)) / 86400
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if age <= CACHE_DAYS:
            return old, ""
    except (OSError, ValueError):
        pass
    try:
        with urllib.request.urlopen(OSV_URL.format(id=vid), timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - network/HTTP/JSON: old copy or error
        return (old, "") if old is not None else (None, type(e).__name__)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return data, ""


def level_lookup(ids):
    """GHSA id first (carries the level), then the rest -> (level, error)."""
    err = ""
    for vid in sorted(set(ids), key=lambda i: (not i.upper().startswith("GHSA-"), i)):
        if vid.upper().startswith("CVE-"):
            continue
        e, f = osv_fetch(vid)
        if e is None:
            err = err or f
            continue
        lv = level_from_osv(e)
        if lv != "unknown":
            return lv, ""
    return "unknown", err


# ------------------------------------------------------------------ exceptions
def load_exceptions(paths, today=None):
    """-> (valid[], errors[]). Required: id (dependency) or finding (secret), reason >= 10 chars,
    until (date, at most MAX_EXCEPTION_DAYS from today)."""
    today = today or _today()
    valid, errors = [], []
    for path in paths:
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except OSError:
            continue                                   # missing file = no exceptions
        except ValueError:
            errors.append("%s: not valid JSON" % name)
            continue
        items = d.get("exceptions") if isinstance(d, dict) else None
        if not isinstance(items, list):
            errors.append("%s: list \"exceptions\" missing" % name)
            continue
        for i, e in enumerate(items):
            where = "%s[%d]" % (name, i)
            if not isinstance(e, dict):
                errors.append(where + ": not an object")
                continue
            kind = e.get("kind", "dependency")
            reason = str(e.get("reason", "") or "").strip()
            until = str(e.get("until", "") or "")
            ident = str((e.get("id") if kind == "dependency" else e.get("finding")) or "").strip()
            if kind not in ("dependency", "secret"):
                errors.append(where + ": kind must be dependency or secret")
                continue
            if not ident:
                errors.append(where + (": id missing" if kind == "dependency" else ": finding (fingerprint) missing"))
                continue
            if len(reason) < 10:
                errors.append(where + ": reason missing or too short")
                continue
            try:
                day = datetime.date.fromisoformat(until)
            except ValueError:
                errors.append(where + ": expiry missing or unreadable (until, YYYY-MM-DD)")
                continue
            if (day - today).days > MAX_EXCEPTION_DAYS:
                errors.append(where + ": expiry more than %d days ahead" % MAX_EXCEPTION_DAYS)
                continue
            valid.append({"kind": kind, "ident": ident, "package": str(e.get("package", "") or "").lower(),
                          "reason": reason[:300], "until": day.isoformat(), "expired": day < today,
                          "source": where, "used": False})
    return valid, errors


def _exc_dependency(f, excs):
    ids = {k.upper() for k in [f["id"], *f.get("aliases", [])]}
    for a in excs:
        if a["kind"] == "dependency" and a["ident"].upper() in ids and (not a["package"] or a["package"] == f["package"].lower()):
            return a
    return None


def _exc_secret(f, excs):
    return next((a for a in excs if a["kind"] == "secret" and a["ident"] == f["finding"]), None)


# ------------------------------------------------------------------ Python (pip-audit)
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9.+!_-]+)\s*(;.*)?$")
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement(line):
    """-> ("pin", name, version, line) | ("open", name) | None (comment/option/empty)."""
    z = line.split(" #", 1)[0].strip()
    if not z or z.startswith(("#", "-", "git+", "http:", "https:", "file:", ".")):
        return None
    z = re.sub(r"\s--hash=\S+", "", z).strip().rstrip("\\").strip()
    m = _PIN.match(z)
    if m:
        return ("pin", m.group(1).lower(), m.group(3), m.group(1) + (m.group(2) or "") + "==" + m.group(3) + (m.group(4) or ""))
    n = _NAME.match(z)
    return ("open", n.group(1).lower()) if n else None


def python_sources(repo, files):
    """Shipped Python requirements -> (pins{line: [files]}, open[(name, file)], used_files[])."""
    pins, opened, used = {}, [], []
    for rel in files:
        if NOT_SHIPPED.search(rel):
            continue
        base = os.path.basename(rel)
        lines = []
        if re.fullmatch(r"requirements[^/]*\.txt", base) and not DEV_REQ.search(base[len("requirements"):-4]):
            try:
                lines = open(os.path.join(repo, rel), encoding="utf-8").read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
        elif base == "pyproject.toml":
            try:
                import tomllib
                with open(os.path.join(repo, rel), "rb") as f:
                    lines = list((tomllib.load(f).get("project") or {}).get("dependencies") or [])
            except Exception:  # noqa: BLE001 - broken file: no requirements
                continue
        else:
            continue
        hit = False
        for z in lines:
            a = _requirement(z)
            if not a:
                continue
            hit = True
            if a[0] == "pin":
                pins.setdefault(a[3], []).append(rel)
            else:
                opened.append((a[1], rel))
        if hit:
            used.append(rel)
    return pins, opened, used


def pip_audit_parse(data, pin_sources):
    findings, notes, seen = [], [], set()
    for dep in (data or {}).get("dependencies") or []:
        name, ver = str(dep.get("name", "")).lower(), str(dep.get("version", ""))
        if dep.get("skip_reason"):
            notes.append("pypi %s: not checked (%s)" % (name, str(dep["skip_reason"])[:120]))
        for v in dep.get("vulns") or []:
            ids = {str(v.get("id", ""))} | {str(a) for a in v.get("aliases") or []}
            ids.discard("")
            if not ids or any((name, ver, i) in seen for i in ids):
                continue
            for i in ids:
                seen.add((name, ver, i))
            main = sorted(ids, key=lambda i: (not i.upper().startswith("GHSA-"), i))[0]
            sources = sorted({q for z, qs in pin_sources.items() if _requirement(z)[1] == name for q in qs})
            findings.append({"ecosystem": "pypi", "package": name, "version": ver, "id": main,
                             "aliases": sorted(ids - {main}), "level": None, "fixed_in": list(v.get("fix_versions") or []),
                             "title": str(v.get("description") or "")[:120].replace("\n", " "), "sources": sources})
    return findings, notes


def scan_python(repo, files, res):
    pins, opened, used = python_sources(repo, files)
    eco = {"files": used, "packages": len(pins), "status": "none"}
    res["dependencies"]["ecosystems"]["pypi"] = eco
    for name, rel in opened:
        res["notes"].append("pypi %s (%s): not pinned - not checked" % (name, rel))
    if not pins:
        return
    tool = _tool(PIP_AUDIT)
    text = "\n".join(sorted(pins)) + "\n"
    key = _sha("pypi\n" + text)
    data, error = None, ""
    if not tool:
        error = "pip-audit not installed"
    else:
        with tempfile.TemporaryDirectory(prefix="security-gate-") as t:
            req = os.path.join(t, "requirements.txt")
            with open(req, "w", encoding="utf-8") as f:
                f.write(text)
            rc, out, err = _run([tool, "-r", req, "--no-deps", "--disable-pip", "-f", "json", "-s", "osv",
                                 "--progress-spinner", "off", "--cache-dir", os.path.join(CACHE, "pip-audit")], t)
            try:
                data = json.loads(out) if rc in (0, 1) and out.strip() else None
            except ValueError:
                data = None
            if data is None:
                error = "pip-audit failed (exit %s): %s" % (rc, ((err or "").strip().splitlines() or [""])[-1])
    findings, notes = pip_audit_parse(data, pins) if data is not None else ([], [])
    if data is not None:
        for f in findings:
            lv, lerr = level_lookup([f["id"], *f["aliases"]])
            if lerr and lv == "unknown":
                data, error = None, "level of %s not available (%s)" % (f["id"], lerr)
                break
            f["level"] = lv
    if data is not None:
        cache_write("pypi", key, {"findings": findings, "notes": notes})
        eco["status"] = "checked"
    else:
        old, age = cache_read("pypi", key)
        if old is None:
            eco["status"], eco["error"] = "error", error
            res["errors"].append("pypi: " + error)
            return
        findings, notes = old.get("findings", []), old.get("notes", [])
        eco["status"], eco["cache_age_days"] = "cached", round(age, 1)
        res["notes"].append("pypi: %s - result from cache (%.1f days old)" % (error, age))
    res["notes"].extend(notes)
    res["dependencies"]["findings"].extend(findings)


# ------------------------------------------------------------------ Node (npm audit)
def npm_versions(lock_text):
    try:
        d = json.loads(lock_text)
    except ValueError:
        return {}
    out = {}
    for path, info in (d.get("packages") or {}).items():
        if "node_modules/" in path and isinstance(info, dict) and info.get("version"):
            out.setdefault(path.rsplit("node_modules/", 1)[1], set()).add(str(info["version"]))
    for name, info in (d.get("dependencies") or {}).items():
        if isinstance(info, dict) and info.get("version"):
            out.setdefault(name, set()).add(str(info["version"]))
    return {k: sorted(v) for k, v in out.items()}


def npm_parse(data, source, versions=None):
    """npm audit JSON (report version 2) -> findings[]; one advisory per package once."""
    findings, seen, versions = [], set(), versions or {}
    for name, v in ((data or {}).get("vulnerabilities") or {}).items():
        for via in v.get("via") or []:
            if not isinstance(via, dict):
                continue
            m = re.search(r"(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})", str(via.get("url") or ""), re.I)
            vid = m.group(1) if m else "NPM-%s" % via.get("source", "?")
            pkg = str(via.get("name") or name)
            if (pkg, vid) in seen:
                continue
            seen.add((pkg, vid))
            w = via["cvss"].get("score") if isinstance(via.get("cvss"), dict) else None
            lv = _TEXT_LEVEL.get(str(via.get("severity") or v.get("severity") or "").upper()) or level_from_score(w)
            rng = str(via.get("range") or v.get("range") or "")
            findings.append({"ecosystem": "npm", "package": pkg, "version": ", ".join(versions.get(pkg, [])) or rng,
                             "id": vid, "aliases": [], "level": lv, "fixed_in": [],
                             "title": str(via.get("title") or "")[:120], "sources": [source]})
    return findings


def _lock_loop(kind, locks, repo, res, eco, fetch):
    """Shared per-lock-file loop with cache fallback. fetch(rel, content) -> (findings|None, notes, error)."""
    states = []
    for rel in locks:
        try:
            content = open(os.path.join(repo, rel), encoding="utf-8").read()
        except OSError:
            continue
        key = _sha(kind + "\n" + content)
        findings, notes, error = fetch(rel, content)
        if findings is not None:
            cache_write(kind, key, {"findings": findings})
            res["notes"].extend(notes)
            states.append("checked")
        else:
            old, age = cache_read(kind, key)
            if old is None:
                states.append("error")
                res["errors"].append("%s %s: %s" % (kind, rel, error))
                continue
            findings = old.get("findings", [])
            states.append("cached")
            res["notes"].append("%s %s: %s - result from cache (%.1f days old)" % (kind, rel, error, age))
        res["dependencies"]["findings"].extend(findings)
    eco["status"] = "error" if "error" in states else ("cached" if "cached" in states else "checked")


def scan_npm(repo, files, res):
    locks = [d for d in files if os.path.basename(d) == "package-lock.json" and not NOT_SHIPPED.search(d)]
    have = {os.path.dirname(d) for d in locks}
    for d in files:
        if os.path.basename(d) == "package.json" and not NOT_SHIPPED.search(d) and os.path.dirname(d) not in have:
            res["notes"].append("npm %s: no package-lock.json - not checked" % d)
    eco = {"files": locks, "status": "none" if not locks else "checked"}
    res["dependencies"]["ecosystems"]["npm"] = eco
    if not locks:
        return
    tool = _tool(NPM)

    def fetch(rel, content):
        if not tool:
            return None, [], "npm not installed"
        rc, out, err = _run([tool, "audit", "--omit=dev", "--json", "--package-lock-only"],
                            os.path.join(repo, os.path.dirname(rel)))
        try:
            d = json.loads(out) if out.strip() else None
        except ValueError:
            d = None
        if isinstance(d, dict) and "vulnerabilities" in d and not d.get("error"):
            return npm_parse(d, rel, npm_versions(content)), [], ""
        return None, [], "npm audit failed (exit %s): %s" % (rc, (err or "").strip()[-160:])
    _lock_loop("npm", locks, repo, res, eco, fetch)


# ------------------------------------------------------------------ Rust (cargo audit, only if installed)
def cargo_parse(data, source):
    findings = []
    for e in (((data or {}).get("vulnerabilities") or {}).get("list") or []):
        adv, pkg = e.get("advisory") or {}, e.get("package") or {}
        if adv.get("informational"):
            continue
        findings.append({"ecosystem": "cargo", "package": str(pkg.get("name") or ""), "version": str(pkg.get("version") or ""),
                         "id": str(adv.get("id") or "?"), "aliases": [str(a) for a in adv.get("aliases") or []],
                         "level": level_from_score(cvss3_score(adv.get("cvss"))),
                         "fixed_in": list((e.get("versions") or {}).get("patched") or []),
                         "title": str(adv.get("title") or "")[:120], "sources": [source]})
    return findings


def scan_cargo(repo, files, res):
    locks = [d for d in files if os.path.basename(d) == "Cargo.lock" and not NOT_SHIPPED.search(d)]
    eco = {"files": locks, "status": "none"}
    res["dependencies"]["ecosystems"]["cargo"] = eco
    if not locks:
        return
    tool = _tool(CARGO_AUDIT)
    if not tool:
        eco["status"] = "skipped"
        res["notes"].append("cargo: cargo-audit not installed - %d Cargo.lock not checked" % len(locks))
        return

    def fetch(rel, content):
        path = os.path.join(repo, rel)
        for extra in ([], ["--no-fetch", "--stale"]):
            rc, out, err = _run([tool, "audit", "--json", *extra, "--file", path], repo)
            try:
                d = json.loads(out) if rc in (0, 1) and out.strip() else None
            except ValueError:
                d = None
            if d is not None:
                return cargo_parse(d, rel), [], ""
        return None, [], "cargo audit failed (exit %s)" % rc
    _lock_loop("cargo", locks, repo, res, eco, fetch)


# ------------------------------------------------------------------ secrets (whole history)
_PREFILTER = re.compile(r"ghp_|github_pat_|sk-|xox[baprs]-|AKIA|-----BEGIN|eyJ|CLAUDE_CODE_OAUTH_TOKEN|(?i:password|secret|api[_-]?key|token)")


def fingerprint(rule, match):
    return hashlib.sha256((rule + "\0" + match).encode("utf-8", "replace")).hexdigest()[:16]


def _record(found, rule, match, path, commit, line, masked):
    fp = fingerprint(rule, match)
    if fp in found:
        found[fp]["commits"] += 1
        found[fp].update(commit=commit[:12], file=path, line=line)   # log runs new -> old: the end is the oldest
        return
    found[fp] = {"rule": rule, "file": path, "commit": commit[:12], "line": line, "masked": masked,
                 "finding": fp, "commits": 1}


def scan_history(repo, commit="HEAD"):
    """Every added line / created file in the history of `commit` against the BLOCK rules of scan.py."""
    rules = scrub.load_rules()
    text_rules = [(n, rx) for n, sev, rx in rules["rules"] if sev == "block"]
    file_rules = [rx for sev, rx in rules["risky"] if sev == "block"]
    cnt = subprocess.run(["git", "-C", repo, "rev-list", "--count", commit], capture_output=True, text=True)
    if cnt.returncode != 0:
        return {"status": "error", "commits": 0, "findings": [], "error": "commit %s not readable" % commit[:12]}
    p = subprocess.Popen(["nice", "-n", "19", "git", "-C", repo, "-c", "core.quotepath=off", "log", "-p", "--no-color",
                          "--no-ext-diff", "--no-textconv", "--unified=0", "--format=@@WKC@@%H", commit],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    found, cur, path, new, line = {}, "", "", False, 0
    for raw in p.stdout:
        z = raw.decode("utf-8", "replace").rstrip("\n")
        if z.startswith("@@WKC@@"):
            cur, path, new = z[7:], "", False
        elif z.startswith("--- "):
            new = z == "--- /dev/null"
        elif z.startswith("+++ "):
            path = z[6:] if z.startswith("+++ b/") else ""
            if new and path:
                for rx in file_rules:
                    if rx.search(path):
                        _record(found, "risky-file", "file:" + path, path, cur, 0, "file created: " + path)
        elif z.startswith("@@ "):
            m = re.match(r"@@ -\S+ \+(\d+)", z)
            line = int(m.group(1)) if m else 0
        elif z.startswith("+") and path:
            body = z[1:4001]
            if _PREFILTER.search(body):
                for name, rx in text_rules:
                    m = rx.search(body)
                    if m:
                        _record(found, name, m.group(0), path, cur, line, scrub._mask(m.group(0)))
            line += 1
    p.stdout.close()
    p.wait()
    commits = int(cnt.stdout.strip() or 0)
    if p.returncode not in (0, None):
        return {"status": "error", "commits": commits, "findings": [], "error": "git log exit %s" % p.returncode}
    return {"status": "checked", "commits": commits, "findings": list(found.values())}


# ------------------------------------------------------------------ verdict
def check(repo, commit="HEAD", exception_paths=(), mode="enforce", only=None):
    repo = os.path.abspath(repo)
    sha = subprocess.run(["git", "-C", repo, "rev-parse", commit], capture_output=True, text=True).stdout.strip()
    res = {"format": FORMAT, "version": VERSION, "commit": sha or commit, "mode": mode,
           "time": _now().isoformat(timespec="seconds"), "status": "green", "reasons": [], "errors": [], "notes": [],
           "dependencies": {"status": "skipped", "ecosystems": {}, "findings": [], "counts": {s: 0 for s in LEVELS}},
           "secrets": {"status": "skipped", "commits": 0, "findings": []},
           "exceptions": {"valid": 0, "used": 0, "expired": [], "unused": [], "errors": []}}
    excs, exc_errors = load_exceptions(list(exception_paths))
    res["exceptions"]["valid"], res["exceptions"]["errors"] = len(excs), exc_errors
    files = tracked_files(repo)
    if only in (None, "dependencies"):
        n = len(res["errors"])
        for fn in (scan_python, scan_npm, scan_cargo):
            try:
                fn(repo, files, res)
            except Exception as e:  # noqa: BLE001 - a broken sub-scan is an error, not a crash
                res["errors"].append("%s: %s" % (fn.__name__, type(e).__name__))
        dep = res["dependencies"]
        for f in dep["findings"]:
            f["level"] = f.get("level") or "unknown"
            dep["counts"][f["level"]] += 1
            x = _exc_dependency(f, excs)
            f["exception"] = None
            if x:
                x["used"] = True
                f["exception"] = {"reason": x["reason"], "until": x["until"], "expired": x["expired"]}
            f["red"] = f["level"] in RED_LEVELS and not (x and not x["expired"])
        dep["findings"].sort(key=lambda f: (LEVELS.index(f["level"]), f["ecosystem"], f["package"], f["id"]))
        dep["status"] = "error" if len(res["errors"]) > n else "checked"
    if only in (None, "secrets"):
        try:
            g = scan_history(repo, sha or commit)
        except Exception as e:  # noqa: BLE001
            g = {"status": "error", "commits": 0, "findings": [], "error": type(e).__name__}
        for f in g["findings"]:
            x = _exc_secret(f, excs)
            f["exception"] = None
            if x:
                x["used"] = True
                f["exception"] = {"reason": x["reason"], "until": x["until"], "expired": x["expired"]}
            f["red"] = not (x and not x["expired"])
        if g.get("error"):
            res["errors"].append("secrets: " + g["error"])
        res["secrets"] = g
    for x in excs:
        if x["expired"]:
            res["exceptions"]["expired"].append("%s (%s, until %s)" % (x["ident"], x["source"], x["until"]))
        elif not x["used"]:
            res["exceptions"]["unused"].append("%s (%s) - can be removed" % (x["ident"], x["source"]))
        res["exceptions"]["used"] += 1 if x["used"] else 0
    for f in res["dependencies"]["findings"]:
        if f["red"]:
            res["reasons"].append("%s %s %s: %s %s%s" % (f["ecosystem"], f["package"], f["version"], f["level"], f["id"],
                                                         " (exception expired)" if f["exception"] else ""))
    for f in res["secrets"]["findings"]:
        if f["red"]:
            res["reasons"].append("secret %s in %s (commit %s, fingerprint %s)" % (f["rule"], f["file"], f["commit"], f["finding"]))
    res["reasons"] += ["scan failed - " + t for t in res["errors"]]
    res["reasons"] += ["exception list invalid - " + t for t in exc_errors]
    if res["reasons"]:
        res["status"] = "red" if mode == "enforce" else "advisory"
    res["log_line"] = log_line(res)
    return res


def log_line(res):
    a, s, x = res["dependencies"], res["secrets"], res["exceptions"]
    c = a["counts"]
    return ("SECURITY: %s (dependencies %s: red %d, critical %d, high %d, medium %d, low %d, unknown %d; "
            "secrets %s: %d finding(s), red %d in %d commits; exceptions %d valid, %d used, %d expired, %d invalid; commit %s)"
            % (res["status"], a["status"], sum(1 for f in a["findings"] if f["red"]), c["critical"], c["high"], c["medium"],
               c["low"], c["unknown"], s["status"], len(s["findings"]), sum(1 for f in s["findings"] if f["red"]), s["commits"],
               x["valid"], x["used"], len(x["expired"]), len(x["errors"]), (res["commit"] or "?")[:8]))


def report(res):
    out = []
    for f in res["dependencies"]["findings"]:
        if f["level"] in ("critical", "high", "medium") or f["red"]:
            out.append("%s %-8s %s %s %s %s%s" % ("RED" if f["red"] else "   ", f["level"], f["ecosystem"], f["package"], f["version"],
                                                 f["id"], " -> fixed in " + ", ".join(f["fixed_in"][:3]) if f["fixed_in"] else ""))
    small = sum(1 for f in res["dependencies"]["findings"] if f["level"] in ("low", "unknown") and not f["red"])
    if small:
        out.append("    %d low/unknown finding(s) (advisory, list in the JSON)" % small)
    for f in res["secrets"]["findings"]:
        out.append("%s secret %s - %s:%s - commit %s - %s - fingerprint %s%s" % (
            "RED" if f["red"] else "   ", f["rule"], f["file"], f["line"], f["commit"], f["masked"], f["finding"],
            " (exception until %s)" % f["exception"]["until"] if f.get("exception") else ""))
    out += ["    note: " + h[:200] for h in res["notes"][:20]]
    out += ["    exception expired: " + t for t in res["exceptions"]["expired"]]
    out += ["    exception unused: " + t for t in res["exceptions"]["unused"]]
    out += ["    reason: " + t[:240] for t in res["reasons"]]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="check a repo (dependencies + secrets in history)")
    p.add_argument("repo")
    p.add_argument("--commit", default="HEAD")
    p.add_argument("--exceptions", action="append", default=None, metavar="FILE")
    p.add_argument("--json", metavar="OUT")
    p.add_argument("--mode", choices=["enforce", "advisory"], default="enforce")
    p.add_argument("--only", choices=["dependencies", "secrets"])
    a = ap.parse_args(argv)
    if not os.path.isdir(a.repo):
        print("folder missing: %s" % a.repo, file=sys.stderr)
        return 2
    excs = a.exceptions if a.exceptions is not None else [os.path.join(a.repo, "security-exceptions.json")]
    one_core()
    res = check(a.repo, a.commit, excs, a.mode, a.only)
    if a.json:
        tmp = a.json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        os.replace(tmp, a.json)
    for z in report(res):
        print(z)
    print(res["log_line"])
    return 1 if res["status"] == "red" else 0


if __name__ == "__main__":
    sys.exit(main())
