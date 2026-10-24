import { $, $$, esc, store, fmtBytes, download } from "./util.js";
import { apply, finalState, newState } from "./state.js";
import { Runner } from "./stream.js";
import { renderActivity, renderIssues, renderMetrics, renderModels, renderPatches, renderSummary, renderTimeline, renderUsage, errorView } from "./render.js";
import { clearHistory, renderHistory, saveRun } from "./history.js";

/* ------------------------------------------------------------------ theme */
const root = document.documentElement;
function setTheme(t) {
  root.dataset.theme = t;
  const label = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  $("#theme").setAttribute("aria-label", label); $("#theme").dataset.tip = label;
}
setTheme(store.get("sm_theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
$("#theme").onclick = () => { const t = root.dataset.theme === "dark" ? "light" : "dark"; setTheme(t); store.set("sm_theme", t); };

/* ------------------------------------------------------------------ globals */
let health = { live_available: false, default_mode: "demo", tavily: false };
let S = newState();
let file = null, submitting = false, showRun = false, confirmClear = false;
const ui = { filter: "all", q: "", sort: "severity", limit: 50, open: new Set(), tab: "issues" };
const announce = (t) => { $("#srStatus").textContent = ""; setTimeout(() => ($("#srStatus").textContent = t), 30); };
const engine = () => ($("#scripted").checked ? "demo" : "live");
const running = () => S.status === "running";

/* ------------------------------------------------------------------ routing */
function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const view = h === "history" ? "history" : h === "help" ? "help" : showRun ? "run" : "launch";
  for (const v of ["launch", "run", "history", "help"]) $(`#view-${v}`).hidden = v !== view;
  $$(".links a").forEach((a) => { const on = a.dataset.route === (view === "run" || view === "launch" ? "dashboard" : view); on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"); });
  if (view === "history") { renderHistory($("#historyBody"), confirmClear); }
  const target = $({ launch: "#heroTitle", run: "#runTitle", history: "#histTitle", help: "#helpTitle" }[view]);
  if (!route.first) { target.setAttribute("tabindex", "-1"); target.focus({ preventScroll: true }); window.scrollTo({ top: 0 }); }
  route.first = false;
  if (view === "run") schedule(true);
  updateGo();
}
route.first = true;
addEventListener("hashchange", route);
$("#cta").addEventListener("click", (e) => { e.preventDefault(); if (location.hash && location.hash !== "#/") location.hash = "#/"; $("#form").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); $("input[name=mode]:checked").focus(); });

/* ------------------------------------------------------------------ launch panel */
function renderPill() {
  const p = $("#modePill"), live = (running() || finalState(S)) && S.engine ? S.engine === "live" : engine() === "live";
  p.textContent = live ? `Live · Nemotron on Nebius${health.tavily ? " + Tavily" : ""}` : health.live_available ? "Demo · scripted fixes, real audit" : "Demo mode · live unavailable";
  p.classList.toggle("demo", !live);
}
function renderEngineNote() {
  const mode = $("input[name=mode]:checked").value, el = $("#engineNote");
  if (engine() === "demo") {
    el.textContent = "Scripted demo mode makes no live model calls. Patches are scripted and only cover the bundled storefront; the audit and every verification are real." +
      (mode === "upload" ? " For your own site, switch to live mode: scripted demo can audit it but has no fixes for it." : "") +
      (!health.live_available ? " Live mode is unavailable because NEBIUS_API_KEY is not configured on the server (see Help)." : "");
  } else {
    el.textContent = "Live mode calls NVIDIA Nemotron on Nebius Token Factory; the source files relevant to each issue are sent to the model provider. Tavily guidance: " +
      (health.tavily ? "configured." : "not configured, so it will be skipped and reported as skipped.");
  }
  renderPill();
}
function showLaunchError(title, msg, extra = "") {
  const el = $("#launchError");
  el.innerHTML = `<b>${esc(title)}</b>${esc(msg)}${extra}`; el.hidden = false;
}
const clearLaunchError = () => { $("#launchError").hidden = true; };
function updateGo() {
  const busy = submitting || running();
  $("#go").disabled = busy;
  $("#go .spin").hidden = !busy;
  $("#goText").textContent = submitting ? "Starting…" : running() ? "Audit in progress" : "Run audit";
  $("#goNote").innerHTML = running() ? `<a href="#/" id="viewRun">View progress</a>` : "";
  $("#viewRun")?.addEventListener("click", (e) => { e.preventDefault(); showRun = true; route(); });
}

fetch("/api/health").then((r) => r.json()).then((h) => {
  health = { ...health, ...h };
  const cb = $("#scripted");
  if (!h.live_available) { cb.checked = true; cb.disabled = true; } else { cb.checked = h.default_mode !== "live"; }
  renderEngineNote();
}).catch(() => { $("#modePill").textContent = "Server unreachable"; showLaunchError("Cannot reach the SiteMortem server", " Start it with: uvicorn app.main:app"); });

$$("input[name=mode]").forEach((r) => r.addEventListener("change", () => {
  $$(".opt").forEach((o) => o.classList.toggle("on", o.querySelector("input").checked));
  $("#uploadBox").hidden = $("input[name=mode]:checked").value !== "upload";
  clearLaunchError(); renderEngineNote();
}));
$("#scripted").addEventListener("change", () => { clearLaunchError(); renderEngineNote(); });

/* upload */
const MAX_ZIP = 8 * 1024 * 1024;
function setFile(f) {
  file = null; clearLaunchError();
  const row = $("#fileRow"), chip = $("#zipChip"), status = $("#zipStatus");
  if (!f) { row.hidden = true; $("#drop").hidden = false; status.innerHTML = `No file selected. <a href="/api/demo-site.zip">Download the demo site as a zip</a> to try the upload path.`; $("#zip").value = ""; return; }
  row.hidden = false; $("#drop").hidden = true; $("#fileName").textContent = f.name; $("#fileSize").textContent = fmtBytes(f.size);
  let err = "";
  if (!/\.zip$/i.test(f.name)) err = "This is not a .zip file.";
  else if (f.size > MAX_ZIP) err = "The file is larger than 8 MB.";
  else if (f.size === 0) err = "The file is empty.";
  chip.className = "chip " + (err ? "bad" : "ok"); chip.textContent = err ? "Invalid" : "Ready to upload";
  status.textContent = err || "File looks fine. The server checks the archive again when the audit starts.";
  if (!err) file = f;
}
$("#pick").onclick = $("#replaceZip").onclick = () => $("#zip").click();
$("#clearZip").onclick = () => setFile(null);
$("#zip").addEventListener("change", (e) => setFile(e.target.files[0] || null));
const drop = $("#drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0] || null));

/* submit */
$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submitting || running()) return;                        // never start a duplicate audit
  clearLaunchError();
  const mode = $("input[name=mode]:checked").value;
  if (mode === "upload" && !file) { showLaunchError("Choose a valid .zip first", " Pick a static site archive (up to 8 MB)."); return; }
  const fd = new FormData(); fd.append("mode", mode); fd.append("engine", engine());
  if (mode === "upload") fd.append("site", file);
  submitting = true; updateGo();
  const res = await runner.start(fd);
  submitting = false;
  if (res.error) {
    updateGo();
    const d = res.error.detail;
    const key = /NEBIUS_API_KEY/.test(d);
    showLaunchError(key ? "Live mode isn't configured" : res.error.status === 429 ? "Please wait" : res.error.status === 0 ? "Server unreachable" : "The audit could not start", " " + d,
      key && !$("#scripted").disabled ? `<div class="actions"><button type="button" class="btn sm" id="useDemo">Switch to scripted demo</button></div>` : "");
    $("#useDemo")?.addEventListener("click", () => { $("#scripted").checked = true; clearLaunchError(); renderEngineNote(); });
    return;
  }
  S = newState(); S.status = "running"; S.engine = engine(); ui.open.clear(); ui.limit = 50; setTab("issues");
  showRun = true; location.hash = "#/"; route(); schedule(true); announce("Audit started.");
});

