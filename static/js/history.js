/* Audit history: summaries of past runs kept in this browser only (localStorage). */
import { esc, store, fmtSecs } from "./util.js";
const KEY = "sm_history", MAX = 20;

export const loadHistory = () => store.get(KEY, []).filter((x) => x && x.run);
export function saveRun(s) {
  const r = s.report; if (!r) return;
  const list = loadHistory().filter((x) => x.run !== s.run);
  list.unshift({ run: s.run, ts: Date.now(), engine: s.engine, before: r.before.total, after: r.after.total, fixed: r.fixed,
    verified: s.patches.filter((p) => p.verdict === "verified").length, rejected: s.patches.filter((p) => ["rolled_back", "rejected"].includes(p.verdict)).length,
    secs: r.evidence?.elapsed_s ?? null });
  store.set(KEY, list.slice(0, MAX));
}
export const clearHistory = () => store.set(KEY, []);

export function renderHistory(el, confirming) {
  const list = loadHistory();
  if (!list.length) { el.innerHTML = `<div class="empty"><b>No audits yet</b><span>Finished runs are listed here so you can compare results over time.</span><a class="btn sm primary" href="#/">Start an audit</a></div>`; return; }
  const max = Math.max(1, ...list.map((x) => x.before)), recent = list.slice(0, 8).reverse();
  el.innerHTML = `
    <section class="card chart" style="margin-bottom:12px"><h2 class="h3">Issues per audit, before and after</h2>
      <p class="fine">${list.length >= 2 ? "Most recent audits, oldest first." : "Run another audit to see a trend."}</p>
      ${recent.map((x, i) => `<div class="brow"><span>#${list.length - list.indexOf(x)}</span><span class="bar"><b style="width:${x.before / max * 100}%"></b></span><span>${x.before} before</span></div>
        <div class="brow"><span></span><span class="bar after"><b style="width:${x.after / max * 100}%"></b></span><span>${x.after} after</span></div>`).join("")}</section>
    <section class="card pad"><div class="tscroll" role="region" tabindex="0" aria-label="Saved audits"><table class="hist"><thead><tr><th scope="col">When</th><th scope="col">Engine</th><th scope="col">Before → after</th><th scope="col">Verified</th><th scope="col">Rejected</th><th scope="col">Time</th></tr></thead><tbody>
      ${list.map((x) => `<tr><td>${new Date(x.ts).toLocaleString()}</td><td>${x.engine === "live" ? "Live" : "Scripted demo"}</td><td>${x.before} → ${x.after}</td><td>${x.verified}</td><td>${x.rejected}</td><td>${fmtSecs(x.secs)}</td></tr>`).join("")}</tbody></table></div>
      <p><button type="button" class="btn sm" id="clearHist">${confirming ? `Delete all ${list.length} saved audit(s)? Click again to confirm` : "Clear history"}</button></p></section>`;
}
