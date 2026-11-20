"""Reading proof (opt-in): parse and check the ``READ: <files>`` line of a result.

The web container puts a reading list into the first message of a new run (``context_pack.reading_list``) and hands
the stems (file names without ``.md``) to the runner. A result is valid when it carries a ``READ:`` line that names at
least one of those places; without any places every line with content counts (also ``READ: none - <reason>``). The
template ``READ: <file1>, ...`` does not count. Standard library only.
"""
import re

READ_RX = re.compile(r"(?im)^[ \t>*_`#-]*READ[ \t*_`]*:[ \t*_`]*(.+?)[ \t*_`]*$")


def _norm(s):
    return re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())


def parse_read_line(text):
    """Last ``READ:`` line of a result -> list of the names it mentions, or None (no line, empty or template)."""
    found = READ_RX.findall(text or "")
    if not found:
        return None
    raw = found[-1].strip()
    if not raw or raw.strip("`*_ -—–") == "" or "<file" in raw.lower():
        return None
    parts = [p.strip(" `*_\t\"'") for p in re.split(r"[,;]|\s{2,}|\s[|·]\s", raw)]
    return [p for p in parts if p] or None


def check(text, stems=()):
    """-> (valid, entries|None). With places (``stems``) at least one of them must be named."""
    entries = parse_read_line(text)
    if not entries:
        return False, None
    wanted = [_norm(x) for x in (stems or ()) if _norm(x)]
    if not wanted:
        return True, entries
    named = _norm(" ".join(entries))
    return (True, entries) if any(w in named for w in wanted) else (False, entries)


def nudge_text(paths):
    """The single follow-up that asks for the proof."""
    listing = ("\n".join("- `%s`" % p for p in list(paths)[:10])) if paths else \
        "- (no places named: search the knowledge and memory folders yourself)"
    return ("Reading proof missing: read first, then confirm.\n\n"
            "Your result has no valid line `READ: <files>`. The rule is: read the knowledge base first, then act. "
            "Read the places from the head of your order now, really (open them, do not rely on the excerpts):\n%s\n\n"
            "Then check briefly whether anything you read changes your work and give your complete result once more, "
            "with a line of its own `READ: <the files you really read>`. Do not redo finished work and do not abort "
            "anything." % listing)