/* ------------------------------------------------------------------ run stream */
const runner = new Runner({
  onEvent(ev) {
    if (!apply(S, ev)) return;
    if (ev.type === "audit") announce(`Browser audit completed: ${ev.counts.total} issues found.`);
    if (ev.type === "evidence" && ev.stage === "result") announce(`${ev.id} attempt ${ev.attempt}: ${ev.verdict.replace("_", " ")}.`);
    if (ev.type === "report") { saveRun(S); announce(`Report ready. ${ev.fixed} of ${ev.total} issues fixed.`); }
    if (ev.type === "error") announce(`Run stopped. ${ev.message}`);
    if (ev.type === "done" || ev.type === "error") { clearBanner(); updateGo(); }
    schedule();
  },
  onConn(state, n) {
    if (state === "reconnecting") banner("warn", "Connection lost", ` Reconnecting (attempt ${n})… the run continues on the server and resumes from the last event received.`);
    else if (state === "ok") { clearBanner(); announce("Reconnected."); }
    else if (state === "failed") banner("", "Connection lost", " Could not reconnect. The run may still be finishing on the server. Start a new audit when you're ready.", true);
    else if (state === "expired") banner("", "Run expired", " The server no longer has this run's events. Start a new audit.", true);
    if (state === "failed" || state === "expired") { S.status = "error"; S.error = { message: "Connection lost", kind: "network" }; updateGo(); schedule(); }
  },
});
function banner(kind, title, msg, withNew) {
  const b = $("#banner"); b.className = "alert " + kind; b.hidden = false;
  b.innerHTML = `<b>${esc(title)}</b>${esc(msg)}${withNew ? `<div class="actions"><button type="button" class="btn sm" id="bannerNew">Start new audit</button></div>` : ""}`;
  $("#bannerNew")?.addEventListener("click", newAudit);
}
const clearBanner = () => { $("#banner").hidden = true; };
function newAudit() { showRun = false; clearBanner(); route(); $("#heroTitle").focus(); }
$("#again").onclick = newAudit;
$("#stop").onclick = async () => {
  S.cancelRequested = true; $("#stop").disabled = true;
  banner("info", "Stopping", " The run will stop after its current step. Fixes verified so far are kept.");
  if (!(await runner.cancel())) banner("warn", "Could not send the stop request", " The run will finish on its own.");
  schedule();
};

