/* Run state: a pure reducer over the backend's event stream. Nothing here is simulated; every field is set
   by an event the server actually emitted. */
import { NETWORK_RULES } from "./util.js";

export const PHASES = [
  ["audit", "Browser audit"], ["triage", "Issue triage"], ["guidance", "Guidance retrieval"], ["diagnosis", "Root-cause analysis"],
  ["patch", "Patch generation"], ["apply", "Applied to temporary copy"], ["verify", "Verification re-audit"],
  ["retry", "Retry / rollback"], ["report", "Final report"],
];
const ACTIVE = { diagnosing: "diagnosis", patching: "patch", verifying: "verify" };
export const BATCH_LABEL = {
  triaged: "Queued", diagnosing: "Analysing root cause", patching: "Generating patch", verifying: "Verifying in browser",
  retry: "Retrying", fixed: "Fixed", partial: "Partly fixed", failed: "Failed", "needs-review": "Needs review", skipped: "Skipped",
};

export function newState() {
  return { run: null, engine: null, live: false, models: {}, tavily: false, status: "idle", seq: -1, startedTs: null, endedTs: null,
    error: null, before: null, now: null, pages: [], issues: new Map(), batches: new Map(), patches: [], resolved: new Set(),
    report: null, usage: null, activity: [], t: {}, tally: { intel: {}, diag: 0, patch: 0, apply: 0, verify: 0, retry: 0, rollback: 0 },
    cancelRequested: false, version: 0 };
}

function act(s, key, state, text, ts, detail) {
  const e = { key, state, text, ts, detail: detail ? [detail] : [] };
  const i = s.activity.findIndex((x) => x.key === key);
  if (i >= 0) { e.detail = [...s.activity[i].detail, ...e.detail]; e.t0 = s.activity[i].t0; s.activity[i] = e; }
  else { e.t0 = ts; s.activity.push(e); }
}
const addDetail = (s, key, line) => { const e = s.activity.find((x) => x.key === key); if (e) e.detail.push(line); };

function batchOf(s, id) {
  if (!s.batches.has(id)) s.batches.set(id, { id, title: id, kind: "", rule: "", status: "triaged", issue_ids: [], triage: {}, notes: [], intel: null, diagnosis: null, fix: null, t0: null, t1: null });
  return s.batches.get(id);
}
const tick = (o, k) => { o[k] = (o[k] || 0) + 1; };

