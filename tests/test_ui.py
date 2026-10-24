"""Dashboard tests: a real browser against the real backend (scripted demo engine, no keys, no network)."""
import io
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(os.getenv("SITEMEDIC_SHOTS", ROOT / ".test-shots"))
AXE = (ROOT / "static" / "vendor" / "axe.min.js").read_text()
VIEWPORTS = [(360, 740), (390, 844), (768, 1024), (1280, 800), (1920, 1080)]


def start_server(env_extra=None):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    env = {**os.environ, "NEBIUS_API_KEY": "", "TAVILY_API_KEY": "", "SITEMEDIC_MODE": "demo", **(env_extra or {})}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env)
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                return proc, f"http://127.0.0.1:{port}"
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def server():
    proc, url = start_server()
    yield url
    proc.terminate(); proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def wait_idle(server):
    """A previous test's run may still be finishing on the shared server (one run at a time)."""
    for _ in range(240):
        if not httpx.get(f"{server}/api/health").json().get("busy"):
            return
        time.sleep(0.5)
    raise RuntimeError("server stayed busy")


def new_page(browser, server, problems=None, **ctx):
    wait_idle(server)
    page = browser.new_context(viewport={"width": 1280, "height": 900}, **ctx).new_page()
    if problems is not None:
        # network failures (Google Fonts blocked in a sandbox) are not app bugs; CSP violations and JS errors are
        page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource") and problems.append(m.text))
        page.on("pageerror", lambda e: problems.append(str(e)))
    page.goto(server)
    page.wait_for_function("document.querySelector('#modePill').textContent !== 'Checking…'")
    return page


def run_demo(page):
    page.click("#go")
    expect(page.locator("#runStatus")).to_have_text("Complete", timeout=180_000)


@pytest.fixture
def fresh(browser, server):
    """A new page that is always closed, even if the test fails."""
    page = new_page(browser, server)
    yield page
    page.context.close()


@pytest.fixture(scope="module")
def problems():
    return []


@pytest.fixture(scope="module")
def done(browser, server, problems):
    """One completed demo run shared by the read-only tests below."""
    SHOTS.mkdir(exist_ok=True)
    page = new_page(browser, server, problems)
    page.screenshot(path=str(SHOTS / "01-launch.png"), full_page=True)
    run_demo(page)
    page.screenshot(path=str(SHOTS / "02-run-issues.png"), full_page=True)
    return page


def axe(page):
    page.evaluate(AXE)
    res = page.evaluate("""() => axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa']}})""")
    return [(v["id"], v["impact"], [n["target"] for n in v["nodes"]][:4]) for v in res["violations"]]


def overflow(page):
    return page.evaluate("document.documentElement.scrollWidth - window.innerWidth")


# ------------------------------------------------------------------------------------ launch page
def test_launch_page_content_and_demo_mode_state(fresh):
    page = fresh
    assert "Find what's broken." in page.inner_text("h1") and "Fix it. Prove it worked." in page.inner_text("h1")
    assert "verify every accepted fix in a real browser" in page.inner_text(".sub")
    assert "NVIDIA Nemotron" in page.text_content(".eyebrow") and "Nebius Token Factory" in page.text_content(".eyebrow")
    assert [a.inner_text() for a in page.locator(".links a").all()] == ["Dashboard", "History", "Help"]
    assert "Demo mode" in page.inner_text("#modePill") and "unavailable" in page.inner_text("#modePill")
    cb = page.locator("#scripted")
    assert cb.is_checked() and cb.is_disabled()                        # no key: live cannot be selected
    assert "no live model calls" in page.inner_text("#engineNote") and "NEBIUS_API_KEY" in page.inner_text("#engineNote")
    assert page.locator(".steps li").count() == 4


def test_every_nav_target_exists_and_nothing_is_a_dead_control(fresh):
    page = fresh
    base = page.url.split("#")[0]
    for route, heading in [("history", "Audit history"), ("help", "Help"), ("", "Find what's broken.")]:
        page.goto(f"{base}#/{route}")
        assert heading in page.inner_text("main h1:visible")
    for b in page.locator("button:visible").all():
        assert b.inner_text().strip() or b.get_attribute("aria-label"), "button without a name"


