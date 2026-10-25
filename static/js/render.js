/* Rendering. Every number and label here comes from reducer state, i.e. from real backend events. */
import { esc, SEVERITY, sevRank, issueTitle, issueWhere, parseDiff, fmtSecs, clock, fmtBytes, download } from "./util.js";
import { BATCH_LABEL, counters, finalState, issueStatus, timeline } from "./state.js";

/* ------------------------------------------------------------------ metrics */
export function renderMetrics(s, el) {
  const c = counters(s);
  el.classList.toggle("ro", s.mode === "url" || !!s.report?.audit_only);
  if (!c.b) { el.innerHTML = `<p class="fine">Waiting for the browser audit to finish…</p>`; return; }
  const card = (label, now, was, cls = "") => `<div class="metric ${cls}"><small>${label}</small><div class="nums"><span class="now ${now === 0 && cls !== "plain" ? "zero" : ""}">${now}</span>${was != null ? `<span class="was">${was}</span>` : ""}</div></div>`;
  const b = c.b, n = c.n, js = (x) => x.js ?? x.runtime, net = (x) => x.network ?? 0;
  if (s.mode === "url" || s.report?.audit_only) {          // report-only: nothing is patched, so no resolved/patch counts
    el.innerHTML = card("Accessibility violations", b.a11y, null, b.a11y ? "" : "plain") + card("JavaScript errors", js(b), null, js(b) ? "" : "plain") +
      card("Network failures", net(b), null, net(b) ? "" : "plain") + card("Issues found", b.total, null, b.total ? "bad" : "plain") +
      card("Pages audited", s.pages.length, null, "plain");
    return;
  }
  el.innerHTML =
    card("Accessibility violations", n.a11y, `was ${b.a11y}`) + card("JavaScript errors", js(n), `was ${js(b)}`) +
    card("Network failures", net(n), `was ${net(b)}`) +
    card("Issues resolved", c.resolved, `of ${b.total}`, c.resolved ? "good" : "plain") +
    card("Issues remaining", c.remaining, `was ${b.total}`, c.remaining ? "bad" : "") +
    card("Verified patches", c.verified, null, c.verified ? "good" : "plain") + card("Rejected patches", c.rejected, null, c.rejected ? "bad" : "plain");
}