/** Returns true if the event changed state (false for duplicates). */
export function apply(s, ev) {
  if (typeof ev.seq === "number") { if (ev.seq <= s.seq) return false; s.seq = ev.seq; }
  s.version++;
  const ts = ev.ts;
  switch (ev.type) {
    case "run":
      s.run = ev.run; s.engine = ev.engine; s.status = "running"; s.startedTs = ts;
      act(s, "run", "done", `Run started (${ev.engine === "live" ? "live engine" : "scripted demo engine"})`, ts);
      if (ev.skipped_files) act(s, "skipped", "skip", `${ev.skipped_files} non-web file(s) in the zip were skipped (sources, docs, media)`, ts);
      break;
    case "mode":
      s.live = ev.live; s.models = ev.models || {}; s.tavily = !!ev.tavily;
      if (ev.live) act(s, "models", "done", "Nemotron models resolved on Token Factory", ts, Object.entries(s.models).map(([k, v]) => `${k}: ${v}`).join("\n"));
      break;
    case "stage":
      s.t[ev.key] = { s: "running", t0: ts };
      act(s, ev.key, "running", ev.label, ts);
      break;
    case "audit":
      s.before = ev.counts; s.now = ev.counts; s.pages = ev.pages || [];
      s.t.audit = { ...(s.t.audit || {}), s: "done", t1: ts };
      act(s, "audit", "done", `Browser audit completed: ${ev.counts.total} issue(s) on ${s.pages.length} page(s)`, ts,
        `Accessibility ${ev.counts.a11y} · JavaScript ${ev.counts.js ?? ev.counts.runtime} · Network ${ev.counts.network ?? 0}`);
      break;
    case "issues":
      for (const r of ev.issues || []) s.issues.set(r.id, { ...r, category: r.category || (r.kind === "a11y" ? "a11y" : NETWORK_RULES.has(r.rule) ? "network" : "js") });
      break;
    case "batch": {
      const b = batchOf(s, ev.id);
      for (const k of ["title", "kind", "rule", "triage"]) if (ev[k]) b[k] = ev[k];
      if (ev.issue_ids) b.issue_ids = ev.issue_ids;
      if (ev.status) b.status = ev.status;
      if (ev.attempt) b.attempt = ev.attempt;
      if (ev.engine) b.engine = ev.engine;
      if (ev.detail) b.notes.push({ ts, text: ev.detail, bad: ev.status === "retry" || ev.status === "failed" });
      if (ev.status === "triaged" && !s.t.triage?.t1) s.t.triage = { ...s.t.triage, s: "done", t1: ts };
      if (ACTIVE[ev.status]) tick(s.tally, { diagnosing: "diag", patching: "patch", verifying: "verify" }[ev.status]);
      if (ev.status === "retry") tick(s.tally, "retry");
      if (ev.status && !["triaged"].includes(ev.status)) { b.t0 ??= ts; }
      if (["fixed", "partial", "failed", "needs-review", "skipped"].includes(ev.status)) b.t1 = ts;
      if (ev.status && ev.status !== "triaged") {
        const model = ev.engine && ev.engine !== "axe" ? (s.models[ev.engine] || "") : "";
        const running = !["fixed", "partial", "failed", "needs-review", "skipped"].includes(ev.status);
        const st = ev.status === "fixed" || ev.status === "partial" ? "done" : running ? (ev.status === "retry" ? "warn" : "running") : ev.status === "skipped" ? "skip" : "fail";
        act(s, "b:" + b.id, st, `${b.id} ${b.title.slice(0, 60)}: ${BATCH_LABEL[ev.status] || ev.status}${running && model ? ` (${model})` : ""}`, ts,
          ev.detail || (ev.attempt ? `attempt ${ev.attempt}${ev.engine ? ` · ${ev.engine}` : ""}${model ? ` · ${model}` : ""}` : ""));
      }
      break;
    }
    case "intel": {
      const b = batchOf(s, ev.id); b.intel = ev; tick(s.tally.intel, ev.status);
      addDetail(s, "b:" + ev.id, `Tavily ${ev.status}${ev.reason ? ": " + ev.reason : ""} (${(ev.sources || []).length} source(s))`);
      break;
    }
    case "diagnosis": batchOf(s, ev.id).diagnosis = ev.diagnosis; break;
    case "note": { const b = batchOf(s, ev.id); b.notes.push({ ts, text: ev.text, bad: true }); addDetail(s, "b:" + ev.id, ev.text); break; }
    case "evidence": {
      let p = s.patches.find((x) => x.id === ev.id && x.attempt === ev.attempt);
      if (!p) { p = { id: ev.id, attempt: ev.attempt, label: "PROPOSED", edits: [], files: [] }; s.patches.push(p); }
      if (ev.stage === "proposed") {
        tick(s.tally, "apply"); Object.assign(p, { tier: ev.tier, model: ev.model, edits: ev.edits, files: ev.files, explanation: ev.explanation,
          diagnosis: ev.diagnosis, targets: ev.targets, label: "PROPOSED", t0: ts });
      } else {
        const lab = { verified: "VERIFIED", rolled_back: "ROLLED BACK", rejected: "REJECTED" }[ev.verdict];
        Object.assign(p, { tier: ev.tier, model: ev.model, verdict: ev.verdict, label: lab, reason: ev.reason, resolved_ids: ev.resolved_ids,
          targets_total: ev.targets_total, targets_resolved: ev.targets_resolved, new_issues: ev.new_issues, diff: ev.diff,
          counts_after: ev.counts_after, verify_ms: ev.verify_ms, t1: ts });
        if (ev.verdict === "rolled_back") tick(s.tally, "rollback");
      }
      break;
    }
    case "fix": batchOf(s, ev.id).fix = ev; break;
    case "issue_status":
      s.now = ev.counts; (ev.fixed || []).forEach((id) => s.resolved.add(id)); break;
    case "report": {
      s.report = ev; s.now = ev.after; s.t.report = { ...(s.t.report || {}), s: "done", t1: ts };
      const left = new Set((ev.remaining || []).map((x) => x.id));
      if ((ev.remaining || []).length === ev.after.total) for (const id of s.issues.keys()) if (!left.has(id)) s.resolved.add(id);
      act(s, "report", "done", ev.audit_only ? `Audit report ready: ${ev.total} issue(s) found` : `Report ready: ${ev.fixed} of ${ev.total} issue(s) fixed`, ts);
      break;
    }
    case "usage": s.usage = ev.calls; break;
    case "done": if (s.status !== "error") s.status = "done"; s.endedTs = ts; break;
    case "error":
      s.status = "error"; s.error = ev; s.endedTs = ts; act(s, "error", "fail", ev.message, ts); break;
    default: break;
  }
  return true;
}

export const finalState = (s) => s.status === "done" || s.status === "error";

