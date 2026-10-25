"""URL inputs (GitHub repo, live site), the cookie-based history, and the audit-only run."""
import io
import json
import socket
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import history, remote
from app.safety import UserError
from app.safezip import extract_zip
from app.siteserver import SiteServer


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEMEDIC_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


# ---------------------------------------------------------------- GitHub URLs
@pytest.mark.parametrize("url,expect", [
    ("https://github.com/owner/repo", ("owner", "repo", "")),
    ("https://github.com/owner/repo.git", ("owner", "repo", "")),
    ("https://github.com/owner/repo/tree/main", ("owner", "repo", "main")),
    ("https://www.github.com/o/r/", ("o", "r", "")),
])
def test_github_url_parsing(url, expect):
    assert remote.parse_github(url) == expect


@pytest.mark.parametrize("url", ["", "http://github.com/o/r", "https://gitlab.com/o/r", "https://github.com/o",
                                 "https://evil.com/github.com/o/r", "https://github.com@evil.com/o/r",
                                 "https://github.com/../x", "https://github.com/o/r/tree/../../x",
                                 "https://github.com:8080/o/r", "https://github.com/o r/x"])
def test_bad_github_urls_are_rejected(url):
    with pytest.raises(UserError):
        remote.parse_github(url)


def test_repo_zip_extracts_leniently_and_finds_the_site_folder():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("repo-main/README.md", "# hi")
        z.writestr("repo-main/server.py", "print(1)")          # not a web file: skipped, not rejected
        z.writestr("repo-main/node_modules/x/index.js", "1")   # vendored: skipped
        z.writestr("repo-main/.github/workflows/ci.yml", "x")
        z.writestr("repo-main/public/index.html", "<html></html>")
        z.writestr("repo-main/public/app.js", "1")
    ex = extract_zip(buf.getvalue(), lenient=True)
    try:
        assert (ex.root / "index.html").is_file() and (ex.root / "app.js").is_file()
        assert ex.skipped >= 3
    finally:
        import shutil; shutil.rmtree(ex.tmp, ignore_errors=True)


def test_strict_upload_still_rejects_unknown_types():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.html", "<html></html>"); z.writestr("run.exe", "x")
    with pytest.raises(UserError):
        extract_zip(buf.getvalue())


def test_repo_download_is_size_capped_and_404_is_friendly(monkeypatch):
    import httpx

    def fake_stream(method, url, **kw):
        assert url.startswith("https://codeload.github.com/") and kw["follow_redirects"] is False
        class R:
            status_code = 404
            def __enter__(s): return s
            def __exit__(s, *a): return False
        return R()
    monkeypatch.setattr(httpx, "stream", fake_stream)
    with pytest.raises(UserError, match="not found"):
        remote.fetch_repo_zip("https://github.com/o/r")


# ---------------------------------------------------------------- site URLs (SSRF)
def fake_dns(monkeypatch, table):
    def gai(host, *a, **k):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", (ip, 0)) for ip in table[host]]
    monkeypatch.setattr(socket, "getaddrinfo", gai)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "172.16.0.9", "0.0.0.0", "::1", "fc00::1", "100.64.0.1"])
def test_private_addresses_are_refused(monkeypatch, ip):
    fake_dns(monkeypatch, {"evil.test": [ip]})
    with pytest.raises(UserError, match="private or internal"):
        remote.validate_site_url("https://evil.test")


