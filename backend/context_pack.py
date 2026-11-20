"""Context pack: the relevant slice of the knowledge base, inserted into a new run's first message.

Asking an agent to "please read the knowledge base" does not reliably make it read it. So the
relevant parts are inserted directly instead:

  1. the project's own ``<vault>/<slug>/knowledge.md`` (always),
  2. optional global knowledge folder (``AIWERKSTATT_KNOWLEDGE_DIR``, markdown master documents): files whose
     normalised name starts with the project slug, otherwise files matching keywords from the order title,
  3. optional memory folder (``AIWERKSTATT_MEMORY_DIR``): ``MEMORY.md`` is NOT inserted, but the
     ``index_*.md`` that best matches the slug may contribute its newest ``- [`` lines.

From each document: the head (without YAML front matter), the 3 newest dated ``## `` sections (a date such
as YYYY-MM-DD or DD.MM.YYYY anywhere in the heading; documents may be newest-first or newest-last) and one
"pitfalls"/"gotchas" section (``##`` or ``###``) - each with path and line number so the agent can read on.
The size is capped hard (12000 bytes of UTF-8). Sections that look like they contain a secret are omitted
with a note instead of their content. Without any matching master document the pack says so explicitly.

``reading_list()`` (opt-in, ``AIW_READING_PROOF=1``) names the places the agent has to read in full before it acts
and asks for a ``READ: <files>`` line in the result; the runner checks that line (``agent-runner/reading_proof.py``).

Standard library only. ``build()`` returns ``(text, meta)``.
"""
from __future__ import annotations

import math
import os
import re

MAX_BYTES = 12000
MAX_HEAD = 2000          # characters of a document head
MAX_SECTION = 2600       # characters of one section
NEWEST = 3               # dated sections per document
MAX_DOCS = 2             # global master documents per pack
INDEX_ENTRIES = 12

NO_MATCH = ("No master document found - search the knowledge folder yourself and say in your result "
            "which note you used.")

SECRET_RX = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gh[ousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{10,}"
    r"|sk-[A-Za-z0-9]{32,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}"
    r"|\b(?:token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[\"']?[A-Za-z0-9+/_\-]{24,})", re.I)

