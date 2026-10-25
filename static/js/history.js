/* Audit history: run summaries stored on the server under an anonymous cookie id (no login). */
import { esc, fmtSecs } from "./util.js";

let cache = [], offline = false;
/** History lives on the server under an anonymous cookie (no account). */
export async function loadHistory() {
  try { const r = await fetch("/api/history", { credentials: "same-origin" }); if (!r.ok) throw new Error(); cache = ((await r.json()).history || []).filter((x) => x && x.run); offline = false; }
  catch { offline = true; }
  return cache;
}
export async function clearHistory() {
  try { await fetch("/api/history", { method: "DELETE", credentials: "same-origin" }); } catch { /* shown as unchanged on next load */ }
  cache = [];
}
const MODE = { repo: "GitHub repo", url: "Live site", upload: "Zip upload", demo: "Demo" };

export function renderHistory(el, confirming) {
  const list = cache;
  if (offline && !list.length) { el.innerHTML = `<div class="empty"><b>History is unavailable</b><span>The server could not be reached. Your saved audits are kept and will show again once it is back.</span></div>`; return; }
  if (!list.length) { el.innerHTML = `<div class="empty"><b>No audits yet</b><span>Finished runs are listed here so you can compare results over time.</span><a class="btn sm primary" href="#/">Start an audit</a></div>`; return; }
  const max = Math.max(1, ...list.map((x) => x.before)), recent = list.slice(0, 8).reverse();
  el.innerHTML = `
    <section class="card chart" style="margin-bottom:12px"><h2 class="h3">Issues per audit, before and after</h2>
      <p class="fine">${list.length >= 2 ? "Most recent audits, oldest first." : "Run another audit to see a trend."}</p>
      ${recent.map((x, i) => `<div class="brow"><span>#${list.length - list.indexOf(x)}</span><span class="bar"><b style="width:${x.before / max * 100}%"></b></span><span>${x.before} before</span></div>
        <div class="brow"><span></span><span class="bar after"><b style="width:${x.after / max * 100}%"></b></span><span>${x.after} after</span></div>`).join("")}</section>
    <section class="card pad"><div class="tscroll" role="region" tabindex="0" aria-label="Saved audits"><table class="hist"><thead><tr><th scope="col">When</th><th scope="col">Audited</th><th scope="col">Before → after</th><th scope="col">Verified</th><th scope="col">Not fixed</th><th scope="col">Time</th></tr></thead><tbody>
      ${list.map((x) => `<tr><td>${new Date(x.ts).toLocaleString()}</td><td><b>${esc(MODE[x.mode] || "Audit")}</b>${x.target && x.mode !== "demo" ? `<small class="tgt">${esc(x.target)}</small>` : ""}<small class="tgt">${x.audit_only ? "Report only" : x.engine === "live" ? "Live engine" : "Scripted demo"}</small></td><td>${x.before} → ${x.after}</td><td>${x.verified}</td><td>${x.rejected}</td><td>${fmtSecs(x.secs)}</td></tr>`).join("")}</tbody></table></div>
      <p><button type="button" class="btn sm" id="clearHist">${confirming ? `Delete all ${list.length} saved audit(s)? Click again to confirm` : "Clear history"}</button></p></section>`;
}