/* ------------------------------------------------------------------ timeline + activity */
const GLYPH = { completed: "✓", failed: "✕", skipped: "–", running: "", pending: "" };
export function renderTimeline(s, el) {
  el.innerHTML = timeline(s).map((r) => `<li data-s="${r.status}"><span class="st" aria-hidden="true">${GLYPH[r.status]}</span>
    <div><span class="tl-n">${esc(r.label)}</span> <span class="sr-only">${r.status}</span>${r.detail ? `<span class="tl-d">${esc(r.detail)}</span>` : ""}</div></li>`).join("");
}
const ICON = { done: "✓", running: "●", fail: "✕", warn: "↻", skip: "–" };
export function renderActivity(s, el, empty, ui) {
  empty.hidden = s.activity.length > 0;
  const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
  el.innerHTML = s.activity.map((a) => `<li data-s="${a.state}"><span class="a-i" aria-hidden="true">${ICON[a.state]}</span><div>
    <time datetime="${a.ts}">${clock(a.ts)}</time>${esc(a.text)}<span class="sr-only"> (${a.state === "done" ? "completed" : a.state === "fail" ? "failed" : a.state})</span>
    ${a.detail.length ? `<details data-key="act-${esc(a.key)}" ${ui.open.has("act-" + a.key) ? "open" : ""}><summary>Details</summary><pre>${esc(a.detail.join("\n"))}</pre></details>` : ""}</div></li>`).join("");
  if (stick) el.scrollTop = el.scrollHeight;
}
export function renderModels(s, el) {
  if (!s.run) { el.hidden = true; return; }
  el.hidden = false;
  if (s.mode === "url") { el.innerHTML = `<h2 class="sec">Report-only audit</h2><p class="fine">No model calls and no changes to your site. Suggested fixes come from axe-core's rule guidance.</p>`; return; }
  el.innerHTML = `<h2 class="sec">${s.engine === "live" ? "Live · models on Token Factory" : "Scripted demo"}</h2>` + (s.engine === "live"
    ? (Object.keys(s.models).length ? Object.entries(s.models).map(([k, v]) => `<div class="mono"><b>${esc(k)}</b> ${esc(v)}</div>`).join("") : `<p class="fine">Resolving models…</p>`) +
      `<p class="fine">Tavily: ${s.tavily ? "configured" : "not configured (guidance skipped)"}</p>`
    : `<p class="fine">No model calls. Patches are scripted for the bundled demo site. The audit and every verification are real.</p>`);
}
export function renderUsage(s, el) {
  if (!s.usage?.length) { el.hidden = true; return; }
  el.hidden = false;
  const by = {};
  s.usage.forEach((c) => { const t = (by[c.tier] ||= { n: 0, bad: 0, ms: 0, tok: 0, known: 0, model: c.model }); t.n++; t.ms += c.ms; if (!c.ok) t.bad++; if (c.tokens != null) { t.tok += c.tokens; t.known++; } });
  el.innerHTML = `<h2 class="sec">Model calls (as reported)</h2><table><tr><th>Tier</th><th>Calls</th><th>Failed</th><th>Avg ms</th><th>Tokens</th></tr>` +
    ["nano", "super", "ultra"].filter((k) => by[k]).map((k) => `<tr><td title="${esc(by[k].model)}">${k}</td><td>${by[k].n}</td><td>${by[k].bad}</td><td>${Math.round(by[k].ms / by[k].n)}</td><td>${by[k].known ? by[k].tok + (by[k].known < by[k].n ? "+" : "") : "n/a"}</td></tr>`).join("") +
    `</table><p class="fine">Tokens appear only when the provider reported them.</p>`;
}

/* ------------------------------------------------------------------ issues */
export const FILTERS = [["all", "All"], ["a11y", "Accessibility"], ["js", "JavaScript / runtime"], ["network", "Network"], ["high", "Critical / high"],
  ["resolved", "Resolved"], ["unresolved", "Unresolved"], ["rejected", "Rejected patches"]];
const STATUS_CHIP = { resolved: ["ok", "Resolved · verified"], unresolved: ["bad", "Unresolved"], rejected: ["warn", "Patch rejected"], "in-progress": ["info work", "In progress"], queued: ["", "Queued"], skipped: ["", "Skipped"] };
const CAT = { a11y: "Accessibility", js: "JavaScript", network: "Network" };
const STATUS_ORDER = { unresolved: 0, rejected: 1, "in-progress": 2, queued: 3, skipped: 4, resolved: 5 };