DATE_RX = [
    (re.compile(r"(20\d\d)-(\d\d)-(\d\d)"), (1, 2, 3)),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b"), (3, 2, 1)),
]
PITFALL_RX = re.compile(r"pitfall|gotcha", re.I)
STOPWORDS = {"this", "that", "with", "from", "into", "have", "please", "build", "make", "fix", "update",
             "add", "the", "and", "for", "new", "app", "task", "order", "project"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def looks_secret(text: str) -> bool:
    return bool(SECRET_RX.search(text or ""))


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _date(text: str):
    """Newest date in a heading as (y, m, d) or None."""
    best = None
    for rx, (iy, im, idd) in DATE_RX:
        for m in rx.finditer(text):
            try:
                d = (int(m.group(iy)), int(m.group(im)), int(m.group(idd)))
            except (ValueError, IndexError):
                continue
            if 1 <= d[1] <= 12 and 1 <= d[2] <= 31 and (best is None or d > best):
                best = d
    return best


def _strip_front_matter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def sections(text: str) -> list[dict]:
    """``## `` sections: [{title, start (1-based line), text}]. The head (before the first ``##``) has title ''."""
    out, cur, in_code = [], {"title": "", "start": 1, "lines": []}, False
    for i, line in enumerate(text.split("\n"), 1):
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and line.startswith("## "):
            out.append(cur)
            cur = {"title": line[3:].strip(), "start": i, "lines": []}
        cur["lines"].append(line)
    out.append(cur)
    for s in out:
        lines = s.pop("lines")
        s["text"] = "\n".join(lines[1:] if s["title"] else lines).strip()
    return [s for s in out if s["text"]]


def select(text: str, newest: int = NEWEST) -> list[tuple[str, dict, int]]:
    """Relevant parts of a document: [(kind, section, char_limit)] - head, newest dated, one pitfalls section."""
    secs = sections(text)
    head = next((s for s in secs if s["title"] == ""), None)
    dated = [(_date(s["title"]), i, s) for i, s in enumerate(secs) if s["title"] and _date(s["title"])]
    # newest-first documents list their dates descending, newest-last ones ascending: break ties accordingly
    newest_first = len(dated) > 1 and dated[0][0] >= dated[-1][0]
    dated.sort(key=lambda x: (x[0], -x[1] if newest_first else x[1]), reverse=True)
    picked = [s for _d, _i, s in dated[:newest]]
    pit = next((s for s in secs if s["title"] and PITFALL_RX.search(s["title"]) and s not in picked), None)
    if pit is None:     # a "### Pitfalls" subsection inside a bigger section counts too
        for s in secs:
            m = re.search(r"^### [^\n]*(?:pitfall|gotcha)[^\n]*\n", s["text"], re.M | re.I)
            if m and s not in picked:
                before = s["text"][:m.start()].count("\n")
                rest = s["text"][m.start():]
                end = re.search(r"^#{2,3} ", rest[4:], re.M)
                pit = {"title": s["title"] + " > " + m.group(0)[4:].strip(), "start": s["start"] + before + 1,
                       "text": rest[:end.start() + 4] if end else rest}
                break
    parts: list[tuple[str, dict, int]] = []
    if head:
        t = _strip_front_matter(head["text"])
        if t:
            parts.append(("head", dict(head, text=t), MAX_HEAD))
    parts += [("newest", s, MAX_SECTION) for s in picked]
    if pit:
        parts.append(("pitfalls", pit, MAX_SECTION))
    return parts


def _clip(text: str, limit: int, path: str, line: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (truncated — rest in %s from line %d)" % (path, line)


def find_docs(knowledge_dir: str, slug: str, title: str = "") -> tuple[list[str], str]:
    """Matching markdown files in the global knowledge folder -> ([file names], how)."""
    try:
        files = sorted(f for f in os.listdir(knowledge_dir)
                       if f.lower().endswith(".md") and not f.startswith("."))
    except OSError:
        return [], ""
    n = norm(slug)
    if len(n) >= 3:
        hit = [f for f in files if norm(os.path.splitext(f)[0]).startswith(n)]
        if hit:
            return hit[:MAX_DOCS], "name"
    words = [w for w in re.findall(r"[a-z0-9]{4,}", (title or "").lower()) if w not in STOPWORDS]
    scored = []
    for f in files:
        stem = norm(os.path.splitext(f)[0])
        score = sum(1 for w in set(words) if w in stem)
        if score:
            scored.append((-score, f))
    scored.sort()
    return [f for _s, f in scored[:MAX_DOCS]], ("title" if scored else "")


def find_index(memory_dir: str, slug: str, title: str = "") -> str:
    """The ``index_*.md`` whose name/contents match the slug best ('' if none)."""
    try:
        names = sorted(f for f in os.listdir(memory_dir) if f.startswith("index_") and f.endswith(".md"))
    except OSError:
        return ""
    words = [w for w in re.findall(r"[a-z0-9-]{3,}", (slug or "").lower())][:4]
    if not words:
        return ""
    n = norm(slug)
    best, best_score = "", 0
    for f in names:
        score = 0
        if n and n in norm(f):
            score += 1000
        text = _read(os.path.join(memory_dir, f)).lower()
        score += sum(text.count(w) for w in words)
        if score > best_score:
            best, best_score = f, score
    return best


def index_excerpt(text: str, limit: int, n: int = INDEX_ENTRIES) -> str:
    out, used = [], 0
    for line in [x for x in text.split("\n") if x.startswith("- [")][:n]:
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def build(slug: str, title: str = "", vault_dir: str = "", knowledge_dir: str = "", memory_dir: str = "",
          max_bytes: int = MAX_BYTES) -> tuple[str, dict]:
    """-> (text, meta). ``vault_dir`` = the project's own folder (holds ``knowledge.md``).

    meta: docs (paths used), index (file name or ''), bytes, no_match, omitted (secret-like sections), sections.
    """
    meta = {"docs": [], "index": "", "bytes": 0, "no_match": False, "omitted": 0, "sections": []}
    budget = int(max_bytes * 0.94)          # umlauts/dashes cost 2-3 bytes; the hard byte cut is below
    parts = ["Knowledge selected for this order (read-only excerpts). Read the full section before changing "
             "a system."]
    sources = []                            # (path, how)
    own = os.path.join(vault_dir, "knowledge.md") if vault_dir else ""
    if own and os.path.isfile(own):
        sources.append((own, "project knowledge"))
    global_hit = False
    if knowledge_dir and os.path.isdir(knowledge_dir):
        names, how = find_docs(knowledge_dir, slug, title)
        for f in names:
            sources.append((os.path.join(knowledge_dir, f), "master document, matched by %s" % how))
            global_hit = True
    idx = find_index(memory_dir, slug, title) if memory_dir and os.path.isdir(memory_dir) else ""
    share = int(budget * (0.78 if idx else 1.0))
    used = len(parts[0])
    truncated_at = None
    for path, how in sources:
        text = _read(path)
        if not text.strip():
            continue
        meta["docs"].append(path)
        block = ["### %s: %s (%d KB, %s)" % ("Document", path, max(1, len(text) // 1024), how)]
        for kind, sec, limit in select(text):
            line = sec["start"]
            if looks_secret(sec["text"]):
                meta["omitted"] += 1
                block.append("#### %s (line %d) — omitted: looks like it contains a secret. Read it yourself in "
                             "%s if needed, never print it." % (sec["title"] or "head", line, path))
                continue
            room = min(limit, share - used - sum(len(b) + 2 for b in block) - 120)
            if room < 300:
                truncated_at = (path, line)
                break
            block.append("#### [%s] %s (%s, line %d)\n%s"
                         % (kind, sec["title"] or "introduction", path, line, _clip(sec["text"], room, path, line)))
            meta["sections"].append("%s:%d" % (os.path.basename(path), line))
        if truncated_at:
            block.append("… (truncated — rest in %s from line %d)" % truncated_at)
        blk = "\n\n".join(block)
        parts.append(blk)
        used += len(blk) + 2
        if truncated_at:
            break
    if idx:
        meta["index"] = idx
        path = os.path.join(memory_dir, idx)
        head = "### Memory index: %s (newest entries)" % path
        room = budget - used - len(head) - 10
        t = index_excerpt(_read(path), max(0, room))
        if looks_secret(t):
            meta["omitted"] += 1
            t = "(omitted: looks like it contains a secret — read it yourself)"
        if t:
            parts.append(head + "\n" + t)
    if not meta["docs"] or (knowledge_dir and not global_hit):
        meta["no_match"] = True
        parts.append(NO_MATCH)
    text = "\n\n".join(parts)
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        note = "\n… (truncated — rest in the paths named above)"
        text = raw[:max_bytes - len(note.encode("utf-8"))].decode("utf-8", errors="ignore").rstrip() + note
    meta["bytes"] = len(text.encode("utf-8"))
    return text, meta


# ---------------------------------------------------------------- reading list (opt-in reading proof)
# The pack above inserts excerpts. The reading list names the PLACES the agent has to read in full before it acts
# and asks for a `READ: <files>` line in the result. Pure release-train fixes and tests only need the project's
# master document (no memory pass).
LIGHT_RX = re.compile(
    r"(?i)^\s*release[- ]train\b|\b(?:red gate|gate (?:is )?red)\b|^\s*(?:[\w-]+-)?tests?\s*[:\u00b7]"
    r"|\b(?:tests? only|only tests?)\b|^\s*tests?\b[^\n]{0,30}\b(?:repair|fix)")
READING_STOPWORDS = STOPWORDS | {
    "about", "after", "before", "should", "could", "would", "there", "their", "which", "while", "where",
    "these", "those", "other", "using", "without", "within", "every", "again", "still", "also", "only",
    "nicht", "oder", "auch", "eine", "einer", "einem", "einen", "eines", "nach", "statt", "ohne", "beim",
    "sind", "wird", "werden", "wurde", "mehr", "alle", "alles", "dass", "damit", "dann", "noch", "sowie",
    "sonst", "ueber", "fertig", "neue", "neuer", "neues", "neuen"}
MAX_MEMORY_HITS = 4
READ_MARK = "READ:"


def is_light(title: str) -> bool:
    """Exception to the reading duty: a pure release-train fix or test order -> only the project's master document."""
    return bool(LIGHT_RX.search(title or ""))


def title_words(title: str, n: int = 8) -> list[str]:
    """Key words of an order title (lower case, 5+ characters, no filler words, order kept)."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9\u00e4\u00f6\u00fc\u00df][a-z0-9\u00e4\u00f6\u00fc\u00df-]{3,}", (title or "").lower()):
        w = w.strip("-")
        if len(w) >= 5 and w not in READING_STOPWORDS and w not in out:
            out.append(w)
    return out[:n]


def memory_hits(memory_dir: str, title: str, n: int = MAX_MEMORY_HITS, exclude=()) -> list[str]:
    """Single memory files (not ``MEMORY.md``, not an index) that match key words of the title, best first.

    Score: a word in the file name counts 3, in the file head (front matter + first 700 characters) 1, each weighted
    by the word's rarity. A word that occurs in more than 30 % of the files (and in more than 3) does not count."""
    words = title_words(title)
    if not words or not memory_dir or not os.path.isdir(memory_dir):
        return []
    try:
        names = sorted(f for f in os.listdir(memory_dir) if f.endswith(".md") and f != "MEMORY.md"
                       and not f.startswith("index_") and not f.startswith("."))
    except OSError:
        return []
    if not names:
        return []
    heads = {f: _read(os.path.join(memory_dir, f))[:1100].lower() for f in names}
    total = len(names)
    weight: dict[str, float] = {}
    for w in words:
        df = sum(1 for f in names if w in f.lower() or w in heads[f])
        if 0 < df <= max(3, int(total * 0.30)):
            weight[w] = math.log(1 + total / df)
    scored = []
    for f in names:
        if f in exclude:
            continue
        score, hits = 0.0, 0
        for w, g in weight.items():
            if w in f.lower():
                score, hits = score + 3 * g, hits + 1
            elif w in heads[f]:
                score, hits = score + g, hits + 1
        if hits >= 2 or (hits == 1 and score >= 3 * 1.5):
            try:
                mtime = os.path.getmtime(os.path.join(memory_dir, f))
            except OSError:
                mtime = 0
            scored.append((score, mtime, f))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [f for _s, _m, f in scored[:n]]


def reading_list(slug: str, title: str = "", knowledge_dir: str = "", memory_dir: str = "") -> tuple[str, dict]:
    """Places the agent must read before it acts -> (text, meta). Never raises.

    meta: required (always True), light, paths (as shown to the agent), stems (file names without ``.md``, lower
    case - checked against the ``READ:`` line), docs, index, memory, none (no place found). Without a knowledge or
    memory folder the list is empty and the ``READ:`` line is still required."""
    light = is_light(title)
    meta = {"required": True, "light": light, "paths": [], "stems": [], "docs": [], "index": "", "memory": [],
            "none": False}
    places: list[tuple[str, str]] = []        # (path, why)
    try:
        docs = find_docs(knowledge_dir, slug, title)[0] if knowledge_dir and os.path.isdir(knowledge_dir) else []
    except Exception:    # noqa - a broken folder must never block a run
        docs = []
    for f in docs:
        places.append((os.path.join(knowledge_dir, f), "master document"))
        meta["docs"].append(f)
    if not light:
        idx = ""
        try:
            idx = find_index(memory_dir, slug, title) if memory_dir and os.path.isdir(memory_dir) else ""
        except Exception:    # noqa
            idx = ""
        if idx:
            meta["index"] = idx
            places.append((os.path.join(memory_dir, idx), "memory topic index"))
        try:
            hits = memory_hits(memory_dir, title, exclude=(idx,))
        except Exception:    # noqa
            hits = []
        for f in hits:
            meta["memory"].append(f)
            places.append((os.path.join(memory_dir, f), "memory note matching words of the title"))
    meta["paths"] = [p for p, _w in places]
    meta["stems"] = [os.path.splitext(os.path.basename(p))[0].lower() for p in meta["paths"]]
    meta["none"] = not places
    lines = ["## Reading duty - read first, act second, prove it at the end", ""]
    if places:
        lines.append("Places to read in full before you act (not only the excerpts below):" if not light else
                     "Place to read (pure release-train fix or test order: only the project's master document, "
                     "no full memory pass):")
        lines += ["%d. `%s` - %s" % (i, p, w) for i, (p, w) in enumerate(places, 1)]
    else:
        lines.append("No master document or topic index matched this order. Search the knowledge and memory "
                     "folders yourself and read what fits.")
    lines += ["",
              "**Proof:** put a line of its own into your result: `%s <file1>, <file2>, ...` naming the files you "
              "really read%s. If the line is missing, the workshop does not treat the order as finished but sends "
              "one follow-up (\"Reading proof missing: read first, then confirm.\") - nothing is aborted."
              % (READ_MARK, " (nothing fitting: `READ: none - <reason>`)" if not places else "")]
    return "\n".join(lines), meta
