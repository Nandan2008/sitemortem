"""Pipeline tests. They use a real headless Chromium + axe-core; only the model is stubbed.

Run:  python -m pytest -q
"""
from pathlib import Path

from app import pipeline
from app.agents import ScriptedAgent
from app.llm import parse_json, strip_think

DEMO = Path(__file__).resolve().parent.parent / "demo_site"


class LiveLike(ScriptedAgent):
    live = True
    models = {"nano": "n", "super": "s", "ultra": "u"}

    def diagnose(self, *a):
        return {"root_cause": "rc", "plan": "p", "risks": "r"}


def run(agent):
    ev = list(pipeline.run(DEMO, agent))
    return ev, next(e for e in ev if e["type"] == "report")


def test_demo_site_goes_to_zero():
    _, rep = run(ScriptedAgent())
    assert rep["before"]["total"] > 20 and rep["after"]["total"] == 0


def test_source_folder_is_never_modified():
    before = (DEMO / "index.html").read_text()
    run(ScriptedAgent())
    assert (DEMO / "index.html").read_text() == before


def test_unmatched_patch_is_retried_with_ultra():
    class Flaky(LiveLike):
        def patch(self, batch, repo, rels, diagnosis, guidance, feedback, attempt):
            if attempt == 1 and batch.rule == "html-has-lang":
                return {"edits": [{"file": "index.html", "search": "<html lang=NOPE>", "replace": "x"}]}
            out = super().patch(batch, repo, rels, diagnosis, guidance, feedback, attempt)
            out["tier"] = "super" if attempt == 1 else "ultra"
            return out

    ev, rep = run(Flaky())
    assert rep["after"]["total"] == 0
    assert any(e["type"] == "fix" and e["attempt"] == 2 and e["tier"] == "ultra" for e in ev)


def test_patch_that_introduces_a_new_issue_is_reverted():
    class Sneaky(LiveLike):
        def patch(self, batch, repo, rels, diagnosis, guidance, feedback, attempt):
            out = super().patch(batch, repo, rels, diagnosis, guidance, feedback, attempt)
            if attempt == 1 and batch.rule == "html-has-lang":
                out["edits"].append({"file": "index.html", "search": "<main>",
                                     "replace": '<main><img src="img/kenya.svg" width="10" height="10">'})
            return out

    ev, rep = run(Sneaky())
    assert any(e["type"] == "batch" and "introduced" in e.get("detail", "") for e in ev)
    assert 'width="10" height="10"' not in rep["diff"]
    assert rep["after"]["total"] == 0


def test_llm_json_helpers():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('noise {"a": [1, 2]} trailing') == {"a": [1, 2]}
    assert strip_think("<think>hidden</think>answer") == "answer"