function matches(f, i, st) {
  return f === "all" || (f === "a11y" && i.category === "a11y") || (f === "js" && i.category === "js") || (f === "network" && i.category === "network") ||
    (f === "high" && ["critical", "serious"].includes(i.impact)) || (f === "resolved" && st === "resolved") ||
    (f === "unresolved" && st !== "resolved") || (f === "rejected" && st === "rejected");
}
export function renderIssues(s, ui, els) {
  const issues = [...s.issues.values()];
  const group = new Map(); for (const b of s.batches.values()) for (const id of b.issue_ids) group.set(id, b);
  const st = new Map(issues.map((i) => [i.id, issueStatus(s, i)]));
  els.cnt.textContent = issues.length;
  els.filters.innerHTML = FILTERS.map(([k, l]) => `<button type="button" class="f" data-f="${k}" aria-pressed="${ui.filter === k}">${l}<span class="n">${issues.filter((i) => matches(k, i, st.get(i.id))).length}</span></button>`).join("");
  const q = ui.q.trim().toLowerCase();
  let rows = issues.filter((i) => matches(ui.filter, i, st.get(i.id)) &&
    (!q || [i.id, issueTitle(i), issueWhere(i), i.message, i.rule, i.selector].some((x) => String(x || "").toLowerCase().includes(q))));
  const key = { severity: (a, b) => sevRank(a.impact) - sevRank(b.impact) || a.id.localeCompare(b.id), status: (a, b) => STATUS_ORDER[st.get(a.id)] - STATUS_ORDER[st.get(b.id)] || sevRank(a.impact) - sevRank(b.impact),
    file: (a, b) => (a.source?.path || a.page).localeCompare(b.source?.path || b.page) || sevRank(a.impact) - sevRank(b.impact) }[ui.sort];
  rows.sort(key);
  const shown = rows.slice(0, ui.limit);
  els.count.textContent = issues.length ? `Showing ${shown.length} of ${rows.length} matching issue(s) · ${issues.length} total` : "";
  els.more.hidden = rows.length <= shown.length;
  if (!issues.length) {
    els.list.innerHTML = s.before ? `<div class="empty"><b>No issues found</b><span>The browser audit found nothing to fix on this site.</span></div>`
      : finalState(s) ? `<div class="empty"><b>No audit results</b><span>The run ended before the browser audit finished. Start a new audit to try again.</span></div>`
      : `<div class="skel" aria-busy="true"><p class="sr-only">Auditing. Issues appear here when the browser audit finishes.</p>${'<div class="skel-row" aria-hidden="true"><i></i><i></i></div>'.repeat(5)}</div>`;
    return;
  }
  if (!shown.length) { els.list.innerHTML = `<div class="empty"><b>No issues match</b><span>No issues match this filter or search.</span><button type="button" class="btn sm" data-clear-filters>Clear all filters</button></div>`; return; }
  els.list.innerHTML = shown.map((i) => {
    const [cls, label] = STATUS_CHIP[st.get(i.id)], g = group.get(i.id), k = "iss-" + i.id;
    const patches = s.patches.filter((p) => p.id === g?.id);
    return `<details class="issue" data-key="${k}" ${ui.open.has(k) ? "open" : ""}><summary>
      <span class="row1"><span class="sev ${esc(i.impact)}">${esc(i.impact)}</span><span class="t">${esc(issueTitle(i))}</span><span class="chip ${cls}">${label}</span></span>
      <span class="where">${esc(i.id)} · ${CAT[i.category]} · ${esc(issueWhere(i))}${g ? ` · ${g.issue_ids.length} related` : ""}</span></summary>
      <div class="body"><dl><dt>Rule</dt><dd>${esc(i.rule)}</dd><dt>Page</dt><dd>${esc(i.page)}</dd>${i.selector ? `<dt>Element</dt><dd><code>${esc(i.selector)}</code></dd>` : ""}
        ${i.source?.path ? `<dt>Source</dt><dd>${esc(issueWhere({ ...i, selector: "" }))}</dd>` : ""}
        <dt>Group</dt><dd>${g ? `${esc(g.id)} · ${esc(g.title)} (${esc(BATCH_LABEL[g.status] || g.status)})` : "Not grouped yet"}</dd>
        <dt>Patches</dt><dd>${patches.length ? patches.map((p) => `${patchLabel(s, p)} attempt ${p.attempt}`).join(" · ") : "None yet"}</dd></dl>
        <pre class="code">${esc(i.message)}</pre>${i.html ? `<pre class="code">${esc(i.html)}</pre>` : ""}
        ${i.help_url ? `<a href="${esc(i.help_url)}" target="_blank" rel="noopener noreferrer">Rule documentation (opens in a new tab)</a>` : ""}</div></details>`;
  }).join("");
}

/* ------------------------------------------------------------------ patches */
const LABEL_CLASS = { PROPOSED: "proposed", VERIFYING: "verifying", VERIFIED: "verified", "ROLLED BACK": "rolled", REJECTED: "rejected" };
function liveLabel(s, p) {
  const b = s.batches.get(p.id);
  return p.label === "PROPOSED" && b?.status === "verifying" && b.attempt === p.attempt ? "VERIFYING" : p.label;
}
const patchLabel = (s, p) => { const l = liveLabel(s, p); return `<span class="stat ${LABEL_CLASS[l]}">${l}</span>`; };

