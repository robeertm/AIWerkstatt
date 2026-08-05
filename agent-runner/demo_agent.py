#!/usr/bin/env python3
"""Demo builder — the zero-config provider. No API key, no network: it turns each
request into a small, real, self-contained web page in the workspace so a fresh
`docker compose up` produces a working live app immediately (and powers the
end-to-end self-test). Speaks the same stream-json shape the Claude adapter parses.
"""
import json
import os
import sys
import time

SESSION = "demo-%d" % (time.time_ns() % 1_000_000)


def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def page(request, n):
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Built by AIWerkstatt</title>
<style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:ui-rounded,system-ui,sans-serif;
background:radial-gradient(1200px 600px at 20% -10%,#6e56cf33,transparent),#0f1020;color:#e5e7eb}}
.card{{max-width:34rem;margin:2rem;padding:2.2rem;border-radius:20px;background:#161a2e;
border:1px solid #ffffff18;box-shadow:0 20px 60px #0008}}
h1{{margin:.2rem 0 .6rem;font-size:1.6rem}}
.req{{padding:.9rem 1rem;border-radius:12px;background:#0f1020;border:1px solid #ffffff14;color:#c7d2fe;margin:.8rem 0 1.4rem}}
button{{font:inherit;font-weight:700;border:0;border-radius:12px;padding:.7rem 1.1rem;cursor:pointer;
background:#6e56cf;color:#fff}}
.count{{font-size:2.4rem;font-weight:800;margin:.4rem 0;font-variant-numeric:tabular-nums}}
.foot{{margin-top:1.4rem;opacity:.6;font-size:.8rem}}
</style></head>
<body><div class="card">
<div style="font-size:2rem">🛠️</div>
<h1>Your AIWerkstatt demo app</h1>
<div>You asked for:</div>
<div class="req">{req}</div>
<div class="count" id="c">0</div>
<button onclick="document.getElementById('c').textContent=++window.n">Tap me</button>
<script>window.n=0</script>
<div class="foot">Built live in a container · revision {n} · connect a real AI provider for the full experience.</div>
</div></body></html>""".format(req=(request or "a little demo").replace("<", "&lt;")[:300], n=n)


def main():
    emit({"type": "system", "subtype": "init", "session_id": SESSION})
    rev = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        content = (msg.get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        # ignore control messages like /compact
        if str(content).strip() == "/compact":
            emit({"type": "result", "subtype": "success", "result": "Context compacted.",
                  "usage": {"output_tokens": 1}, "total_cost_usd": 0.0})
            continue
        rev += 1
        try:
            with open("index.html", "w", encoding="utf-8") as f:
                f.write(page(content, rev))
        except OSError as e:
            emit({"type": "result", "is_error": True, "subtype": "error",
                  "result": "Could not write the app: %s" % e})
            continue
        emit({"type": "assistant", "message": {"usage": {
            "input_tokens": 400, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})
        emit({"type": "result", "subtype": "success",
              "result": "Built a small demo page for your request and deployed it live. "
                        "Open the live preview to see it. Connect a real AI provider for full apps.",
              "usage": {"output_tokens": 30}, "total_cost_usd": 0.0,
              "modelUsage": {"demo": {"contextWindow": 200000}}})


if __name__ == "__main__":
    main()
