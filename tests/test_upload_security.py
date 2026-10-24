"""ZIP upload handling: safe extraction, limits, and cleanup."""
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import safezip
from app.safety import UserError
from app.safezip import extract_zip


@pytest.fixture(autouse=True)
def isolated_tmp(tmp_path, monkeypatch):
    t = tmp_path / "tmp"
    t.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(t))
    return t


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for item in entries:
            if isinstance(item, zipfile.ZipInfo):
                z.writestr(item, "x")
            else:
                name, data = item if isinstance(item, tuple) else (item, "<html></html>")
                z.writestr(name, data)
    return buf.getvalue()


def leftovers(t):
    return [p for p in os.listdir(t)]


def test_valid_zip_extracts_and_cleans_up(isolated_tmp):
    ex = extract_zip(make_zip(["index.html", ("css/a.css", "a{}"), ("img/x.svg", "<svg/>")]))
    assert (ex.root / "index.html").is_file() and (ex.root / "css" / "a.css").is_file()
    import shutil
    shutil.rmtree(ex.tmp)
    assert leftovers(isolated_tmp) == []


def test_single_top_level_folder_becomes_the_root():
    ex = extract_zip(make_zip(["mysite/index.html"]))
    assert ex.root.name == "mysite"


def test_dotfiles_and_macos_junk_are_skipped():
    ex = extract_zip(make_zip(["index.html", (".env", "KEY=1"), (".git/config", "x"), ("__MACOSX/._i", "x")]))
    assert not (ex.root / ".env").exists() and not (ex.root / ".git").exists()


@pytest.mark.parametrize("name", ["../evil.html", "a/../../evil.html", "/abs/evil.html", "C:/evil.html",
                                  "a\\b.html", "nul\x00.html"])
def test_traversal_and_absolute_paths_are_rejected(name, isolated_tmp):
    if name == "a\\b.html" and os.sep == "\\":
        pytest.skip("on Windows, zipfile itself turns a backslash into a path separator, so 'a\\b.html' is an ordinary nested path")
    with pytest.raises(UserError):
        extract_zip(make_zip(["index.html", name]))
    assert leftovers(isolated_tmp) == []                 # failure leaves nothing behind


def test_symlink_entries_are_rejected(isolated_tmp):
    info = zipfile.ZipInfo("link.html")
    info.external_attr = (0o120777 << 16)
    with pytest.raises(UserError, match="symbolic link"):
        extract_zip(make_zip(["index.html", info]))
    assert leftovers(isolated_tmp) == []


def test_encrypted_entries_are_rejected():
    data = bytearray(make_zip(["index.html", "secret.html"]))
    i = 0
    while (i := data.find(b"PK\x01\x02", i)) != -1:     # set the "encrypted" flag in each central-directory entry
        data[i + 8] |= 0x1
        i += 4
    with pytest.raises(UserError, match="Encrypted"):
        extract_zip(bytes(data))


@pytest.mark.parametrize("name", ["run.exe", "script.py", "payload.sh", "x.php", "lib.dll", "noextension"])
def test_unexpected_file_types_are_rejected(name):
    with pytest.raises(UserError, match="not allowed"):
        extract_zip(make_zip(["index.html", (name, "x")]))


def test_invalid_archive_is_rejected(isolated_tmp):
    with pytest.raises(UserError, match="not a valid zip"):
        extract_zip(b"this is not a zip")
    assert leftovers(isolated_tmp) == []


def test_too_many_entries_are_rejected():
    with pytest.raises(UserError, match="too many entries"):
        extract_zip(make_zip([f"p{i}.html" for i in range(safezip.MAX_ENTRIES + 1)]))


def test_size_limits_are_enforced_on_real_bytes(monkeypatch, isolated_tmp):
    monkeypatch.setattr(safezip, "MAX_FILE", 1000)
    with pytest.raises(UserError, match="larger than"):
        extract_zip(make_zip(["index.html", ("big.js", "a" * 5000)]))
    monkeypatch.setattr(safezip, "MAX_FILE", 10_000)
    monkeypatch.setattr(safezip, "MAX_TOTAL", 3000)
    with pytest.raises(UserError, match="expands to more"):
        extract_zip(make_zip(["index.html", ("a.js", "a" * 2000), ("b.js", "b" * 2000)]))
    assert leftovers(isolated_tmp) == []


def test_zip_without_html_is_rejected():
    with pytest.raises(UserError, match="No .html"):
        extract_zip(make_zip([("a.css", "a{}")]))


# ---- HTTP level ---------------------------------------------------------------------------------
@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def post(client, data, **form):
    return client.post("/api/run", data={"mode": "upload", "engine": "demo", **form},
                       files={"site": ("s.zip", data, "application/zip")})


def test_http_rejects_traversal_zip_and_cleans_up(client, isolated_tmp):
    r = post(client, make_zip(["index.html", "../evil.html"]))
    assert r.status_code == 400 and "leave the upload folder" in r.json()["detail"]
    assert leftovers(isolated_tmp) == []


def test_http_rejects_oversized_upload(client):
    r = post(client, b"0" * (8 * 1024 * 1024 + 10))
    assert r.status_code in (400, 413)


def test_http_rejects_bad_parameters(client):
    assert client.post("/api/run", data={"mode": "nope"}).status_code == 400
    assert client.post("/api/run", data={"mode": "demo", "engine": "nope"}).status_code == 400
    assert client.post("/api/run", data={"mode": "upload", "engine": "demo"}).status_code == 400


def test_common_project_files_are_skipped_not_rejected():
    ex = extract_zip(make_zip(["index.html", ("README.md", "x"), ("src/app.scss", "x"), ("fonts/a.eot", "x"),
                               ("LICENSE", "x"), ("img/Thumbs.db", "x")]))
    try:
        assert ex.skipped == 4 and (ex.root / "index.html").exists() and (ex.root / "fonts/a.eot").exists()
        assert not (ex.root / "README.md").exists() and not (ex.root / "src/app.scss").exists()
    finally:
        import shutil
        shutil.rmtree(ex.tmp, ignore_errors=True)