export function diffHtml(text, openAll, ui, prefix) {
  const files = parseDiff(text);
  if (!files.length) return `<p class="fine">No diff available.</p>`;
  return `<div class="diffs">${files.map((f, n) => {
    const k = `${prefix}-d${n}`;
    return `<details class="dfile" data-key="${esc(k)}" ${openAll || ui.open.has(k) ? "open" : ""}><summary><span>${esc(f.name)}</span><span class="adds">+${f.add}</span><span class="dels">−${f.del}</span></summary>
      <div class="dscroll" tabindex="0" role="region" aria-label="Diff of ${esc(f.name)}"><table class="dl">${f.lines.map((l) => l.t === "h" ? `<tr class="h"><td class="n"></td><td class="s">${esc(l.text)}</td></tr>`
        : `<tr class="${l.t}"><td class="n">${l.n}</td><td class="s">${esc(l.text) || " "}</td></tr>`).join("")}</table></div></details>`;
  }).join("")}</div>`;
}
const yesno = (v, y, n) => `<b>${v ? y : n}</b>`;

export function renderPatches(s, ui, els) {
  els.cnt.textContent = s.patches.length;
  const v = s.patches.filter((p) => p.verdict === "verified").length, rb = s.patches.filter((p) => p.verdict === "rolled_back").length, rj = s.patches.filter((p) => p.verdict === "rejected").length;
  els.summary.textContent = s.patches.length ? `${s.patches.length} patch attempt(s): ${v} verified, ${rb} rolled back, ${rj} rejected before applying.` : "";
  if (!s.patches.length) { els.list.innerHTML = `<div class="empty">${finalState(s) ? "No patch attempts were made." : "Patch attempts and their verification evidence appear here as the agent works."}</div>`; return; }
  els.list.innerHTML = s.patches.map((p) => {
    const b = s.batches.get(p.id) || {}, label = liveLabel(s, p), k = `p-${p.id}-${p.attempt}`;
    const files = parseDiff(p.diff), add = files.reduce((a, f) => a + f.add, 0), del = files.reduce((a, f) => a + f.del, 0);
    const done = !!p.verdict, na = p.counts_after;
    const outcome = p.verdict === "verified" ? "Accepted: kept in the final patch" : p.verdict === "rolled_back" ? "Rolled back: reverted, not in the final patch" : p.verdict === "rejected" ? "Rejected before it could be applied: nothing changed" : "Pending verification";
    return `<details class="patch" data-key="${k}" ${ui.open.has(k) ? "open" : ""}><summary>
      <span class="row1"><span class="stat ${LABEL_CLASS[label]}">${label}</span><span class="t">${esc(p.id)} · ${esc(b.title || "")}</span><span class="chip">attempt ${p.attempt}${p.tier ? ` · ${esc(p.tier)}` : ""}</span></span>
      <span class="where">${p.model ? esc(p.model) + " · " : ""}${files.length ? `${files.length} file(s) · +${add} −${del}` : p.files?.length ? esc(p.files.join(", ")) : "no changes applied"}</span></summary>
      <div class="body">
        <table class="ev"><tbody>
          <tr><th scope="row">Original issue</th><td>${esc(b.title || p.id)} · ${(p.targets || b.issue_ids || []).length} targeted issue(s): ${esc((p.targets || b.issue_ids || []).slice(0, 8).join(", "))}</td></tr>
          <tr><th scope="row">Proposed repair</th><td>${p.edits?.length ? `${p.edits.length} search/replace edit(s) in ${esc((p.files || []).join(", "))}` : "None (no usable patch was produced)"}${p.explanation ? `<br>${esc(p.explanation)}` : ""}</td></tr>
          <tr><th scope="row">Post-patch audit</th><td>${done && p.verdict !== "rejected" ? `Re-audited in a real browser${p.verify_ms ? ` (${fmtSecs(p.verify_ms / 1000)})` : ""}${na ? `: ${na.total} issue(s) remain (accessibility ${na.a11y}, JavaScript ${na.js ?? na.runtime}, network ${na.network ?? 0})` : ""}` : done ? "Not run (nothing was applied)" : "Waiting"}</td></tr>
          <tr><th scope="row">Targeted issue disappeared?</th><td>${done && p.verdict !== "rejected" ? `${yesno(p.targets_resolved >= p.targets_total, "Yes", p.targets_resolved ? "Partly" : "No")} (${p.targets_resolved} of ${p.targets_total})` : "n/a"}</td></tr>
          <tr><th scope="row">New issues appeared?</th><td>${done && p.verdict !== "rejected" ? (p.new_issues?.length ? `<b>Yes (${p.new_issues.length})</b>: ${esc(p.new_issues.map((x) => `${x.rule} on ${x.page}`).join("; "))}` : "<b>No</b>") : "n/a"}</td></tr>
          <tr><th scope="row">Outcome</th><td><b>${outcome}</b></td></tr>
          ${p.reason ? `<tr><th scope="row">Reason</th><td>${esc(p.reason)}</td></tr>` : ""}
        </tbody></table>
        ${p.diagnosis ? `<div><b>Root cause</b><br>${esc(p.diagnosis.root_cause)}<br><b>Plan</b><br>${esc(p.diagnosis.plan)}</div>` : ""}
        ${p.diff ? `<div><b>${p.verdict === "rolled_back" ? "Diff that was rolled back" : "Diff"}</b>${diffHtml(p.diff, false, ui, k)}</div>` : ""}
        ${p.edits?.length ? `<details data-key="${k}-e" ${ui.open.has(k + "-e") ? "open" : ""}><summary>Search and replace edits (${p.edits.length})</summary>${p.edits.map((e) => `<div class="edit"><b>${esc(e.file)}</b><pre class="s">${esc(e.search)}</pre><pre class="r">${esc(e.replace)}</pre></div>`).join("")}</details>` : ""}
      </div></details>`;
  }).join("");
}

