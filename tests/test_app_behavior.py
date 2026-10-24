"""Application behaviour: modes, streaming, honest failures, the live path (with a mocked provider), rollback."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import pipeline, tavily
from app.agents import NemotronAgent, ScriptedAgent
from app.audit import Auditor
from app.llm import ModelError
from app.siteserver import SiteServer
from conftest import IDS, completion, err, make_llm, models_response

DEMO = Path(__file__).resolve().parent.parent / "demo_site"


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def sse(resp):
    return [json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


# ------------------------------------------------------------------------------------ modes & API
def test_health_reports_availability_without_leaking_keys(client, monkeypatch):
    h = client.get("/api/health").json()
    assert h == {"ok": True, "live_available": False, "default_mode": "demo", "tavily": False, "busy": False}
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-abc-should-not-appear")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-should-not-appear")
    r = client.get("/api/health")
    assert r.json()["live_available"] is True and r.json()["tavily"] is True
    assert "should-not-appear" not in r.text


def test_live_mode_refuses_to_run_without_credentials(client):
    r = client.post("/api/run", data={"mode": "demo", "engine": "live"})
    assert r.status_code == 400 and "NEBIUS_API_KEY" in r.json()["detail"]
    assert "scripted" in r.json()["detail"].lower()          # tells the user it will not fall back


def test_security_headers_are_present(client):
    h = client.get("/").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert "default-src 'self'" in h["content-security-policy"]


def test_live_failure_is_reported_and_never_falls_back_to_scripted(client, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-test-123456789")

    def boom():
        raise ModelError("Nebius rejected the API key (401). Check NEBIUS_API_KEY in .env.", "auth", True, 401)

    monkeypatch.setattr("app.main.NemotronAgent", boom)
    events = sse(client.post("/api/run", data={"mode": "demo", "engine": "live"}))
    assert [e["type"] for e in events] == ["run", "error"]     # no mode/audit/fix events: nothing was scripted
    assert events[0]["engine"] == "live"
    assert "401" in events[1]["message"] and events[1]["kind"] == "auth" and events[1]["ref"]


def test_unexpected_errors_are_not_leaked_to_the_browser(client, monkeypatch):
    def broken(source, agent, **kw):
        yield {"type": "stage", "key": "audit", "label": "x", "engine": "axe"}
        raise RuntimeError("secret path /home/user/.ssh/id_rsa and key sk-test-123456789")

    monkeypatch.setattr("app.main.pipeline.run", broken)
    events = sse(client.post("/api/run", data={"mode": "demo", "engine": "demo"}))
    assert events[-1]["type"] == "error" and events[-1]["kind"] == "internal"
    assert "secret path" not in events[-1]["message"] and "Reference" in events[-1]["message"]
    # the semaphore was released: a second run is accepted (and fails the same safe way)
    assert client.post("/api/run", data={"mode": "demo", "engine": "demo"}).status_code == 200


def test_live_run_budget_is_enforced(client, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-test-123456789")
    monkeypatch.setattr("app.main.LIVE_RUNS_PER_HOUR", 0)
    assert client.post("/api/run", data={"mode": "demo", "engine": "live"}).status_code == 429


# ------------------------------------------------------------------------------------ demo mode
@pytest.mark.slow
def test_demo_mode_works_without_keys_and_counters_match_the_audit(client):
    events = sse(client.post("/api/run", data={"mode": "demo", "engine": "demo"}))
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["run", "mode"] and events[1]["live"] is False and "error" not in kinds
    audit = next(e for e in events if e["type"] == "audit")
    report = next(e for e in events if e["type"] == "report")
    assert report["before"] == audit["counts"] and report["before"]["total"] > 20
    assert report["after"]["total"] == 0 and report["fixed"] == report["before"]["total"]
    last_status = [e for e in events if e["type"] == "issue_status"][-1]
    assert last_status["counts"] == report["after"]           # live counters == final audit
    ev = report["evidence"]
    assert ev["live"] is False and ev["source_unchanged"] is True and ev["blocked_external"] == []
    assert not [e for e in events if e["type"] == "usage"]    # no fabricated model usage in demo mode
    # categories never double-count
    for c in (report["before"], report["after"]):
        assert c["a11y"] + c["js"] + c["network"] == c["total"] and c["js"] + c["network"] == c["runtime"]
    assert report["before"]["network"] >= 1 and report["before"]["js"] >= 1
    # every event is stamped; seq is strictly increasing from 0; the first event identifies the run
    assert events[0]["type"] == "run" and [e["seq"] for e in events] == list(range(len(events)))
    assert len({e["run"] for e in events}) == 1 and all(e["ts"] > 1e9 for e in events)
    # evidence: one result per attempt, verdicts only from the backend's verification
    results = [e for e in events if e["type"] == "evidence" and e["stage"] == "result"]
    assert results and all(e["verdict"] == "verified" and e["targets_resolved"] == e["targets_total"] and not e["new_issues"] for e in results)
    assert all(e["diff"] for e in results) and len([e for e in events if e["type"] == "evidence" and e["stage"] == "proposed"]) == len(results)
    assert all({"category", "html", "help_url"} <= set(i) for i in next(e for e in events if e["type"] == "issues")["issues"])
    assert all(a["model"] == "demo (scripted)" for b in report["batches"] for a in b["attempts"])


# ------------------------------------------------------------------------------------ live path, mocked provider
HAS_LANG_EDIT = {"edits": [{"file": "index.html", "search": "<html>", "replace": '<html lang="en">'},
                           {"file": "about.html", "search": "<html>", "replace": '<html lang="en">'}],
                 "explanation": "Declare the page language."}


def provider(pr_text="# fix: mocked PR"):
    """A fake Nebius: valid triage/diagnosis, a correct patch for html-has-lang, garbage for everything else."""
    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(200, json=models_response())
        body = json.loads(request.content)
        sys_p, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "triage ONE group" in sys_p:
            out = {"priority": 2, "complexity": "simple", "summary": "s", "user_impact": "u"}
        elif "root-cause analysis" in sys_p:
            out = {"root_cause": "rc", "plan": "p", "risks": ""}
        elif "careful front-end engineer" in sys_p:
            if "RULE: html-has-lang" in user:
                out = HAS_LANG_EDIT
            else:
                return httpx.Response(200, json=completion("I cannot produce JSON, sorry", model=body["model"]))
        else:
            return httpx.Response(200, json=completion(pr_text, model=body["model"]))
        return httpx.Response(200, json=completion(json.dumps(out), model=body["model"]))
    return handler


@pytest.mark.slow
def test_live_path_with_mocked_provider_is_honest_about_what_worked():
    llm = make_llm(provider())
    llm.models = {}                                            # force real discovery against the mock account
    agent = NemotronAgent(llm)
    events = list(pipeline.run(DEMO, agent))
    report = next(e for e in events if e["type"] == "report")

    assert events[0]["live"] is True and events[0]["models"] == IDS
    fixes = [e for e in events if e["type"] == "fix"]
    assert fixes and all(f["model"] == IDS["super"] and f["tier"] == "super" for f in fixes)
    # only the group the "model" could patch is fixed; the rest are rejected and reported, not hidden
    assert 0 < report["fixed"] < report["total"] and report["after"]["total"] > 0
    assert report["rejected"] and all("valid JSON" in x["reason"] for x in report["rejected"])
    statuses = {b["title"].split(" ·")[0]: b["status"] for b in report["batches"]}
    assert statuses["html-has-lang"] == "fixed" and "needs-review" in statuses.values()
    # retry policy: second attempts go to Ultra, never more than two attempts
    attempts = [a for b in report["batches"] for a in b["attempts"]]
    assert max(a["n"] for a in attempts) == 2
    assert {a["tier"] for a in attempts if a["n"] == 2} == {"ultra"}
    # PR text comes from the (mock) model; usage is what the provider reported
    assert report["pr"] == "# fix: mocked PR"
    usage = next(e for e in events if e["type"] == "usage")["calls"]
    assert usage and all(c["tokens"] == 15 for c in usage if c["ok"]) and {c["tier"] for c in usage} == {"nano", "super", "ultra"}
    assert report["evidence"]["live"] is True and report["evidence"]["source_unchanged"] is True
    # Tavily key is absent: every lookup is reported as skipped, not faked
    intel = [e for e in events if e["type"] == "intel"]
    assert intel and all(e["status"] == "skipped" and not e["sources"] for e in intel)
    assert report["evidence"]["tavily"] == {"skipped": len(intel)}
    assert 'lang="en"' in report["diff"] and "Scripted" not in json.dumps(events)


@pytest.mark.slow
def test_tavily_failure_is_reported_and_the_run_continues(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setattr(tavily, "search", lambda q: tavily.TavilyResult("error", reason="Tavily plan/usage limit reached (432)"))
    agent = NemotronAgent(make_llm(provider()))
    events = list(pipeline.run(DEMO, agent))
    intel = [e for e in events if e["type"] == "intel"]
    assert intel and all(e["status"] == "error" and "432" in e["reason"] for e in intel)
    assert any(e["type"] == "fix" for e in events)             # repair continued without guidance


@pytest.mark.slow
def test_fatal_model_error_aborts_the_run_instead_of_grinding_on():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=models_response())
        return err(402, "no credits")

    agent = NemotronAgent(make_llm(handler))
    gen = pipeline.run(DEMO, agent)
    with pytest.raises(ModelError) as e:
        list(gen)
    assert e.value.kind == "credits"


@pytest.mark.slow
def test_pr_failure_uses_a_labelled_template_not_a_fake():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=models_response())
        sys_p = json.loads(request.content)["messages"][0]["content"]
        if "pull-request description" in sys_p:
            return err(503, "down")
        return provider()(request)

    events = list(pipeline.run(DEMO, NemotronAgent(make_llm(handler))))
    report = next(e for e in events if e["type"] == "report")
    assert "without a model" in report["pr"]
    assert any("PR description could not be written" in w for w in report["evidence"]["warnings"])


# ------------------------------------------------------------------------------------ rollback / retry
class AlwaysBad(ScriptedAgent):
    live = True
    models = {"nano": "n", "super": "s", "ultra": "u"}

    def __init__(self, edits):
        self.bad_edits = edits

    def diagnose(self, *a):
        return {"root_cause": "rc", "plan": "p", "risks": ""}

    def patch(self, batch, repo, rels, diagnosis, guidance, feedback, attempt):
        if batch.rule == "html-has-lang":
            return {"edits": self.bad_edits, "explanation": "", "model": "m", "tier": "super" if attempt == 1 else "ultra"}
        return super().patch(batch, repo, rels, diagnosis, guidance, feedback, attempt)


def run_events(agent):
    events = list(pipeline.run(DEMO, agent))
    return events, next(e for e in events if e["type"] == "report")


@pytest.mark.slow
def test_failed_retry_is_rejected_and_rolled_back():
    bad = [{"file": "index.html", "search": "<html lang=NOPE>", "replace": "x"}]
    events, rep = run_events(AlwaysBad(bad))
    b = next(x for x in rep["batches"] if x["title"].startswith("html-has-lang"))
    assert b["status"] == "needs-review" and [a["outcome"] for a in b["attempts"]] == ["no_usable_patch"] * 2
    assert 'lang="en"' not in rep["diff"]                      # nothing from the failed group survived
    assert any(r["batch"] and "matched 0" in r["reason"] for r in rep["rejected"])
    assert rep["remaining"] and {x["rule"] for x in rep["remaining"]} == {"html-has-lang"}   # only the failed group is left
    assert rep["after"]["total"] == len(rep["remaining"]) and rep["evidence"]["source_unchanged"]


@pytest.mark.slow
@pytest.mark.parametrize("target", ["../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini", ".env", "x.py"])
def test_patches_aimed_outside_the_workspace_are_rejected(target):
    bad = [{"file": target, "search": "a", "replace": "b"}]
    events, rep = run_events(AlwaysBad(bad))
    assert any(r["reason"] for r in rep["rejected"]) and rep["evidence"]["source_unchanged"]
    assert target not in rep["diff"]


# ------------------------------------------------------------------------------------ deadline & egress
@pytest.mark.slow
def test_run_deadline_stops_work_and_says_so():
    events = list(pipeline.run(DEMO, ScriptedAgent(), deadline_s=0.001))
    rep = next(e for e in events if e["type"] == "report")
    assert rep["after"] == rep["before"] and rep["diff"] == ""
    assert all(b["status"] == "skipped" for b in rep["batches"])
    assert any("time limit" in w for w in rep["evidence"]["warnings"])


@pytest.mark.slow
def test_uploaded_sites_cannot_reach_private_or_external_hosts(tmp_path):
    site = tmp_path / "s"
    site.mkdir()
    (site / "index.html").write_text(
        '<!doctype html><html lang="en"><head><title>t</title></head><body><main><h1>x</h1>'
        '<img src="http://203.0.113.9/pixel.png" alt="x">'
        "<script>fetch('http://169.254.169.254/latest/meta-data/').catch(()=>{});"
        "fetch('http://localhost:9/x').catch(()=>{});</script></main></body></html>")
    with SiteServer(site) as srv, Auditor() as aud:
        issues, _ = aud.audit(srv.url)
    assert {"203.0.113.9", "169.254.169.254", "localhost"} <= aud.blocked
    assert not [i for i in issues if i.rule in ("request-failed", "http-error")]   # blocked != a site bug


# ------------------------------------------------------------------------------------ run store, resume, cancel
def test_run_stream_resumes_after_the_last_seen_event():
    from app.main import Run
    r = Run()
    for n in range(5):
        r.emit({"type": "x", "n": n})
    r.emit(None)
    all_ev = [json.loads(l[6:]) for l in "".join(r.stream(0)).splitlines() if l.startswith("data: ")]
    assert [e["seq"] for e in all_ev] == [0, 1, 2, 3, 4] and all(e["run"] == r.id for e in all_ev)
    rest = [json.loads(l[6:]) for l in "".join(r.stream(3)).splitlines() if l.startswith("data: ")]
    assert [e["seq"] for e in rest] == [3, 4]                       # resume gives exactly the missing events, no duplicates


def test_unknown_runs_are_404(client):
    assert client.get("/api/runs/nope/events").status_code == 404
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_resume_and_cancel_endpoints_work_on_a_known_run(client):
    from app import main
    r = main.Run()
    main._runs[r.id] = r
    r.emit({"type": "x"}); r.emit({"type": "y"}); r.emit(None)
    resp = client.get(f"/api/runs/{r.id}/events?after=0")
    assert [json.loads(l[6:])["type"] for l in resp.text.splitlines() if l.startswith("data: ")] == ["y"]
    assert client.post(f"/api/runs/{r.id}/cancel").json()["ok"] and r.cancel.is_set()


@pytest.mark.slow
def test_cancel_stops_before_more_work_and_says_so():
    events = list(pipeline.run(DEMO, ScriptedAgent(), should_stop=lambda: True))
    rep = next(e for e in events if e["type"] == "report")
    assert rep["after"] == rep["before"] and rep["diff"] == "" and all(b["status"] == "skipped" for b in rep["batches"])
    assert any("stopped by the user" in w for w in rep["evidence"]["warnings"])


def test_browser_launch_failure_is_explained_without_a_stack_trace():
    from app.main import safe_message
    msg, kind, ref = safe_message(Exception("BrowserType.launch: Executable doesn't exist at /x/chrome\nRun playwright install"))
    assert kind == "browser" and "playwright install chromium" in msg and "/x/chrome" not in msg
