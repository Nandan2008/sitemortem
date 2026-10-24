"""SiteMortem pipeline.

audit (Chromium + axe-core)  ->  triage (Nano, parallel)  ->  for each issue group:
    diagnose (Ultra)  ->  patch (Super)  ->  apply  ->  RE-AUDIT  ->  keep only if verified
    (otherwise revert and retry once with Ultra)  ->  report + PR text.

A fix is kept only when the same audit that found the problem no longer finds it AND the
audit finds nothing new. Everything the model proposes is checked by software, not trusted.
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import tavily
from .audit import Auditor, Issue, category, counts, _norm
from .llm import ModelError
from .safety import redact
from .repo import PatchError, SiteRepo
from .siteserver import SiteServer

MAX_BATCHES = 12
KIND_RANK = {"runtime": 0, "a11y": 1}
RULE_RANK = {"page-unreachable": 0, "request-failed": 1, "http-error": 1, "js-error": 2, "console-error": 3}
IMPACT_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


@dataclass
class Batch:
    id: str
    kind: str
    rule: str
    issues: list[Issue]
    title: str = ""
    triage: dict = field(default_factory=dict)
    status: str = "detected"
    attempts: int = 0

    @property
    def fps(self) -> set[str]:
        return {i.fingerprint for i in self.issues}


def make_batches(issues: list[Issue]) -> list[Batch]:
    groups: dict[tuple, list[Issue]] = {}
    for i in issues:
        key = (i.kind, i.rule) if i.kind == "a11y" else (i.kind, i.rule, _norm(i.message))
        groups.setdefault(key, []).append(i)
    batches = []
    for key, items in groups.items():
        first = items[0]
        title = (f"{first.rule} · {len(items)} element(s)" if first.kind == "a11y"
                 else f"{first.rule} · {first.message[:70]}")
        batches.append(Batch("", first.kind, first.rule, items, title))
    return batches


def order(batches: list[Batch]) -> list[Batch]:
    batches.sort(key=lambda b: (KIND_RANK[b.kind], RULE_RANK.get(b.rule, 9),
                                IMPACT_RANK.get(b.issues[0].impact, 4), b.triage.get("priority", 3)))
    for n, b in enumerate(batches, 1):
        b.id = f"B{n}"
    return batches


def issue_row(i: Issue) -> dict:
    return {"id": i.id, "kind": i.kind, "category": category(i), "rule": i.rule, "impact": i.impact, "page": i.page,
            "selector": i.selector, "message": i.message[:400], "html": i.html[:300], "source": i.source,
            "help_url": i.help_url}


def diff_between(before: dict[str, str], repo: SiteRepo, rels: list[str]) -> str:
    out = []
    for rel in rels:
        a = before.get(rel, "").splitlines(keepends=True)
        b = repo.read(rel).splitlines(keepends=True)
        out += list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=2))
    return "".join(out)


def stage(key, label, engine):
    return {"type": "stage", "key": key, "label": label, "engine": engine}


def run(source: Path, agent, deadline_s: float | None = None, should_stop=None) -> Iterator[dict]:
    repo = SiteRepo(source)
    try:
        h0 = repo.source_hash()
        deadline = time.time() + deadline_s if deadline_s else None
        with SiteServer(repo.work) as server, Auditor() as auditor:
            yield from _run(repo, server, auditor, agent, h0, deadline, should_stop)
    finally:
        repo.cleanup()


def template_pr(summary: dict, reason: str) -> str:
    """Deterministic PR text used only when the model could not write it. Says so explicitly."""
    b, a = summary["before"], summary["after"]
    lines = [f"# fix: resolve {summary['fixed']} of {summary['total']} accessibility and runtime issues", "",
             f"> Generated from the verified results without a model ({reason}).", "", "## Summary",
             f"Issues went from {b['total']} to {a['total']} (accessibility {b['a11y']} -> {a['a11y']}, "
             f"runtime {b['runtime']} -> {a['runtime']}) across {summary['pages']} page(s).", "", "## Changes"]
    lines += [f"- {c['title']}: {c['status']}" for c in summary["batches"]]
    lines += ["", "## Limitations", "- Automated checks cover only part of WCAG; review generated labels and text."]
    return "\n".join(lines)


def _run(repo: SiteRepo, server: SiteServer, auditor: Auditor, agent, h0: str, deadline: float | None,
         should_stop=None) -> Iterator[dict]:
    t_start = time.time()
    warnings: list[str] = []
    yield {"type": "mode", "live": agent.live, "models": agent.models, "tavily": tavily.available()}

    # ------------------------------------------------------------ 1. audit
    yield stage("audit", "Auditing the site in headless Chromium (axe-core + runtime errors)", "axe")
    issues, pages = auditor.audit(server.url)
    before = counts(issues)
    yield {"type": "audit", "phase": "before", "counts": before, "pages": pages}
    yield {"type": "issues", "issues": [issue_row(i) for i in issues]}
    if not issues:
        yield {"type": "report", "before": before, "after": before, "fixed": 0, "total": 0, "pages": len(pages),
               "batches": [], "diff": "", "pr": "No issues found.", "remaining": [], "rejected": [],
               "evidence": {"live": agent.live, "models": agent.models, "source_unchanged": repo.source_hash() == h0,
                            "blocked_external": sorted(auditor.blocked), "tavily": {}, "warnings": [],
                            "elapsed_s": round(time.time() - t_start, 1)}}
        yield {"type": "done"}
        return

    # ------------------------------------------------------------ 2. triage
    batches = make_batches(issues)
    yield stage("triage", f"Triaging {len(batches)} issue groups in parallel", "nano")
    agent.triage_all(batches)           # raises on fatal model errors (bad key, no credits ...)
    n_fallback = sum(1 for b in batches if b.triage.get("source") == "fallback")
    if n_fallback:
        warnings.append(f"Triage fell back to deterministic labels for {n_fallback} group(s) because the model "
                        f"call failed or returned invalid JSON.")
    batches = order(batches)
    if len(batches) > MAX_BATCHES:
        warnings.append(f"{len(batches) - MAX_BATCHES} lower-priority group(s) were not attempted (limit {MAX_BATCHES}).")
    batches = batches[:MAX_BATCHES]
    for b in batches:
        b.status = "triaged"
        yield {"type": "batch", "id": b.id, "title": b.title, "kind": b.kind, "rule": b.rule, "status": "triaged",
               "issue_ids": [i.id for i in b.issues], "triage": b.triage}

    # ------------------------------------------------------------ 3. fix loop
    current, open_fps = issues, {i.fingerprint for i in issues}
    fp2id = {i.fingerprint: i.id for i in issues}  # ids stay stable across re-audits
    log, rejected, tstats = [], [], {}
    stop = {"why": ""}

    def late() -> bool:
        if should_stop and should_stop():
            stop["why"] = "cancelled"
        elif deadline and time.time() > deadline:
            stop["why"] = stop["why"] or "time"
        return bool(stop["why"])

    for b in batches:
        if late():
            b.status = "skipped"
            yield {"type": "batch", "id": b.id, "status": "skipped",
                   "detail": "Run stopped by the user before this group." if stop["why"] == "cancelled"
                   else "Run time limit reached before this group."}
            log.append({"title": b.title, "status": "skipped", "attempts": []})
            continue
        targets = [i for i in b.issues if i.fingerprint in open_fps]
        if not targets:  # already cured by an earlier fix
            b.status = "fixed"
            yield {"type": "batch", "id": b.id, "status": "fixed", "detail": "Resolved as a side effect of an earlier fix."}
            log.append({"title": b.title, "status": "fixed as a side effect", "attempts": []})
            continue
        b.issues = targets
        feedback, done, guide, attempts = "", False, "", []
        for attempt in (1, 2):                                  # explicit, bounded retry policy: 2 attempts
            if late():
                break
            b.attempts = attempt
            tier = "super" if attempt == 1 else "ultra"
            rels = repo.locate(b.issues)
            if agent.live and attempt == 1:
                res = tavily.search(_guidance_query(b))
                tstats[res.status] = tstats.get(res.status, 0) + 1
                guide = tavily.as_reference_block(res.hits)
                yield {"type": "intel", "id": b.id, "status": res.status, "reason": res.reason, "sources": res.hits}
            diagnosis = None
            if agent.live and (b.triage.get("complexity") == "complex" or b.kind == "runtime" or attempt > 1):
                yield {"type": "batch", "id": b.id, "status": "diagnosing", "engine": "ultra", "attempt": attempt}
                try:
                    diagnosis = agent.diagnose(b, repo.context(rels), guide, feedback)
                    yield {"type": "diagnosis", "id": b.id, "diagnosis": diagnosis}
                except Exception as e:
                    if isinstance(e, ModelError) and e.fatal:
                        raise
                    diagnosis = None
                    yield {"type": "note", "id": b.id, "text": f"Diagnosis skipped: {redact(e)}"}
                    warnings.append(f"{b.id}: diagnosis step failed ({redact(e)[:80]}); patch was written without it.")
            yield {"type": "batch", "id": b.id, "status": "patching", "engine": tier, "attempt": attempt, "files": rels}
            snap = repo.snapshot()
            try:
                patch = agent.patch(b, repo, rels, diagnosis, guide, feedback, attempt)
                touched = repo.apply(patch["edits"])
            except Exception as e:
                if isinstance(e, ModelError) and e.fatal:
                    raise
                reason = redact(e)[:300]
                yield {"type": "evidence", "stage": "result", "verdict": "rejected", "id": b.id, "attempt": attempt,
                       "tier": tier, "model": agent.models.get(tier, ""), "reason": reason, "resolved_ids": [],
                       "targets_total": len(b.issues), "targets_resolved": 0, "new_issues": [], "diff": ""}
                feedback = f"Attempt {attempt} produced no usable patch: {reason}"
                repo.restore(snap)
                attempts.append({"n": attempt, "tier": tier, "model": agent.models.get(tier, ""),
                                 "outcome": "no_usable_patch", "detail": reason})
                rejected.append({"batch": b.id, "attempt": attempt, "reason": reason})
                yield {"type": "batch", "id": b.id, "status": "retry" if attempt == 1 else "failed", "detail": feedback}
                if isinstance(e, PatchError) and "side effect" in str(e):
                    break
                continue

            applied_diff = diff_between(snap, repo, touched)
            yield {"type": "evidence", "stage": "proposed", "id": b.id, "attempt": attempt, "tier": tier,
                   "model": patch.get("model", ""), "edits": patch["edits"], "files": touched,
                   "explanation": patch.get("explanation", ""), "diagnosis": diagnosis,
                   "targets": [i.id for i in b.issues]}
            yield {"type": "batch", "id": b.id, "status": "verifying", "engine": "axe", "attempt": attempt}
            t_verify = time.time()
            new_issues, _ = auditor.audit(server.url)
            new_fps = {i.fingerprint for i in new_issues}
            introduced = new_fps - {i.fingerprint for i in current}
            gone = b.fps - new_fps
            if introduced or not gone:
                why = (f"it introduced {len(introduced)} new issue(s): " + "; ".join(sorted(introduced))[:240]
                       if introduced else "the targeted issues were still present after the change")
                feedback = f"Attempt {attempt} was reverted because {why}."
                repo.restore(snap)
                yield {"type": "evidence", "stage": "result", "verdict": "rolled_back", "id": b.id, "attempt": attempt,
                       "tier": tier, "model": patch.get("model", ""), "reason": why,
                       "resolved_ids": [fp2id[fp] for fp in sorted(gone) if fp in fp2id],
                       "targets_total": len(b.issues), "targets_resolved": len(gone),
                       "new_issues": [{"rule": i.rule, "page": i.page, "message": i.message[:160]}
                                      for i in new_issues if i.fingerprint in introduced][:8],
                       "diff": applied_diff, "counts_after": counts(new_issues),
                       "verify_ms": int((time.time() - t_verify) * 1000)}
                attempts.append({"n": attempt, "tier": tier, "model": patch.get("model", ""),
                                 "outcome": "reverted", "detail": why})
                rejected.append({"batch": b.id, "attempt": attempt, "reason": why})
                yield {"type": "batch", "id": b.id, "status": "retry" if attempt == 1 else "failed", "detail": feedback}
                continue

            # verified improvement: keep it
            diff = diff_between(snap, repo, touched)
            fixed_ids = [fp2id[fp] for fp in sorted(open_fps - new_fps) if fp in fp2id]
            open_fps = {i.fingerprint for i in new_issues}
            current = new_issues
            b.issues = [i for i in b.issues if i.fingerprint in new_fps]
            attempts.append({"n": attempt, "tier": tier, "model": patch.get("model", ""), "outcome": "verified",
                             "detail": f"{len(fixed_ids)} issue(s) gone, 0 introduced", "fixed_ids": fixed_ids})
            yield {"type": "evidence", "stage": "result", "verdict": "verified", "id": b.id, "attempt": attempt,
                   "tier": tier, "model": patch.get("model", ""), "reason": "", "resolved_ids": fixed_ids,
                   "targets_total": len(targets), "targets_resolved": len(gone), "new_issues": [], "diff": diff,
                   "counts_after": counts(new_issues), "verify_ms": int((time.time() - t_verify) * 1000)}
            yield {"type": "fix", "id": b.id, "diff": diff, "explanation": patch.get("explanation", ""),
                   "model": patch.get("model", ""), "tier": patch.get("tier", ""), "attempt": attempt,
                   "files": touched, "fixed_ids": fixed_ids}
            yield {"type": "issue_status", "fixed": fixed_ids, "batch": b.id, "counts": counts(new_issues)}
            if b.issues and attempt == 1:
                feedback = ("Attempt 1 fixed part of the group; still failing: "
                            + "; ".join(i.fingerprint for i in b.issues)[:300])
                yield {"type": "batch", "id": b.id, "status": "retry", "detail": feedback}
                continue
            done = not b.issues
            break
        b.status = "fixed" if done else ("partial" if len(b.issues) < len(targets) else "needs-review")
        yield {"type": "batch", "id": b.id, "status": b.status, "detail": ""}
        log.append({"title": b.title, "status": b.status, "attempts": attempts})
    if stop["why"] == "cancelled":
        warnings.append("The run was stopped by the user; some groups were not attempted. Kept fixes were verified.")
    elif stop["why"] == "time":
        warnings.append("The run time limit was reached; some groups were not attempted.")

    # ------------------------------------------------------------ 4. report
    after = counts(current)
    for i in current:
        i.id = fp2id.get(i.fingerprint, i.id)
    remaining = [issue_row(i) for i in current]
    fixed_total = before["total"] - after["total"]
    yield stage("report", "Writing the pull-request description", "ultra")
    full_diff = repo.diff()
    summary = {"before": before, "after": after, "fixed": fixed_total, "total": before["total"],
               "pages": len(pages), "batches": [{"title": x["title"], "status": x["status"]} for x in log],
               "remaining": remaining[:15]}
    try:
        pr = agent.pr(summary)
    except Exception as e:
        if isinstance(e, ModelError) and e.fatal:
            raise
        warnings.append(f"The PR description could not be written by the model ({redact(e)[:80]}); a template was used.")
        pr = template_pr(summary, "model call failed")
    if auditor.blocked:
        warnings.append("Requests to external hosts were blocked during the audit for safety: "
                        + ", ".join(sorted(auditor.blocked)[:6]) + ". Issues that depend on them are not covered.")
    warnings += [w for w in getattr(agent, "warnings", []) if w not in warnings]
    source_unchanged = repo.source_hash() == h0
    if not source_unchanged:                                    # must never happen; surfaced loudly if it does
        warnings.append("INTEGRITY ALERT: the original source folder changed during the run.")
    evidence = {"live": agent.live, "models": agent.models, "source_unchanged": source_unchanged,
                "blocked_external": sorted(auditor.blocked), "tavily": tstats, "warnings": warnings,
                "elapsed_s": round(time.time() - t_start, 1)}
    yield {"type": "report", **summary, "remaining": remaining[:300], "batches": log, "diff": full_diff, "pr": pr,
           "rejected": rejected, "evidence": evidence}
    if agent.usage():
        yield {"type": "usage", "calls": agent.usage()}
    yield {"type": "done"}


def _guidance_query(b: Batch) -> str:
    if b.kind == "a11y":
        return f"WCAG axe-core rule {b.rule} how to fix accessible HTML example"
    msg = re.sub(r"https?://\S+|\d+", "", b.issues[0].message)[:100]
    return f"how to fix JavaScript browser error {msg}"