/* ------------------------------------------------------------------ summary */
function bars(rows, max, cls) { return rows.map(([l, n, t]) => `<div class="brow"><span>${esc(l)}</span><span class="bar ${cls}"><b style="width:${max ? Math.max(n ? 3 : 0, (n / max) * 100) : 0}%"></b></span><span>${esc(t ?? n)}</span></div>`).join(""); }
export function errorView(ev) {
  const hints = { auth: "Check NEBIUS_API_KEY in .env, then restart the server.", model_not_found: "Run python scripts/list_models.py and pin the model IDs in .env.",
    credits: "Add credits to your Nebius account, or switch to the scripted demo.", rate_limit: "Wait a moment and run again.", timeout: "The model took too long. Try again.",
    network: "Check the server's internet connection.", config: "Add NEBIUS_API_KEY to .env, or switch to the scripted demo.", browser: "Run: playwright install chromium", internal: "Details are in the server log; quote the error ID if you report this." };
  return { title: { auth: "Nebius rejected the credentials", model_not_found: "Model configuration problem", credits: "No credits left", rate_limit: "Rate limit reached", timeout: "Request timed out", network: "Network problem", config: "Live mode is not configured", browser: "Chromium is not available", internal: "Something went wrong on the server" }[ev.kind] || "The run stopped", hint: hints[ev.kind] || "" };
}