def test_one_private_answer_among_public_ones_is_refused(monkeypatch):
    fake_dns(monkeypatch, {"mixed.test": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(UserError):
        remote.validate_site_url("https://mixed.test")


@pytest.mark.parametrize("url", ["ftp://x.test", "https://x.test:8443", "http://127.0.0.1", "https://user:pw@x.test", "javascript:alert(1)", ""])
def test_unsafe_site_urls_are_refused(monkeypatch, url):
    fake_dns(monkeypatch, {"x.test": ["93.184.216.34"]})
    with pytest.raises(UserError):
        remote.validate_site_url(url)


def test_public_site_is_pinned_and_www_variant_allowed(monkeypatch):
    fake_dns(monkeypatch, {"example.test": ["93.184.216.34"], "www.example.test": ["93.184.216.35"]})
    url, pins = remote.validate_site_url("example.test")
    assert url == "https://example.test" and pins == {"example.test": "93.184.216.34", "www.example.test": "93.184.216.35"}


# ---------------------------------------------------------------- history
def test_history_store_roundtrip_cap_and_bad_ids():
    uid = history.new_id()
    for n in range(60):
        history.add(uid, {"run": f"r{n}", "before": 1, "after": 0})
    items = history.load(uid)
    assert len(items) == history.MAX_ENTRIES and items[0]["run"] == "r59"
    history.clear(uid)
    assert history.load(uid) == []
    for bad in ("../../etc/passwd", "", "x" * 32, None):
        assert history.load(bad) == []
        history.add(bad, {"run": "x"})           # silently ignored, never touches the disk
    assert not list(history.data_dir().glob("*..*"))


def test_history_cookie_is_httponly_and_isolates_users():
    a, b = TestClient(main.app), TestClient(main.app)
    r = a.get("/api/history")
    assert r.json() == {"history": []}
    sc = r.headers["set-cookie"].lower()
    assert "sm_uid=" in sc and "httponly" in sc and "samesite=lax" in sc
    uid = a.cookies["sm_uid"]
    history.add(uid, {"run": "abc", "before": 3, "after": 1})
    assert a.get("/api/history").json()["history"][0]["run"] == "abc"
    assert b.get("/api/history").json()["history"] == []          # a different browser sees nothing
    assert a.delete("/api/history").json() == {"ok": True}
    assert a.get("/api/history").json()["history"] == []


def test_forged_cookie_cannot_read_or_write_other_files():
    c = TestClient(main.app)
    c.cookies.set("sm_uid", "../../etc/passwd")
    r = c.get("/api/history")
    assert r.json() == {"history": []} and "sm_uid=" in r.headers["set-cookie"]


# ---------------------------------------------------------------- end to end
def events(resp):
    return [json.loads(l[6:]) for l in resp.iter_lines() if l.startswith("data: ")]


@pytest.mark.slow
def test_live_site_audit_is_report_only_makes_no_model_calls_and_is_saved(monkeypatch):
    site = Path(main.DEMO)
    with SiteServer(site) as srv:
        port = srv.port
        monkeypatch.setattr(main.remote, "validate_site_url",
                            lambda u: (f"http://demo.test:{port}/", {"demo.test": "127.0.0.1"}))
        c = TestClient(main.app)
        with c.stream("POST", "/api/run", data={"mode": "url", "site_url": "anything.test"}) as r:
            assert r.status_code == 200
            evs = events(r)
    types = [e["type"] for e in evs]
    rep = next(e for e in evs if e["type"] == "report")
    assert "error" not in types and types[-1] == "done"
    assert rep["audit_only"] and rep["fixed"] == 0 and rep["diff"] == "" and rep["before"]["total"] > 0
    assert rep["suggestions"] and all("advice" in s for s in rep["suggestions"])
    assert next(e for e in evs if e["type"] == "mode")["live"] is False
    saved = c.get("/api/history").json()["history"]
    assert saved and saved[0]["mode"] == "url" and saved[0]["audit_only"] is True


@pytest.mark.slow
def test_repo_mode_runs_the_normal_pipeline_on_a_downloaded_zip(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in Path(main.DEMO).rglob("*"):
            if p.is_file():
                z.writestr("shop-main/" + str(p.relative_to(main.DEMO)).replace("\\", "/"), p.read_bytes())
        z.writestr("shop-main/README.md", "# shop"); z.writestr("shop-main/build.py", "print(1)")
    monkeypatch.setattr(main.remote, "fetch_repo_zip", lambda u: (buf.getvalue(), "o/shop"))
    c = TestClient(main.app)
    with c.stream("POST", "/api/run", data={"mode": "repo", "repo_url": "https://github.com/o/shop", "scripted": "true"}) as r:
        assert r.status_code == 200
        evs = events(r)
    rep = next(e for e in evs if e["type"] == "report")
    assert not rep.get("audit_only") and rep["before"]["total"] > rep["after"]["total"]
    assert c.get("/api/history").json()["history"][0]["target"] == "o/shop"


def test_bad_inputs_are_400_not_500(monkeypatch):
    fake_dns(monkeypatch, {})
    c = TestClient(main.app)
    assert c.post("/api/run", data={"mode": "repo", "repo_url": "https://gitlab.com/a/b"}).status_code == 400
    assert c.post("/api/run", data={"mode": "url", "site_url": "http://127.0.0.1"}).status_code == 400
    assert c.post("/api/run", data={"mode": "url", "site_url": "nonexistent.test"}).status_code == 400
    assert c.post("/api/run", data={"mode": "nope"}).status_code == 400