def test_live_unavailable_message_and_upload_validation(fresh):
    page = fresh
    page.click("label.opt:has(input[value=upload])")
    page.click("#go")                                                   # no file chosen
    assert "Choose a valid .zip" in page.inner_text("#launchError")
    page.set_input_files("#zip", {"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hi"})
    assert page.inner_text("#zipChip") == "Invalid" and "not a .zip" in page.inner_text("#zipStatus")
    assert page.inner_text("#fileName") == "notes.txt" and page.inner_text("#fileSize") == "2 B"
    page.click("#clearZip")
    assert page.locator("#fileRow").is_hidden() and "No file selected" in page.inner_text("#zipStatus")


def test_server_side_zip_rejection_is_shown_on_the_launch_panel(fresh):
    page = fresh
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.html", "<html></html>"); z.writestr("../evil.html", "x")
    page.click("label.opt:has(input[value=upload])")
    page.set_input_files("#zip", {"name": "evil.zip", "mimeType": "application/zip", "buffer": buf.getvalue()})
    assert page.inner_text("#zipChip") == "Ready to upload"
    page.click("#go")
    expect(page.locator("#launchError")).to_contain_text("leave the upload folder")
    assert page.locator("#view-run").is_hidden() and not page.locator("#go").is_disabled()   # stays on the launch page


# ------------------------------------------------------------------------------------ completed run
def test_metrics_timeline_and_activity_reflect_real_events(done):
    page = done
    vals = {m.locator("small").text_content(): m.locator(".now").text_content() for m in page.locator(".metric").all()}
    total = int(page.locator(".metric:has-text('Issues resolved') .was").inner_text().split()[-1])
    assert vals["Accessibility violations"] == vals["JavaScript errors"] == vals["Network failures"] == "0"
    assert vals["Issues resolved"] == str(total) and vals["Issues remaining"] == "0" and total > 20
    assert vals["Verified patches"] == "8" and vals["Rejected patches"] == "0"
    rows = {li.locator(".tl-n").inner_text(): li.get_attribute("data-s") for li in page.locator("#timeline li").all()}
    assert rows["Browser audit"] == rows["Issue triage"] == rows["Verification re-audit"] == rows["Final report"] == "completed"
    assert rows["Root-cause analysis"] == "skipped" and rows["Guidance retrieval"] == "skipped"   # scripted demo: nothing simulated
    assert rows["Retry / rollback"] == "skipped"
    assert page.locator("#activity li").filter(has_text="Browser audit completed").count() == 1
    assert "Scripted demo" in page.text_content("#modelsBox")


def test_issue_explorer_filters_search_and_sort(done):
    page = done
    n_all = page.locator("#filters [data-f=all] .n").inner_text()
    assert page.locator("#filters [data-f=resolved] .n").inner_text() == n_all           # all resolved after the demo
    assert page.locator("#filters [data-f=unresolved] .n").inner_text() == "0"
    page.click("#filters [data-f=network]")
    expect(page.locator("#filters [data-f=network]")).to_have_attribute("aria-pressed", "true")
    n_net = int(page.locator("#filters [data-f=network] .n").inner_text())
    assert n_net >= 1
    expect(page.locator("#issueList .issue")).to_have_count(n_net)
    page.click("#filters [data-f=unresolved]")
    expect(page.locator("#issueList")).to_contain_text("No issues match")
    page.click("#filters [data-f=all]")
    expect(page.locator("#filters [data-f=all]")).to_have_attribute("aria-pressed", "true")
    page.fill("#q", "html-has-lang")
    expect(page.locator("#issueList .issue")).to_have_count(2)
    page.fill("#q", "zzz-nothing")
    expect(page.locator("#issueList")).to_contain_text("No issues match")
    page.fill("#q", "")
    page.select_option("#sort", "file")
    page.select_option("#sort", "status")
    first = page.locator("#issueList .issue").first
    first.locator("summary").click()
    expect(first).to_contain_text("Rule")
    assert "Resolved" in first.inner_text()


