"""Live integration check for Nebius Token Factory (Nemotron) and Tavily.

Run this on YOUR machine with real keys in .env:   python scripts/live_check.py

It makes a handful of tiny real calls and prints PASS / FAIL / SKIP for each. It never prints keys.
Paste the output into the chat if anything fails. Nothing here is simulated.
"""
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import tavily  # noqa: E402
from app.llm import Nemotron, TIERS, base_url  # noqa: E402

results = []


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        results.append((name, "PASS", detail or ""))
        print(f"PASS  {name}  {detail or ''}  ({int((time.time() - t0) * 1000)} ms)")
    except SkipCheck as e:
        results.append((name, "SKIP", str(e)))
        print(f"SKIP  {name}  {e}")
    except Exception as e:  # report, never crash
        from app.safety import redact
        results.append((name, "FAIL", redact(e)))
        print(f"FAIL  {name}  {type(e).__name__}: {redact(e)}")


class SkipCheck(Exception):
    pass


print(f"Endpoint: {base_url()}\n")
llm = Nemotron()


def need_key():
    if not llm.live:
        raise SkipCheck("NEBIUS_API_KEY not set")


def t_models():
    need_key()
    ids = llm.list_ids()
    return f"{len(ids)} models on key"


def t_preflight():
    need_key()
    m = llm.preflight()
    return ", ".join(f"{k}={v}" for k, v in m.items())


def tier_check(tier):
    def run():
        need_key()
        llm.preflight()
        out = llm.chat(tier, "Reply with exactly one word.", "Say: ready", f"live check {tier}", max_tokens=400)
        u = llm.log[-1]
        return f"model={u.model} reply={out[:30]!r} tokens={u.tokens if u.tokens is not None else 'n/a (provider sent none)'}"
    return run


def t_json():
    need_key()
    llm.preflight()
    data = llm.chat_json("nano", 'Return JSON {"ok": true, "n": 3}.', "go", "live check json", max_tokens=400)
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError(f"unexpected JSON: {data!r}")
    note = f" (note: {llm.notes[-1]})" if llm.notes else " (response_format=json_object accepted)"
    return "valid JSON" + note


def t_patch_shape():
    need_key()
    llm.preflight()
    sys_p = ('You fix HTML. Return JSON {"edits":[{"file":str,"search":str,"replace":str}],"explanation":str}. '
             "`search` must be an exact substring occurring once.")
    user = "=== FILE: index.html ===\n<html>\n<body><img src=\"a.png\"></body>\n</html>\n\nAdd lang=\"en\" to <html> and alt text to the image."
    out = llm.chat_json("super", sys_p, user, "live check patch", max_tokens=1500)
    from app.validate import validate_edits_shape
    edits = validate_edits_shape(out)
    return f"{len(edits)} well-formed edit(s)"


def t_reasoning():
    need_key()
    llm.preflight()
    out = llm.chat("ultra", "You are concise.", "What is 17*23? Answer with just the number.", "live check ultra reasoning",
                   max_tokens=3000, reasoning=True)
    if "391" not in out:
        raise ValueError(f"unexpected answer {out[:60]!r}")
    return f"answer ok; call took {llm.log[-1].ms} ms (reasoning mode: {os.getenv('NEMOTRON_REASONING_MODE', 'system')})"


def t_tavily():
    if not tavily.available():
        raise SkipCheck("TAVILY_API_KEY not set")
    r = tavily.search("WCAG 2.1 image-alt axe rule how to fix")
    if r.status != "ok":
        raise RuntimeError(f"status={r.status}: {r.reason}")
    return f"{len(r.hits)} result(s), first: {r.hits[0]['url']}"


check("nebius: list models", t_models)
check("nebius: resolve nano/super/ultra", t_preflight)
for t in TIERS:
    check(f"nebius: {t} chat call", tier_check(t))
check("nebius: JSON mode", t_json)
check("nebius: patch-shaped JSON from super", t_patch_shape)
check("nebius: ultra reasoning call", t_reasoning)
check("tavily: search", t_tavily)

print("\nSummary:", {s: sum(1 for _, x, _ in results if x == s) for s in ("PASS", "FAIL", "SKIP")})
sys.exit(1 if any(s == "FAIL" for _, s, _ in results) else 0)