/* ------------------------------------------------------------------ rendering */
let raf = 0, lastVersion = -1;
function schedule(force) { if (force) lastVersion = -1; if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); }
function render() {
  if ($("#view-run").hidden) return;
  if (S.version === lastVersion) return;
  lastVersion = S.version;
  renderMetrics(S, $("#metrics")); renderTimeline(S, $("#timeline")); renderActivity(S, $("#activity"), $("#actEmpty"), ui);
  renderModels(S, $("#modelsBox")); renderUsage(S, $("#usage"));
  const fin = finalState(S);
  const chip = $("#runStatus");
  const [cls, label] = S.status === "error" ? ["bad", "Stopped"] : fin ? [S.cancelRequested ? "warn" : "ok", S.cancelRequested ? "Stopped by you" : "Complete"] : S.cancelRequested ? ["warn", "Stopping"] : ["info work", "Running"];
  chip.className = "chip " + cls; chip.textContent = label;
  $("#stop").hidden = fin; $("#stop").disabled = S.cancelRequested || !S.run; $("#again").hidden = !fin;
  $("#runMeta").textContent = S.run ? `Run ${S.run} · ${S.engine === "live" ? "live engine" : "scripted demo engine"}` : "Starting…";
  renderPill();
  if (S.status === "error" && S.error && S.error.kind !== "network") {
    const v = errorView(S.error), b = $("#banner");
    b.className = "alert"; b.hidden = false;
    b.innerHTML = `<b>${esc(v.title)}</b>${esc(S.error.message)}${v.hint ? `<br>${esc(v.hint)}` : ""}${S.error.ref ? `<br><span class="fine">Error ID: ${esc(S.error.ref)}. Nothing in your original files was changed.</span>` : ""}<div class="actions"><button type="button" class="btn sm" id="copyErr">Copy error details</button><span class="toast" id="copyErrDone" role="status"></span></div>`;
    $("#copyErr").onclick = async () => {
      const t = [v.title, S.error.message, `Kind: ${S.error.kind || "unknown"}`, S.error.ref ? `Error ID: ${S.error.ref}` : ""].filter(Boolean).join("\n");
      try { await navigator.clipboard.writeText(t); $("#copyErrDone").textContent = "Copied"; } catch { $("#copyErrDone").textContent = "Copy failed. Select the text above instead."; }
    };
  }
  if (ui.tab === "issues") renderIssues(S, ui, { cnt: $("#cnt-issues"), filters: $("#filters"), list: $("#issueList"), count: $("#issueCount"), more: $("#moreIssues") });
  else $("#cnt-issues").textContent = S.issues.size;
  if (ui.tab === "patches") renderPatches(S, ui, { cnt: $("#cnt-patches"), list: $("#patchList"), summary: $("#patchSummary") });
  else $("#cnt-patches").textContent = S.patches.length;
  if (ui.tab === "summary") { renderSummary(S, ui, $("#summary")); wireSummary(); }
}

