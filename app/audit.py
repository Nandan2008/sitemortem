"""Audit engine: crawl a site with headless Chromium and collect
  * accessibility violations (axe-core, WCAG 2.0/2.1 A + AA)
  * runtime problems (uncaught JS errors, console errors, failed / 4xx / 5xx requests)

Every finding becomes an `Issue` with a stable fingerprint so a later audit can be
compared with an earlier one to prove a fix worked and nothing new broke.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from playwright.sync_api import sync_playwright

AXE = Path(__file__).resolve().parent.parent / "static" / "vendor" / "axe.min.js"
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
IMPACT_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3, None: 4}
FRAME = re.compile(r"(https?://[^\s)]+?):(\d+):(\d+)")


@dataclass
class Issue:
    id: str
    kind: str            # "a11y" | "runtime"
    rule: str            # axe rule id, or js-error / console-error / http-error / request-failed
    impact: str          # critical | serious | moderate | minor
    message: str
    page: str            # path, e.g. "/about.html"
    selector: str = ""
    html: str = ""
    source: dict | None = None      # {"path": "/app.js", "line": 7} when known
    help_url: str = ""
    fingerprint: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(text: str) -> str:
    text = re.sub(r"https?://127\.0\.0\.1:\d+", "", text)
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip()[:160]


LOCAL_SCHEMES = ("data:", "blob:", "about:")
# Small hosts (e.g. 512 MB free tiers): set SITEMEDIC_LOW_MEMORY=1 to keep Chromium to one lean renderer.
LOW_MEMORY_ARGS = (["--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions", "--no-zygote",
                    "--renderer-process-limit=1", "--js-flags=--max-old-space-size=192"]
                   if os.getenv("SITEMEDIC_LOW_MEMORY", "").strip() in ("1", "true", "yes") else [])


class Auditor:
    """Headless Chromium that may ONLY talk to the local copy of the site being audited.

    Uploaded sites are untrusted: their JS could fetch internal addresses or cloud-metadata endpoints
    (SSRF). Every request to anything but the local origin is aborted, and DNS resolution is disabled
    in the browser as a second layer. Blocked hosts are reported as a limitation, not as site bugs.
    """

    def __init__(self, pins: dict[str, str] | None = None):
        self.blocked: set[str] = set()
        self.pins = pins or {}          # remote audits: {hostname: validated public IP}; empty = local copy only

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            args=[self._resolver_rules(), "--disable-background-networking", *LOW_MEMORY_ARGS])
        return self

    def _resolver_rules(self) -> str:
        if not self.pins:
            return "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1"
        maps = ", ".join(f"MAP {h} {ip}" for h, ip in self.pins.items())
        return f"--host-resolver-rules={maps}, MAP * ~NOTFOUND"

    def _ours(self, netloc: str) -> bool:
        host = netloc.split(":")[0].lower()
        return netloc == self._origin or (bool(self.pins) and host in self.pins)

    def __exit__(self, *a):
        self.browser.close()
        self._pw.stop()

    def audit(self, base_url: str, max_pages: int = 6) -> tuple[list[Issue], list[str]]:
        origin = urlparse(base_url).netloc
        self._origin = origin

        def canon(u: str) -> str:  # "", "/" and "/index.html" are the same page
            u = urldefrag(u)[0]
            if not urlparse(u).path:
                u += "/"
            return u[: -len("index.html")] if u.endswith("/index.html") else u

        queue, seen, issues = [canon(base_url)], set(), []
        while queue and len(seen) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            found, links = self._audit_page(url)
            issues += found
            for link in links:
                link = canon(urljoin(url, link))
                if self._ours(urlparse(link).netloc) and link not in seen and link not in queue:
                    queue.append(link)
        # stable ids + de-duplicate identical fingerprints
        uniq, fps = [], set()
        for i in issues:
            if i.fingerprint in fps:
                continue
            fps.add(i.fingerprint)
            uniq.append(i)
        for n, i in enumerate(uniq, 1):
            i.id = f"{'A' if i.kind == 'a11y' else 'R'}{n:02d}"
        return uniq, [urlparse(u).path or "/" for u in seen]

    # ------------------------------------------------------------------
    def _audit_page(self, url: str) -> tuple[list[Issue], list[str]]:
        path = urlparse(url).path or "/"
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")

        def gate(route):
            u = route.request.url
            if u.startswith(LOCAL_SCHEMES) or self._ours(urlparse(u).netloc):
                return route.continue_()
            self.blocked.add(urlparse(u).hostname or "unknown")
            return route.abort("blockedbyclient")

        ctx.route("**/*", gate)
        page = ctx.new_page()
        runtime: list[Issue] = []

        def add(rule, impact, message, source=None, extra=None):
            fp = f"runtime|{rule}|{path}|{_norm(message)}"
            runtime.append(Issue("", "runtime", rule, impact, message, path, source=source,
                                 fingerprint=fp, extra=extra or {}))

        def on_pageerror(err):
            stack = getattr(err, "stack", "") or ""
            m = FRAME.search(stack)
            src = {"path": urlparse(m.group(1)).path, "line": int(m.group(2))} if m else None
            add("js-error", "serious", f"{getattr(err, 'name', 'Error')}: {getattr(err, 'message', err)}", src)

        def on_console(msg):
            if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
                loc = msg.location or {}
                src = {"path": urlparse(loc.get("url", "")).path, "line": loc.get("lineNumber")} if loc.get("url") else None
                add("console-error", "moderate", msg.text, src)

        def on_response(resp):
            p = urlparse(resp.url).path
            if resp.status >= 400 and p != "/favicon.ico":
                add("http-error", "serious", f"{resp.request.method} {p} returned {resp.status}",
                    extra={"url_path": p, "status": resp.status})

        def on_failed(req):
            if not self._ours(urlparse(req.url).netloc):
                return          # blocked external request: reported as a limitation, not a site bug
            add("request-failed", "serious", f"{req.method} {urlparse(req.url).path} failed: {req.failure}")

        page.on("pageerror", on_pageerror)
        page.on("console", on_console)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

        try:
            page.goto(url, wait_until="load", timeout=20000)
            page.wait_for_timeout(700)  # let startup fetches and timers settle
            page.add_script_tag(path=str(AXE))
            res = page.evaluate(
                "(tags) => axe.run(document, {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']})",
                AXE_TAGS,
            )
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        except Exception as e:  # unreachable page is itself a finding
            ctx.close()
            fp = f"runtime|page-unreachable|{path}|{_norm(str(e))[:80]}"
            return [Issue("", "runtime", "page-unreachable", "critical", f"Page failed to load: {str(e)[:200]}", path, fingerprint=fp)], []

        found: list[Issue] = []
        for v in res["violations"]:
            for node in v["nodes"]:
                selector = " ".join(node["target"]) if node.get("target") else ""
                found.append(Issue(
                    "", "a11y", v["id"], v.get("impact") or "moderate",
                    f"{v['help']}. {node.get('failureSummary', '').replace(chr(10), ' ')}".strip(),
                    path, selector=selector, html=node.get("html", "")[:400],
                    help_url=v.get("helpUrl", ""),
                    fingerprint=f"a11y|{v['id']}|{path}|{selector}",
                ))
        ctx.close()
        return found + runtime, [l for l in links if l and not l.startswith(("mailto:", "tel:", "javascript:", "#"))]


NETWORK_RULES = {"http-error", "request-failed", "page-unreachable"}


def category(i: Issue) -> str:
    """a11y | js | network. Every issue belongs to exactly one, so the categories never double-count."""
    if i.kind == "a11y":
        return "a11y"
    return "network" if i.rule in NETWORK_RULES else "js"


def counts(issues: list[Issue]) -> dict:
    cats = [category(i) for i in issues]
    return {"a11y": cats.count("a11y"), "runtime": sum(i.kind == "runtime" for i in issues),
            "js": cats.count("js"), "network": cats.count("network"), "total": len(issues)}
