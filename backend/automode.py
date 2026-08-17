"""🤖 Auto mode — pick a model + intensity per task, automatically.

When a project's model is set to ``auto``, the engine calls ``decide()`` for every
task (a new order OR a follow-up): it reads simple signals from the request (length,
bug/analysis/refactor keywords, breadth, follow-up-vs-new) and picks a concrete model
+ reasoning effort from the chosen provider's model list — plus a short human-readable
reason and a small plan, so the UI can show WHAT was picked and WHY. Because it decides
per task, a workshop thread effectively switches models over its lifetime: the big first
order gets the strongest model, a tiny "make the button blue" follow-up drops to a cheap
one.

Deliberately rule-based/transparent: no second AI round (no latency, no tokens spent just
to decide, always inspectable). The ``decide(...) -> dict`` interface is stable if an LLM
planner ever replaces the heuristic. It is provider-agnostic: tiers map onto whatever
models the selected provider exposes (strongest → weakest), so it works for any provider.
"""
import providers

AUTO_MODEL = "auto"

# Signal words (lowercased, matched as substrings).
_HARD_WORDS = [
    "bug", "error", "crash", "500", "exception", "traceback", "stack trace",
    "race", "deadlock", "hangs", "freezes", "leak", "security", "vulnerab",
    "refactor", "architecture", "migrate", "migration", "debug", "why does",
    "analyse", "analyze", "performance", "slow", "optimi", "broken", "regression",
    "flaky", "timeout", "doesn't work", "does not work", "not working",
]
_BREADTH_WORDS = [
    "complete", "entire", "everything", "all of", "everywhere", "end-to-end",
    "from scratch", "rebuild", "throughout", "whole",
]
_TRIVIAL_WORDS = [
    "typo", "spelling", "label", "caption", "colour", "color", "rename",
    "wording", "text change", "cosmetic", "tiny", "padding", "spacing",
    "swap icon", "move the", "tweak",
]

_TIERS = ["trivial", "light", "standard", "hard", "max"]


def _ladder(spec):
    """From a provider spec build (strong, mid, weak) model ids + effort picks.
    Falls back gracefully for providers with 1–2 real models."""
    # Skip the synthetic "auto" entry itself when ranking real models.
    models = [m.id for m in spec.models if m.id != AUTO_MODEL] if spec else []
    if not models:
        return None
    strong = models[0]
    weak = models[-1]
    mid = models[1] if len(models) >= 2 else models[0]
    efforts = list(spec.efforts) if spec and spec.efforts else []

    def eff(*prefs):
        for p in prefs:
            if p in efforts:
                return p
        return efforts[-1] if efforts else ""
    return {
        "trivial":  (weak,   eff("low")),
        "light":    (mid,    eff("low", "medium")),
        "standard": (mid,    eff("high", "medium")),
        "hard":     (strong, eff("high", "xhigh")),
        "max":      (strong, eff("max", "xhigh", "high")),
    }


def decide(provider: str = "claude", prompt: str = "", kind: str = "new", files=None) -> dict:
    """Pick model + effort for ONE task. Never raises — falls back to the provider default."""
    spec = providers.get_spec(provider)
    ladder = _ladder(spec)
    if not ladder:
        dm = spec.default_model if spec else ""
        return {"model": dm, "effort": "high", "tier": "standard", "score": 0,
                "reason": "provider default", "signals": [], "model_label": dm,
                "effort_label": "high", "plan": None}

    text = (prompt or "").lower()
    files = files or []
    n = len(text)
    signals = []
    score = 0

    if n > 1600:
        score += 3; signals.append("very large request")
    elif n > 800:
        score += 2; signals.append("large request")
    elif n > 280:
        score += 1; signals.append("medium request")

    hard_hits = sum(1 for w in _HARD_WORDS if w in text)
    if hard_hits:
        score += min(3, 1 + hard_hits); signals.append("bug/analysis signal")
    if any(w in text for w in _BREADTH_WORDS):
        score += 1; signals.append("broad scope")
    if files:
        score += 1; signals.append("attachment to inspect")
    if any(w in text for w in _TRIVIAL_WORDS):
        score -= 3; signals.append("tiny change")
    if kind and kind != "new":
        score -= 1; signals.append("follow-up (refinement)")

    # Mid (0..1) = standard: ordinary work stays on the middle model; save on trivial
    # tasks, escalate to the strongest for hard/large ones.
    if score <= -3:
        tier = "trivial"
    elif score <= -1:
        tier = "light"
    elif score <= 1:
        tier = "standard"
    elif score <= 4:
        tier = "hard"
    else:
        tier = "max"

    model, effort = ladder[tier]
    label_of = {m.id: m.label for m in spec.models}
    ml = label_of.get(model, model).split(" (")[0]
    el = effort or "default"
    why = ", ".join(dict.fromkeys(signals)) or "average request"
    reason = f"{why} → {ml} / {el}"

    if tier in ("hard", "max"):
        steps = [f"🧠 Understand & plan — {ml} / {el}",
                 f"🔨 Build — {ml} / {el}",
                 f"✅ Verify — {ml} / {el}"]
    elif tier == "standard":
        steps = [f"🔧 Build — {ml} / {el}", f"✅ Quick check — {ml} / {el}"]
    else:
        steps = [f"⚡ Do it directly — {ml} / {el}"]

    return {
        "model": model, "effort": effort, "tier": tier, "score": score,
        "reason": reason, "signals": list(dict.fromkeys(signals)),
        "model_label": ml, "effort_label": el,
        "plan": {"tier": tier, "steps": steps,
                 "note": "Follow-up questions are graded again by size "
                         "(small changes → a cheaper model)."},
    }