/* tabs (arrow keys, roving tabindex) */
const TABS = ["issues", "patches", "summary"];
function setTab(t) {
  ui.tab = t;
  for (const k of TABS) { const on = k === t; const b = $(`#tab-${k}`); b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1; $(`#panel-${k}`).hidden = !on; }
  schedule(true);
}
$$('[role="tab"]').forEach((b) => {
  b.addEventListener("click", () => setTab(b.id.replace("tab-", "")));
  b.addEventListener("keydown", (e) => {
    const i = TABS.indexOf(ui.tab), d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (d || e.key === "Home" || e.key === "End") { e.preventDefault(); const n = e.key === "Home" ? 0 : e.key === "End" ? 2 : (i + d + 3) % 3; setTab(TABS[n]); $(`#tab-${TABS[n]}`).focus(); }
  });
});

/* issue toolbar */
let qTimer;
$("#issueList").addEventListener("click", (e) => {
  if (!e.target.closest("[data-clear-filters]")) return;
  ui.filter = "all"; ui.q = ""; ui.limit = 50; $("#q").value = ""; schedule(true); $('#filters [data-f="all"]')?.focus();
});
$("#q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { ui.q = e.target.value; ui.limit = 50; schedule(true); }, 150); });
$("#sort").addEventListener("change", (e) => { ui.sort = e.target.value; schedule(true); });
$("#filters").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (!b) return; ui.filter = b.dataset.f; ui.limit = 50; schedule(true); $(`#filters [data-f="${ui.filter}"]`)?.focus(); });
$("#moreIssues").addEventListener("click", () => { ui.limit += 50; schedule(true); });
/* remember which <details> are open across re-renders */
document.addEventListener("toggle", (e) => { const d = e.target; if (!(d instanceof HTMLDetailsElement) || !d.dataset.key) return; d.open ? ui.open.add(d.dataset.key) : ui.open.delete(d.dataset.key); }, true);
$("#expandAll").onclick = () => { $$("#patchList details").forEach((d) => { d.open = true; if (d.dataset.key) ui.open.add(d.dataset.key); }); };
$("#collapseAll").onclick = () => { $$("#patchList details").forEach((d) => { d.open = false; }); S.patches.forEach((p) => [...ui.open].filter((k) => k.startsWith(`p-${p.id}-${p.attempt}`)).forEach((k) => ui.open.delete(k))); };

function wireSummary() {
  const r = S.report; if (!r) return;
  const toast = (t) => { const el = $("#toast"); if (el) { el.textContent = t; setTimeout(() => (el.textContent = ""), 2500); } };
  $("#dlPatch")?.addEventListener("click", () => r.diff && download("sitemortem.patch", r.diff));
  $("#dlPr")?.addEventListener("click", () => download("PR.md", r.pr || ""));
  $("#cpPr")?.addEventListener("click", async () => { try { await navigator.clipboard.writeText(r.pr || ""); toast("Copied to clipboard"); } catch { toast("Copy failed: select the text manually"); } });
}

/* history view */
$("#historyBody").addEventListener("click", (e) => {
  if (e.target.id !== "clearHist") return;
  if (!confirmClear) { confirmClear = true; renderHistory($("#historyBody"), true); $("#clearHist").focus(); return; }
  clearHistory(); confirmClear = false; renderHistory($("#historyBody"), false);
});

addEventListener("pagehide", () => runner.abort());
route();
renderEngineNote();
