"""Audit a LIVE website by URL. Report only: there is no source code to patch, so nothing is changed and
no fix is claimed as verified. Fix guidance comes from axe-core's own rule text plus fixed advice for runtime errors."""
from __future__ import annotations

import time
from typing import Iterator

from .audit import Auditor, counts
from .pipeline import issue_row, stage

RUNTIME_ADVICE = {
    "js-error": "Open the browser console on this page, find the failing line (shown under Source when known) and guard the value that is undefined or null.",
    "console-error": "A script logged an error. Check the message and the file/line shown, and fix the failing call or remove the stale code.",
    "http-error": "A request returned an error status. Fix the path, restore the missing file, or remove the reference.",
    "request-failed": "A request failed to complete. Check the URL, server availability and CORS configuration.",
    "page-unreachable": "The page did not load. Check that it responds quickly and does not block automated browsers.",
}


def suggestions(issues) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for i in issues:
        g = groups.setdefault((i.kind, i.rule), {"rule": i.rule, "kind": i.kind, "impact": i.impact, "count": 0,
                                                  "pages": set(), "message": i.message[:400], "help_url": i.help_url,
                                                  "example": i.selector or i.html[:160]})
        g["count"] += 1
        g["pages"].add(i.page)
    rank = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
    out = []
    for g in sorted(groups.values(), key=lambda g: (rank.get(g["impact"], 4), -g["count"])):
        g["pages"] = sorted(g["pages"])[:6]
        g["advice"] = RUNTIME_ADVICE.get(g["rule"], "") if g["kind"] == "runtime" else g["message"]
        out.append(g)
    return out


def markdown(url: str, sug: list[dict], c: dict, pages: list[str]) -> str:
    lines = [f"# Audit of {url}", "", f"{c['total']} issue(s) on {len(pages)} page(s): accessibility {c['a11y']}, "
             f"JavaScript {c['js']}, network {c['network']}.", "",
             "> Audit only. SiteMortem did not change anything and these are suggestions, not verified fixes.", "",
             "## Suggested fixes"]
    for g in sug:
        lines += [f"- **{g['rule']}** ({g['impact']}, {g['count']}x): {g['advice']}" + (f" ({g['help_url']})" if g["help_url"] else "")]
    lines += ["", "## Limitations", "- Automated checks cover only part of WCAG.",
              "- External hosts (CDNs, analytics, fonts) are blocked during the audit, so pages that depend on them may look incomplete."]
    return "\n".join(lines)


def run(url: str, pins: dict[str, str], agent=None, deadline_s: float | None = None, should_stop=None) -> Iterator[dict]:
    t0 = time.time()
    yield {"type": "mode", "live": False, "models": {}, "tavily": False}
    yield stage("audit", f"Auditing {url} in headless Chromium (axe-core + runtime errors)", "axe")
    with Auditor(pins=pins) as auditor:
        issues, pages = auditor.audit(url)
        blocked = sorted(auditor.blocked)
    c = counts(issues)
    yield {"type": "audit", "phase": "before", "counts": c, "pages": pages}
    yield {"type": "issues", "issues": [issue_row(i) for i in issues]}
    sug = suggestions(issues)
    yield {"type": "report", "audit_only": True, "site_url": url, "suggestions": sug, "before": c, "after": c, "fixed": 0,
           "total": c["total"], "pages": len(pages), "batches": [], "diff": "", "pr": markdown(url, sug, c, pages),
           "remaining": [issue_row(i) for i in issues][:300], "rejected": [],
           "evidence": {"live": False, "models": {}, "source_unchanged": True, "blocked_external": blocked, "tavily": {},
                        "warnings": [], "elapsed_s": round(time.time() - t0, 1)}}
    yield {"type": "done"}