def test_patch_review_shows_evidence_and_diffs(done):
    page = done
    page.click("#tab-patches")
    cards = page.locator("#patchList .patch")
    expect(cards).to_have_count(8)
    assert page.locator("#patchList .stat.verified").count() == 8
    cards.first.locator(":scope > summary").click()
    expect(cards.first).to_contain_text("Original issue")
    text = cards.first.inner_text()
    for needle in ("Original issue", "Proposed repair", "Post-patch audit", "Targeted issue disappeared?", "New issues appeared?", "Accepted: kept in the final patch"):
        assert needle in text
    page.click("#expandAll")
    expect(page.locator("#patchList details[open]")).not_to_have_count(0)
    assert page.locator("#patchList details[open]").count() > 8
    assert page.locator("#patchList tr.a").count() > 0 and page.locator("#patchList tr.d").count() > 0
    page.click("#collapseAll")
    assert page.locator("#patchList details[open]").count() == 0
    page.screenshot(path=str(SHOTS / "03-patches.png"), full_page=True)


def test_summary_charts_downloads_and_evidence(done):
    page = done
    page.click("#tab-summary")
    expect(page.locator("#dlPatch")).to_be_enabled()
    assert page.locator("#dlPr").is_enabled()
    with page.expect_download() as d:
        page.click("#dlPatch")
    patch = Path(d.value.path()).read_text()
    assert patch.startswith("--- a/") and "+++ b/" in patch and 'lang="en"' in patch
    text = page.inner_text("#summary")
    assert "All clear" in text and "Original files unchanged: yes" in text and "Scripted demo (no model calls)" in text
    assert "Severity before and after" in text and "Patch attempts" in text and "Repair time per group" in text
    page.screenshot(path=str(SHOTS / "04-summary.png"), full_page=True)


def test_history_records_the_run(done):
    page = done
    page.goto(page.url.split("#")[0] + "#/history")
    assert page.locator("table.hist tbody tr").count() >= 1 and "Scripted demo" in page.inner_text("table.hist")
    page.screenshot(path=str(SHOTS / "05-history.png"), full_page=True)


def test_no_console_or_csp_errors_during_the_whole_session(done, problems):
    assert problems == [], problems


# ------------------------------------------------------------------------------------ interaction safety
def test_duplicate_submissions_are_prevented(fresh):
    page = fresh
    posts = []
    page.on("request", lambda r: r.method == "POST" and r.url.endswith("/api/run") and posts.append(r.url))
    # three submits in the same tick (double-click, Enter key, script): only the first may reach the server
    page.evaluate("const f = document.querySelector('#form'); f.requestSubmit(); f.requestSubmit(); f.requestSubmit();")
    expect(page.locator("#runStatus")).to_have_text("Complete", timeout=180_000)
    assert len(posts) == 1


def test_stop_run_ends_it_honestly(fresh):
    page = fresh
    page.click("#go")
    expect(page.locator("#stop")).to_be_enabled(timeout=20_000)
    page.click("#stop")
    expect(page.locator("#runStatus")).to_have_text("Stopped by you", timeout=120_000)
    page.click("#tab-summary")
    expect(page.locator("#summary")).to_contain_text("stopped by the user")


def test_dropped_connection_resumes_without_duplicate_events(fresh):
    page = fresh

    def cut(route):                                                      # deliver only the first 4 events, then end the stream
        resp = route.fetch(timeout=600_000)       # a full run can take minutes on a slow machine
        frames = resp.text().split("\n\n")[:4]
        route.fulfill(status=200, headers={"content-type": "text/event-stream"}, body="\n\n".join(frames) + "\n\n")

    page.route("**/api/run", cut)
    page.click("#go")
    expect(page.locator("#banner")).to_contain_text("Reconnecting", timeout=300_000)
    expect(page.locator("#runStatus")).to_have_text("Complete", timeout=300_000)   # resumed from the server's event log
    assert page.locator("#activity li").filter(has_text="Browser audit completed").count() == 1
    assert page.locator(".metric:has-text('Issues remaining') .now").inner_text() == "0"
    assert page.locator("#banner").is_hidden()


