"""Shared fixtures. Tests NEVER call the real Nebius or Tavily APIs: keys are cleared for every test and
model traffic goes through httpx.MockTransport."""
import json

import httpx
import openai
import pytest

import app.main  # noqa: F401  imported first so load_dotenv() runs at collection time, before clean_env clears keys
from app.llm import Nemotron

KEY = "sk-test-SECRET-123456789"
IDS = {"nano": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
       "super": "nvidia/nemotron-3-super-120b-a12b",
       "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b"}
ACCOUNT_MODELS = list(IDS.values()) + ["nvidia/Nemotron-3.5-Super-VL", "nvidia/Nemotron-3_5-Lightning",
                                       "meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-R1"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "NEMOTRON_NANO_MODEL", "NEMOTRON_SUPER_MODEL",
              "NEMOTRON_ULTRA_MODEL", "NEMOTRON_REASONING_MODE", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SITEMEDIC_MODE", "demo")


def completion(content, usage=True, finish="stop", model="m"):
    body = {"id": "x", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return body


def models_response(ids=ACCOUNT_MODELS):
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 1, "owned_by": "x"} for i in ids]}


def make_llm(handler, **kw):
    """A Nemotron client whose HTTP layer is `handler(request) -> httpx.Response`."""
    client = openai.OpenAI(base_url="https://mock.test/v1/", api_key=KEY, max_retries=0,
                           http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    llm = Nemotron(api_key=KEY, client=client, sleep=lambda s: None, **kw)
    llm.models = dict(IDS)          # skip model discovery unless a test wants it
    return llm


class Recorder:
    """Handler that records requests and replays queued responses (last one repeats)."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    def bodies(self):
        return [json.loads(r.content) for r in self.requests if r.content]


def ok(content="hello", **kw):
    return httpx.Response(200, json=completion(content, **kw))


def err(status, message="boom", code=None, headers=None):
    return httpx.Response(status, json={"error": {"message": message, "type": "x", "code": code}}, headers=headers)
