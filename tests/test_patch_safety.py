"""Patch validation and application: nothing a model returns may escape or corrupt the working copy."""
import os
import shutil
from pathlib import Path

import pytest

from app.repo import SiteRepo
from app.validate import (ModelOutputError, PatchError, validate_diagnosis, validate_edits_shape,
                          validate_triage)

DEMO = Path(__file__).resolve().parent.parent / "demo_site"


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(DEMO, src)
    r = SiteRepo(src)
    yield r
    r.cleanup()


def edit(file="index.html", search="<html>", replace='<html lang="en">'):
    return {"file": file, "search": search, "replace": replace}


def test_valid_edit_applies_to_the_copy_only(repo):
    before = repo.source_hash()
    assert repo.apply([edit()]) == ["index.html"]
    assert 'lang="en"' in repo.read("index.html")
    assert repo.source_hash() == before                      # original untouched


@pytest.mark.parametrize("bad", ["../outside.html", "a/../../b.html", "/etc/passwd", "C:\\x.html", "C:/x.html",
                                 "a\\b.html", ".env", "sub/.hidden.js", "package.json", "x.py", "run.sh",
                                 "vendor/lib.js", "node_modules/x.js", "~/x.html", "", "a\x00b.html"])
def test_dangerous_paths_are_rejected(repo, bad):
    with pytest.raises(PatchError):
        repo.apply([edit(file=bad)])


def test_unknown_file_is_rejected(repo):
    with pytest.raises(PatchError, match="unknown file"):
        repo.apply([edit(file="nope.html")])


def test_no_match_is_rejected(repo):
    with pytest.raises(PatchError, match="matched 0 times"):
        repo.apply([edit(search="<html lang=NOPE>")])


def test_ambiguous_match_is_rejected(repo):
    assert repo.read("index.html").count("<a ") > 1
    with pytest.raises(PatchError, match="matched"):
        repo.apply([edit(search="<a ", replace="<a data-x ")])


def test_a_failing_edit_rolls_back_the_earlier_ones(repo):
    original = repo.read("index.html")
    with pytest.raises(PatchError):
        repo.apply([edit(), edit(search="<<<does not exist>>>")])
    assert repo.read("index.html") == original


def test_symlink_escape_is_rejected(repo, tmp_path):
    outside = tmp_path / "outside.html"
    outside.write_text("<html>secret</html>")
    try:
        os.symlink(outside, repo.work / "evil.html")
    except OSError:
        pytest.skip("creating symlinks needs a privilege this OS user lacks (Windows without Developer Mode)")
    with pytest.raises(PatchError, match="escapes"):
        repo.apply([edit(file="evil.html")])
    assert outside.read_text() == "<html>secret</html>"


def test_symlinks_are_not_copied_into_the_working_copy(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "index.html").write_text("<html></html>")
    (tmp_path / "secret.txt").write_text("x")
    try:
        os.symlink(tmp_path / "secret.txt", src / "link.txt")
    except OSError:
        pytest.skip("creating symlinks needs a privilege this OS user lacks (Windows without Developer Mode)")
    r = SiteRepo(src)
    try:
        assert not (r.work / "link.txt").exists()
    finally:
        r.cleanup()


@pytest.mark.parametrize("payload, msg", [
    ({"edits": []}, "no edits"), ({}, "no edits"), ([], "no edits"), ("x", "no edits"),
    ({"edits": [edit()] * 2}, "duplicate"),
    ({"edits": [edit(search="a", replace="a")]}, "changes nothing"),
    ({"edits": [edit(search="")]}, "empty search"),
    ({"edits": [edit(replace="x" * 9000)]}, "too large"),
    ({"edits": [edit(search=str(i)) for i in range(9)]}, "too many"),
    ({"edits": [{"file": "index.html", "search": 1, "replace": "x"}]}, "strings"),
    ({"edits": ["not a dict"]}, "object"),
    ({"edits": [edit(replace='<script src="https://evil.example/x.js"></script>')]}, "external URL"),
])
def test_malformed_or_oversized_patches_are_rejected(payload, msg):
    with pytest.raises(PatchError, match=msg):
        validate_edits_shape(payload)


def test_existing_urls_may_be_kept_but_not_added():
    ok = edit(search='<a href="https://a.example/x">', replace='<a href="https://a.example/x" rel="noopener">')
    assert validate_edits_shape({"edits": [ok]})


def test_triage_validation():
    good = {"priority": "2", "complexity": "simple", "summary": " s ", "user_impact": "u"}
    assert validate_triage(good)["priority"] == 2
    for bad in ({"priority": 9, "complexity": "simple"}, {"priority": 1, "complexity": "weird"},
                {"priority": "high", "complexity": "simple"}, [], "x", {"complexity": "simple"}):
        with pytest.raises(ModelOutputError):
            validate_triage(bad)


def test_diagnosis_validation():
    assert validate_diagnosis({"root_cause": "r", "plan": "p"})["risks"] == ""
    for bad in ({"root_cause": "", "plan": "p"}, {"plan": "p"}, [], {"root_cause": 1, "plan": "p"}):
        with pytest.raises(ModelOutputError):
            validate_diagnosis(bad)