export function renderSummary(s, ui, el) {
  const c = counters(s), r = s.report;
  if (!s.before) { el.innerHTML = `<div class="empty">The summary appears when the audit has results.</div>`; return; }
  const issues = [...s.issues.values()];
  const sev = SEVERITY.map((k) => [k, issues.filter((i) => i.impact === k).length, issues.filter((i) => i.impact === k && !s.resolved.has(i.id)).length]);
  const maxSev = Math.max(1, ...sev.map((x) => x[1]));
  const v = c.verified, rb = s.patches.filter((p) => p.verdict === "rolled_back").length, rj = s.patches.filter((p) => p.verdict === "rejected").length, tot = v + rb + rj;
  const times = [...s.batches.values()].filter((b) => b.t0 && b.t1).map((b) => [b.id, b.t1 - b.t0, fmtSecs(b.t1 - b.t0)]);
  const maxT = Math.max(0, ...times.map((x) => x[1]));
  const e = r?.evidence || {}, w = e.warnings || [];
  const tv = Object.entries(e.tavily || {}).map(([k, n]) => `${k}: ${n}`).join(", ");
  const total = s.startedTs && s.endedTs ? s.endedTs - s.startedTs : null;
  if (r?.audit_only) { el.innerHTML = auditOnlySummary(r, esc); wireAuditOnly(r); return; }
  el.innerHTML = `
    <section class="card pad"><h2 class="h3">${r ? (r.after.total === 0 ? "All clear" : `${r.fixed} of ${r.total} issues fixed`) : "Run in progress"}</h2>
      <p class="fine">${r ? `${r.after.total} issue(s) remain. Every kept fix was re-tested by the same audit that found the problem.` : "Final numbers appear when the report is ready."}</p>
      <div class="actions" style="margin-top:12px"><button class="btn primary" id="dlPatch" ${r?.diff ? "" : "disabled"}>Download patch (.patch)</button>
        <button class="btn" id="dlPr" ${r ? "" : "disabled"}>Download PR description</button><button class="btn" id="cpPr" ${r ? "" : "disabled"}>Copy PR text</button><span class="toast" id="toast" role="status"></span></div>
      ${r && !r.diff ? `<p class="fine">No patch to download: no fix was verified.</p>` : ""}</section>
    <div class="sum-grid" style="margin-top:12px">
      <section class="card chart"><h3>Severity before and after</h3><p class="fine">Issues by axe-core impact level, at the first audit and now.</p>
        ${sev.some((x) => x[1]) ? `<div class="legend"><span><i style="background:var(--bad)"></i>Before</span><span><i style="background:var(--good)"></i>Now</span></div>` + sev.filter((x) => x[1]).map((x) => `<div class="pair"><span class="pl">${esc(x[0])}</span><span class="pb"><span class="bar"><b style="width:${(x[1] / maxSev) * 100}%"></b></span><span class="bar after"><b style="width:${x[2] ? Math.max(3, (x[2] / maxSev) * 100) : 0}%"></b></span></span><span class="pv">${x[1]} → ${x[2]}</span></div>`).join("") : `<p class="fine">No issues were found.</p>`}</section>
      <section class="card chart"><h3>Patch attempts</h3><p class="fine">${tot ? `${v} verified, ${rb} rolled back, ${rj} rejected.` : "No patch attempts yet."}</p>
        ${tot ? `<div class="stack" role="img" aria-label="${v} verified, ${rb} rolled back, ${rj} rejected"><i class="v" style="width:${v / tot * 100}%"></i><i class="rb" style="width:${rb / tot * 100}%"></i><i class="rj" style="width:${rj / tot * 100}%"></i></div>
        <div class="legend"><span><i class="v" style="background:var(--good)"></i>Verified</span><span><i style="background:var(--warn)"></i>Rolled back</span><span><i style="background:var(--bad)"></i>Rejected</span></div>` : ""}</section>
      <section class="card chart"><h3>Repair time per group</h3><p class="fine">${times.length ? `From first work on a group to its final status.${total != null ? ` Whole run: ${fmtSecs(total)}.` : ""}` : "Appears once a group has finished."}</p>
        ${bars(times, maxT, "neutral")}</section>
      <section class="card chart"><h3>Verification evidence</h3>
        <ul class="fine" style="padding-left:18px;margin:8px 0 0"><li>Engine: <b>${s.engine === "live" ? "Live: NVIDIA Nemotron via Nebius Token Factory" : "Scripted demo (no model calls)"}</b></li>
        ${r ? `<li>Original files unchanged: <b>${e.source_unchanged ? "yes (hash checked before and after)" : "NO, see warnings"}</b></li>` : ""}
        ${tv ? `<li>Tavily lookups: ${esc(tv)}</li>` : ""}${e.blocked_external?.length ? `<li>Blocked external hosts: ${esc(e.blocked_external.join(", "))}</li>` : ""}</ul></section>
    </div>
    ${w.length ? `<div class="limits"><b>Warnings</b><ul>${w.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${r ? `<section class="card pad" style="margin-top:12px"><h3 class="h3">Pull request description</h3><pre class="pr" tabindex="0" role="region" aria-label="Pull request description text">${esc(r.pr)}</pre></section>
    <section class="card pad" style="margin-top:12px"><h3 class="h3">Full patch</h3>${diffHtml(r.diff, false, ui, "full")}</section>` : ""}
    <div class="limits"><b>Limitations, stated plainly</b><ul>
      <li>Automated testing catches only part of WCAG. A clean result is not a compliance certificate.</li>
      <li>Generated alt text, labels and colours need human review before merging.</li>
      <li>Static sites only (HTML, CSS, JS). External hosts are blocked during the audit, so issues depending on them are not seen.</li></ul></div>`;
}

function auditOnlySummary(r, esc) {
  const e = r.evidence || {};
  return `<section class="card pad"><h2 class="h3">${r.total ? `${r.total} issue(s) found` : "No issues found"}</h2>
      <p class="fine">${esc(r.site_url || "")} · ${r.pages} page(s) audited in a real browser. Report only: nothing on your site was changed, and these suggestions are not verified fixes.</p>
      <div class="actions" style="margin-top:12px"><button class="btn primary" id="dlPr">Download report (.md)</button><button class="btn" id="cpPr">Copy report</button><span class="toast" id="toast" role="status"></span></div></section>
    <section class="card pad" style="margin-top:12px"><h3 class="h3">Suggested fixes</h3>
      ${(r.suggestions || []).length ? `<ul class="sugg">${r.suggestions.map((g) => `<li><div class="sg-h"><b>${esc(g.rule)}</b><span class="chip">${esc(g.impact)}</span><span class="fine">${g.count} found on ${g.pages.map(esc).join(", ")}</span></div>
        <p>${esc(g.advice)}</p>${g.example ? `<code class="sg-ex">${esc(g.example)}</code>` : ""}${g.help_url ? `<a href="${esc(g.help_url)}" target="_blank" rel="noopener noreferrer">Learn more about this rule</a>` : ""}</li>`).join("")}</ul>` : `<p class="fine">Nothing to fix from the automated checks.</p>`}</section>
    ${e.blocked_external?.length ? `<div class="limits"><b>Blocked during the audit</b><ul><li>Third-party hosts were not loaded for safety: ${esc(e.blocked_external.join(", "))}. Pages that depend on them may look incomplete.</li></ul></div>` : ""}
    <div class="limits"><b>Limitations, stated plainly</b><ul>
      <li>Automated testing catches only part of WCAG. A clean result is not a compliance certificate.</li>
      <li>This is an audit of the live pages only. To get patches that are verified in a browser, audit the source repo or a zip of the site.</li></ul></div>`;
}
function wireAuditOnly(r) {
  const toast = (t) => { const el = document.getElementById("toast"); if (el) { el.textContent = t; setTimeout(() => (el.textContent = ""), 2500); } };
  document.getElementById("dlPr")?.addEventListener("click", () => download("sitemortem-report.md", r.pr || ""));
  document.getElementById("cpPr")?.addEventListener("click", async () => { try { await navigator.clipboard.writeText(r.pr || ""); toast("Copied to clipboard"); } catch { toast("Copy failed: select the text manually"); } });
}

export { fmtBytes };
