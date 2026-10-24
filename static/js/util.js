export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const store = {
  get(k, d = null) { try { const v = JSON.parse(localStorage.getItem(k)); return v ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable: UI still works */ } },
};

export const SEVERITY = ["critical", "serious", "moderate", "minor"];
export const sevRank = (s) => { const i = SEVERITY.indexOf(s); return i < 0 ? 9 : i; };
export const NETWORK_RULES = new Set(["http-error", "request-failed", "page-unreachable"]);

export const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
export const fmtSecs = (s) => (s == null ? "n/a" : s < 10 ? `${s.toFixed(1)}s` : s < 90 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
export const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "");

const RULE_TITLES = {
  "image-alt": "Image has no alternative text", "button-name": "Button has no accessible name",
  "link-name": "Link has no accessible name", label: "Form field has no label", "select-name": "Select has no accessible name",
  "color-contrast": "Text colour contrast is too low", "html-has-lang": "Page has no language set",
  "js-error": "Uncaught JavaScript error", "console-error": "Console error", "http-error": "Request returned an error status",
  "request-failed": "Network request failed", "page-unreachable": "Page failed to load",
};
export function issueTitle(i) {
  if (i.category !== "a11y") return i.message.split("\n")[0].slice(0, 110);
  return RULE_TITLES[i.rule] || i.rule.replace(/-/g, " ");
}
export const issueWhere = (i) => (i.source?.path ? `${i.source.path}${i.source.line ? ":" + i.source.line : ""}` : i.page) + (i.selector && i.category === "a11y" ? ` · ${i.selector}` : "");

export function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Parse a unified diff into files with line numbers and +/- counts. */
export function parseDiff(text) {
  const files = []; let f = null, a = 0, b = 0;
  for (const line of String(text || "").split("\n")) {
    if (line.startsWith("--- ")) { f = { name: "", add: 0, del: 0, lines: [] }; files.push(f); f.name = line.slice(6); continue; }
    if (!f) continue;
    if (line.startsWith("+++ ")) { f.name = line.slice(6) || f.name; continue; }
    const h = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (h) { a = +h[1]; b = +h[2]; f.lines.push({ t: "h", text: line }); continue; }
    if (line.startsWith("+")) { f.add++; f.lines.push({ t: "a", n: b++, text: line.slice(1) }); }
    else if (line.startsWith("-")) { f.del++; f.lines.push({ t: "d", n: a++, text: line.slice(1) }); }
    else if (line !== "") { f.lines.push({ t: "c", n: b, text: line.slice(1) }); a++; b++; }
  }
  return files;
}
