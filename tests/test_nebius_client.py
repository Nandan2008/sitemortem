"""Nebius Token Factory client behaviour, against a mocked provider."""
import httpx
import pytest

from app.llm import Nemotron, ModelError
from conftest import IDS, KEY, Recorder, err, make_llm, models_response, ok


def test_missing_api_key_is_a_clear_fatal_error():
    llm = Nemotron()
    assert not llm.live
    with pytest.raises(ModelError) as e:
        llm.preflight()
    assert e.value.kind == "config" and e.value.fatal and "NEBIUS_API_KEY" in str(e.value)
    with pytest.raises(ModelError):
        llm.chat("nano", "s", "u", "t")


def test_invalid_credentials_fail_fast_without_leaking_the_key():
    rec = Recorder(err(401, f"bad key {KEY}"))
    llm = make_llm(rec)
    llm.models = {}
    with pytest.raises(ModelError) as e:
        llm.preflight()
    assert e.value.kind == "auth" and e.value.fatal and e.value.status == 401
    assert KEY not in str(e.value)
    assert len(rec.requests) == 1          # never retried


def test_chat_auth_error_is_not_retried_and_is_logged():
    rec = Recorder(err(401))
    llm = make_llm(rec)
    with pytest.raises(ModelError) as e:
        llm.chat("super", "s", "u", "t")
    assert e.value.fatal and len(rec.requests) == 1
    assert llm.log[-1].ok is False and llm.log[-1].error == "auth"


def test_unsupported_pinned_model_is_rejected_by_preflight(monkeypatch):
    monkeypatch.setenv("NEMOTRON_ULTRA_MODEL", "nvidia/does-not-exist")
    llm = make_llm(Recorder(httpx.Response(200, json=models_response())))
    llm.models = {}
    with pytest.raises(ModelError) as e:
        llm.preflight()
    assert e.value.kind == "model_not_found" and "NEMOTRON_ULTRA_MODEL" in str(e.value)


def test_unknown_model_404_on_chat_is_fatal():
    llm = make_llm(Recorder(err(404, "no such model", code="model_not_found")))
    with pytest.raises(ModelError) as e:
        llm.chat("ultra", "s", "u", "t")
    assert e.value.kind == "model_not_found" and e.value.fatal and IDS["ultra"] in str(e.value)


def test_tiers_resolve_to_the_right_real_model_ids():
    llm = make_llm(Recorder(httpx.Response(200, json=models_response())))
    llm.models = {}
    assert llm.preflight() == IDS            # 3.5-Super-VL / Lightning are NOT picked


def test_missing_tier_is_an_error_not_a_silent_substitution():
    only_nano = [IDS["nano"], "meta-llama/Llama-3.3-70B-Instruct"]
    llm = make_llm(Recorder(httpx.Response(200, json=models_response(only_nano))))
    llm.models = {}
    with pytest.raises(ModelError) as e:
        llm.preflight()
    assert "super" in str(e.value) and "ultra" in str(e.value)


def test_rate_limit_is_retried_then_succeeds():
    rec = Recorder(err(429, headers={"retry-after": "0"}), err(429), ok("done"))
    llm = make_llm(rec)
    assert llm.chat("nano", "s", "u", "t") == "done"
    assert len(rec.requests) == 3 and llm.log[-1].retries == 2 and llm.log[-1].ok


def test_rate_limit_exhausted_raises_after_bounded_retries():
    rec = Recorder(err(429))
    llm = make_llm(rec)
    with pytest.raises(ModelError) as e:
        llm.chat("nano", "s", "u", "t")
    assert e.value.kind == "rate_limit" and not e.value.fatal
    assert len(rec.requests) == 3            # 1 try + MAX_RETRIES
    assert llm.log[-1].ok is False


def test_timeout_is_retried_then_reported():
    rec = Recorder(httpx.ReadTimeout("slow"))
    llm = make_llm(rec)
    with pytest.raises(ModelError) as e:
        llm.chat("nano", "s", "u", "t")
    assert e.value.kind == "timeout" and len(rec.requests) == 3


def test_server_error_then_success():
    llm = make_llm(Recorder(err(503), ok("fine")))
    assert llm.chat("nano", "s", "u", "t") == "fine"


def test_credits_exhausted_is_fatal():
    llm = make_llm(Recorder(err(402, "no budget")))
    with pytest.raises(ModelError) as e:
        llm.chat("nano", "s", "u", "t")
    assert e.value.kind == "credits" and e.value.fatal


def test_malformed_responses_are_rejected():
    for resp, kind in [(httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1,
                                                  "model": "m", "choices": []}), "bad_response"),
                       (ok(""), "bad_response"),
                       (ok("<think>unfinished reasoning"), "bad_response"),
                       (ok("partial", finish="length"), "truncated")]:
        llm = make_llm(Recorder(resp))
        with pytest.raises(ModelError) as e:
            llm.chat("nano", "s", "u", "t")
        assert e.value.kind == kind


def test_invalid_json_from_chat_json_is_rejected():
    llm = make_llm(Recorder(ok("this is not json at all")))
    with pytest.raises(ModelError) as e:
        llm.chat_json("nano", "s", "u", "t")
    assert e.value.kind == "bad_response"
    assert llm.log[-1].ok is False


def test_success_records_usage_only_when_provider_sends_it():
    llm = make_llm(Recorder(ok("a"), ok("b", usage=False)))
    llm.chat("nano", "s", "u", "one")
    llm.chat("nano", "s", "u", "two")
    assert llm.log[0].tokens == 15 and llm.log[1].tokens is None


def test_think_blocks_are_stripped_and_json_parsed():
    llm = make_llm(Recorder(ok('<think>hmm</think>```json\n{"a": 1}\n```')))
    assert llm.chat_json("nano", "s", "u", "t") == {"a": 1}


def test_json_mode_rejection_falls_back_once_and_is_reported():
    rec = Recorder(err(400, "Unsupported parameter: response_format"), ok('{"ok": true}'))
    llm = make_llm(rec)
    assert llm.chat_json("nano", "s", "u", "t") == {"ok": True}
    first, second = rec.bodies()
    assert "response_format" in first and "response_format" not in second
    assert llm.notes and "response_format" in llm.notes[0]


def test_reasoning_switch_is_sent_in_the_system_prompt():
    rec = Recorder(ok("x"))
    llm = make_llm(rec)
    llm.chat("ultra", "SYS", "u", "t", reasoning=True)
    llm.chat("nano", "SYS", "u", "t", reasoning=False)
    on, off = rec.bodies()
    assert on["messages"][0]["content"].startswith("detailed thinking on")
    assert off["messages"][0]["content"].startswith("detailed thinking off")
    assert on["model"] == IDS["ultra"] and off["model"] == IDS["nano"]


def test_effort_mode_uses_reasoning_effort_instead(monkeypatch):
    monkeypatch.setenv("NEMOTRON_REASONING_MODE", "effort")
    rec = Recorder(ok("x"))
    make_llm(rec).chat("ultra", "SYS", "u", "t", reasoning=True)
    body = rec.bodies()[0]
    assert body.get("reasoning_effort") == "high" and "detailed thinking" not in body["messages"][0]["content"]