/** Per-issue status, derived from real events only. */
export function issueStatus(s, i) {
  if (s.resolved.has(i.id)) return "resolved";
  const b = [...s.batches.values()].find((x) => x.issue_ids.includes(i.id));
  const rejected = s.patches.some((p) => p.id === b?.id && (p.verdict === "rolled_back" || p.verdict === "rejected"));
  if (finalState(s) || s.report) return rejected ? "rejected" : b?.status === "skipped" ? "skipped" : "unresolved";
  if (!b || b.status === "triaged") return "queued";
  if (b.status === "skipped") return "skipped";
  return rejected && ["retry", "failed", "needs-review"].includes(b.status) ? "rejected" : "in-progress";
}

/** Timeline rows. States: pending | running | completed | failed | skipped. */
export function timeline(s) {
  const fin = finalState(s) || !!s.report, T = s.tally, cur = new Set([...s.batches.values()].map((b) => ACTIVE[b.status]).filter(Boolean));
  const batches = [...s.batches.values()];
  const intelN = Object.values(T.intel).reduce((a, b) => a + b, 0);
  const row = (key, label, status, detail) => ({ key, label, status, detail });
  const batchPhase = (key, label, n, unit, offReason) => {
    if (offReason) return row(key, label, "skipped", offReason);
    if (cur.has(key)) return row(key, label, "running", `${n} so far`);
    if (n > 0) return row(key, label, "completed", `${n} ${unit}`);
    if (fin) return row(key, label, "skipped", "Not needed");
    return row(key, label, s.t.triage?.s === "done" ? "pending" : "pending", "");
  };
  const t = s.t, rows = [];
  rows.push(row("audit", "Browser audit", t.audit?.s === "done" ? "completed" : t.audit ? "running" : "pending", s.before ? `${s.before.total} issue(s)` : ""));
  const fb = batches.filter((b) => b.triage?.source === "fallback").length;
  rows.push(row("triage", "Issue triage", t.triage?.s === "done" ? "completed" : t.triage ? "running" : "pending",
    batches.length ? `${batches.length} group(s)${fb ? ` · ${fb} used fallback labels` : ""}` : ""));
  let g;
  if (s.engine === "demo") g = row("guidance", "Guidance retrieval", "skipped", "Scripted demo: no web search");
  else {
    const { ok = 0, empty = 0, skipped = 0, error = 0 } = T.intel, det = [ok && `${ok} ok`, empty && `${empty} empty`, skipped && `${skipped} skipped`, error && `${error} failed`].filter(Boolean).join(", ");
    g = !intelN ? row("guidance", "Guidance retrieval", fin ? "skipped" : "pending", fin ? "Not used" : "")
      : (skipped === intelN) ? row("guidance", "Guidance retrieval", "skipped", `Tavily not configured (${intelN} lookup(s) skipped)`)
      : error === intelN ? row("guidance", "Guidance retrieval", "failed", det) : row("guidance", "Guidance retrieval", fin ? "completed" : "running", det);
  }
  rows.push(g);
  const demo = s.engine === "demo";
  rows.push(batchPhase("diagnosis", "Root-cause analysis", T.diag, "group(s) analysed", demo ? "Scripted demo: no model analysis" : ""));
  rows.push(batchPhase("patch", "Patch generation", T.patch, "attempt(s)"));
  rows.push(batchPhase("apply", "Applied to temporary copy", T.apply, "patch(es) applied"));
  rows.push(batchPhase("verify", "Verification re-audit", T.verify, "re-audit(s)"));
  const rr = T.retry + T.rollback;
  rows.push(rr ? row("retry", "Retry / rollback", "completed", `${T.retry} retry · ${T.rollback} rolled back`)
    : row("retry", "Retry / rollback", fin ? "skipped" : "pending", fin ? "Not needed" : ""));
  rows.push(row("report", "Final report", t.report?.s === "done" ? "completed" : t.report ? "running" : "pending", ""));
  if (s.status === "error") {
    let k = -1; rows.forEach((r, i) => { if (r.status === "running" || r.status === "completed") k = i; });
    const last = rows.findLastIndex((r) => r.status === "running"); if (last >= 0) k = last; else k = Math.min(k + 1, rows.length - 1);
    rows[k] = { ...rows[k], status: "failed", detail: "Run stopped here" };
  } else if (s.cancelRequested && fin) rows.forEach((r) => { if (r.status === "pending") { r.status = "skipped"; r.detail = "Run stopped"; } });
  return rows;
}

export function counters(s) {
  const b = s.before, n = s.now || b;
  const verified = s.patches.filter((p) => p.verdict === "verified").length;
  const rejected = s.patches.filter((p) => p.verdict === "rolled_back" || p.verdict === "rejected").length;
  return { b, n, verified, rejected, resolved: b ? b.total - n.total : 0, remaining: n ? n.total : 0 };
}