def test_server_error_event_is_shown_with_an_id_and_hint(fresh):
    page = fresh
    body = ('data: {"type":"run","run":"r1","seq":0,"ts":1700000000,"engine":"demo"}\n\n'
            'data: {"type":"error","seq":1,"ts":1700000001,"kind":"auth","message":"Nebius rejected the API key (401).","ref":"ab12cd34"}\n\n')
    page.route("**/api/run", lambda r: r.fulfill(status=200, headers={"content-type": "text/event-stream"}, body=body))
    page.click("#go")
    expect(page.locator("#runStatus")).to_have_text("Stopped")
    b = page.inner_text("#banner")
    assert "Nebius rejected the credentials" in b and "401" in b and "ab12cd34" in b and "NEBIUS_API_KEY" in b
    assert "Traceback" not in b


# ------------------------------------------------------------------------------------ accessibility & responsive
def test_axe_launch_history_and_help_in_both_themes(fresh):
    page, found = fresh, {}
    page.click("label.opt:has(input[value=upload])")
    page.set_input_files("#zip", {"name": "site.zip", "mimeType": "application/zip", "buffer": b"PK"})   # shows the file row
    for theme in ("light", "dark"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        for name, route in [("launch", "#/"), ("history", "#/history"), ("help", "#/help")]:
            page.evaluate(f"location.hash = '{route}'")
            page.wait_for_timeout(100)
            v = axe(page)
            if v:
                found[f"{theme}/{name}"] = v
    assert found == {}, found


def test_axe_run_views_in_both_themes(done):
    page, found = done, {}
    page.evaluate("location.hash = '#/'")
    expect(page.locator("#view-run")).to_be_visible()
    for theme in ("light", "dark"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        for tab in ("issues", "patches", "summary"):
            page.click(f"#tab-{tab}")
            if tab == "patches":
                page.click("#expandAll")
            if tab == "issues":
                page.locator("#issueList .issue summary").first.click()
            v = axe(page)
            if v:
                found[f"{theme}/{tab}"] = v
    page.evaluate("document.documentElement.dataset.theme = 'light'")
    assert found == {}, found


def test_no_horizontal_overflow_at_common_viewport_sizes(done):
    page = done
    page.evaluate("location.hash = '#/'")
    for w, h in VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        for view in ("launch", "run"):
            if view == "run":
                page.evaluate("document.querySelector('#view-launch').hidden = true; document.querySelector('#view-run').hidden = false")
                for t in ("issues", "patches", "summary"):
                    page.click(f"#tab-{t}")
                    if t == "patches":
                        page.click("#expandAll")
                    assert overflow(page) <= 1, f"overflow {overflow(page)}px at {w}x{h} in run/{t}"
            else:
                page.evaluate("document.querySelector('#view-launch').hidden = false; document.querySelector('#view-run').hidden = true")
                assert overflow(page) <= 1, f"overflow at {w}x{h} in launch"
        page.screenshot(path=str(SHOTS / f"vp-{w}x{h}.png"))
    page.set_viewport_size({"width": 1280, "height": 900})


def test_keyboard_navigation_and_visible_focus(fresh):
    page = fresh
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip"
    for _ in range(3):
        page.keyboard.press("Tab")
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none"
    run_demo(page)
    page.focus("#tab-issues")
    page.keyboard.press("ArrowRight")
    assert page.get_attribute("#tab-patches", "aria-selected") == "true" and page.evaluate("document.activeElement.id") == "tab-patches"
    page.keyboard.press("End")
    assert page.get_attribute("#tab-summary", "aria-selected") == "true"


def test_reduced_motion_is_respected(browser, server):
    page = new_page(browser, server, reduced_motion="reduce")
    assert page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
    assert page.evaluate("parseFloat(getComputedStyle(document.querySelector('.spin')).animationDuration)") < 0.01


@pytest.mark.parametrize("server_env", [{"NEBIUS_API_KEY": "sk-dummy-not-used"}])
def test_live_option_is_selectable_when_a_key_exists_and_pill_says_so(browser, server_env):
    proc, url = start_server(server_env)
    try:
        page = new_page(browser, url)
        assert not page.locator("#scripted").is_disabled() and page.locator("#scripted").is_checked()   # default stays scripted
        page.uncheck("#scripted")
        assert "Live" in page.inner_text("#modePill") and "Nemotron" in page.inner_text("#modePill")
        assert "sent to the model provider" in page.inner_text("#engineNote") and "not configured" in page.inner_text("#engineNote")
        page.close()
    finally:
        proc.terminate(); proc.wait(timeout=10)
