"""Nebius Token Factory client for NVIDIA Nemotron.

Token Factory exposes an OpenAI-compatible API (base URL https://api.tokenfactory.nebius.com/v1/,
`Authorization: Bearer <key>`, GET /v1/models, POST /v1/chat/completions), so we use the official
`openai` SDK. This module is the ONLY place that talks to it.

Three logical tiers (resolved to real model IDs from env vars or the account's model list):
  nano  -> triage of issue groups      super -> first patch attempt      ultra -> diagnosis, retries, PR text

Behaviour that matters:
  * No silent fallbacks. A missing tier or an unknown pinned model is an error, not a substitution.
  * Explicit retry policy: transient failures (429, 5xx, timeout, connection) are retried up to
    MAX_RETRIES times with backoff. Auth / credit / unknown-model errors are fatal and never retried.
  * Usage is recorded only if the provider returned it; otherwise it is None (never invented).
  * Secrets never appear in error messages.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import openai
from openai import OpenAI

from .safety import redact

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
MAX_RETRIES = 2            # => at most 3 attempts per call
BACKOFF = (1.0, 3.0)       # seconds, used when the provider sends no Retry-After
MAX_RETRY_WAIT = 20.0
TIMEOUT = float(os.getenv("NEBIUS_TIMEOUT", "90"))

TIERS = ("nano", "super", "ultra")
# Auto-discovery hints (substring, case-insensitive). Pin exact IDs with NEMOTRON_<TIER>_MODEL.
TIER_HINTS = {
    "nano": ["nemotron-3-nano", "nemotron-nano"],
    "super": ["nemotron-3-super-1", "nemotron-3-super", "nemotron-super"],
    "ultra": ["nemotron-3-ultra", "nemotron-ultra"],
}


def base_url() -> str:
    return os.getenv("NEBIUS_BASE_URL") or DEFAULT_BASE_URL


class ModelError(Exception):
    """A failed model call. `fatal` means retrying (or running more batches) cannot help."""

    def __init__(self, message: str, kind: str = "error", fatal: bool = False, status: int | None = None):
        super().__init__(message)
        self.kind, self.fatal, self.status = kind, fatal, status


class _JsonModeRejected(Exception):
    pass


@dataclass
class CallLog:
    tier: str
    model: str
    purpose: str
    ms: int
    tokens: int | None      # None = the provider did not report usage
    ok: bool = True
    retries: int = 0
    error: str = ""


def _msg(e: Exception) -> str:
    return redact(getattr(e, "message", None) or str(e))[:240]


def map_error(e: Exception, model: str = "") -> tuple[ModelError, bool]:
    """Translate an SDK exception to (ModelError, retryable)."""
    if isinstance(e, openai.APITimeoutError):
        return ModelError("The model request timed out.", "timeout"), True
    if isinstance(e, openai.APIConnectionError):
        return ModelError("Could not reach Nebius Token Factory (network error).", "network"), True
    if isinstance(e, openai.AuthenticationError):
        return ModelError("Nebius rejected the API key (401). Check NEBIUS_API_KEY in .env.", "auth", True, 401), False
    if isinstance(e, openai.PermissionDeniedError):
        return ModelError(f"Nebius denied access (403) to {model or 'this model'}. {_msg(e)}", "auth", True, 403), False
    if isinstance(e, openai.NotFoundError):
        return ModelError(f"Model '{model}' was not found on this key (404). Run `python scripts/list_models.py` and "
                          f"update NEMOTRON_*_MODEL.", "model_not_found", True, 404), False
    if isinstance(e, openai.RateLimitError):
        return ModelError("Rate limit reached (429).", "rate_limit", False, 429), True
    if isinstance(e, openai.APIStatusError):
        code = e.status_code
        if code == 402:
            return ModelError("Nebius reports the account has no credits left (402).", "credits", True, 402), False
        if code >= 500:
            return ModelError(f"Nebius server error ({code}).", "server", False, code), True
        return ModelError(f"Nebius rejected the request ({code}): {_msg(e)}", "bad_request", False, code), False
    return ModelError(f"Unexpected error: {type(e).__name__}", "error"), False


def _retry_after(e: Exception) -> float | None:
    try:
        v = e.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return min(float(v), MAX_RETRY_WAIT) if v else None
    except Exception:
        return None


@dataclass
class Nemotron:
    api_key: str | None = field(default_factory=lambda: os.getenv("NEBIUS_API_KEY") or None)
    client: OpenAI | None = None          # injectable for tests
    sleep: object = time.sleep
    models: dict[str, str] = field(default_factory=dict)
    log: list[CallLog] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # honest warnings surfaced in the report
    json_mode: bool = True

    def __post_init__(self):
        if self.client is None and self.api_key:
            self.client = OpenAI(base_url=base_url(), api_key=self.api_key, timeout=TIMEOUT, max_retries=0)

    @property
    def live(self) -> bool:
        return self.client is not None

    # ---------------------------------------------------------------- discovery / preflight
    def list_ids(self) -> list[str]:
        if not self.live:
            raise ModelError("NEBIUS_API_KEY is not set. Add it to .env (see .env.example).", "config", True)
        try:
            return sorted(m.id for m in self.client.models.list().data)
        except Exception as e:
            raise map_error(e)[0]

    def preflight(self) -> dict[str, str]:
        """Resolve all three tiers to real model IDs on this key, or raise a clear error."""
        if len(self.models) == 3:
            return self.models
        ids = self.list_ids()
        lower = {i.lower(): i for i in ids}
        nemotron = [i for i in ids if "nemotron" in i.lower()]
        resolved, problems = {}, []
        for tier in TIERS:
            pinned = os.getenv(f"NEMOTRON_{tier.upper()}_MODEL")
            if pinned:
                hit = pinned if pinned in ids else lower.get(pinned.lower())
                if hit:
                    resolved[tier] = hit
                else:
                    problems.append(f"NEMOTRON_{tier.upper()}_MODEL='{pinned}' is not available on this key")
                continue
            hit = next((i for h in TIER_HINTS[tier] for i in nemotron if h in i.lower()), None)
            if hit:
                resolved[tier] = hit
            else:
                problems.append(f"no Nemotron model matching the '{tier}' tier was found; set NEMOTRON_{tier.upper()}_MODEL")
        if problems:
            raise ModelError("Model configuration problem: " + "; ".join(problems) +
                             ". Run `python scripts/list_models.py` to see what your key can use.",
                             "model_not_found", True)
        self.models = resolved
        return resolved

    # ---------------------------------------------------------------- calls
    def _create(self, tier, messages, max_tokens, temperature, extra, use_json):
        kwargs = dict(model=self.models[tier], messages=messages, max_tokens=max_tokens, temperature=temperature)
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}
        if extra:
            kwargs["extra_body"] = extra
        return self.client.chat.completions.create(**kwargs)

    def _call(self, tier: str, system: str, user: str, purpose: str, max_tokens: int, temperature: float,
              reasoning: bool, use_json: bool) -> str:
        if not self.live:
            raise ModelError("NEBIUS_API_KEY is not set. Add it to .env (see .env.example).", "config", True)
        self.preflight()
        model = self.models[tier]
        mode = os.getenv("NEMOTRON_REASONING_MODE", "system").lower()   # system | effort | off
        extra = None
        if mode == "system":   # Nemotron's documented system-prompt switch
            system = ("detailed thinking on\n\n" if reasoning else "detailed thinking off\n\n") + system
        elif mode == "effort":
            extra = {"reasoning_effort": "high" if reasoning else "none"}
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        t0, retries, last = time.time(), 0, None
        for attempt in range(MAX_RETRIES + 1):
            try:
                try:
                    resp = self._create(tier, messages, max_tokens, temperature, extra, use_json and self.json_mode)
                except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                    if use_json and self.json_mode and "response_format" in _msg(e).lower():
                        raise _JsonModeRejected()
                    raise
                break
            except _JsonModeRejected:
                self.json_mode = False
                self.notes.append("Provider rejected response_format=json_object; continuing with prompt-only JSON.")
                continue   # next loop iteration re-sends without response_format
            except Exception as e:
                err, retryable = map_error(e, model)
                last = err
                if not retryable or attempt == MAX_RETRIES:
                    self._record(tier, model, purpose, t0, None, False, retries, err)
                    raise err
                retries += 1
                self.sleep(_retry_after(e) or BACKOFF[min(attempt, len(BACKOFF) - 1)])
        else:  # pragma: no cover
            raise last or ModelError("model call failed")

        try:
            choice = resp.choices[0]
            text = choice.message.content or ""
        except (AttributeError, IndexError, TypeError):
            err = ModelError("The model returned a malformed response (no choices).", "bad_response")
            self._record(tier, model, purpose, t0, None, False, retries, err)
            raise err
        u = getattr(resp, "usage", None)
        tokens = getattr(u, "total_tokens", None) if u else None
        if getattr(choice, "finish_reason", None) == "length":
            err = ModelError("The model's answer was cut off (max_tokens reached).", "truncated")
            self._record(tier, model, purpose, t0, tokens, False, retries, err)
            raise err
        text = strip_think(text)
        if not text:
            err = ModelError("The model returned an empty answer.", "bad_response")
            self._record(tier, model, purpose, t0, tokens, False, retries, err)
            raise err
        self._record(tier, model, purpose, t0, tokens, True, retries, None)
        return text

    def _record(self, tier, model, purpose, t0, tokens, ok, retries, err):
        self.log.append(CallLog(tier, model, purpose, int((time.time() - t0) * 1000), tokens, ok, retries,
                                err.kind if err else ""))

    def chat(self, tier: str, system: str, user: str, purpose: str, max_tokens: int = 1500,
             temperature: float = 0.3, reasoning: bool = False) -> str:
        return self._call(tier, system, user, purpose, max_tokens, temperature, reasoning, use_json=False)

    def chat_json(self, tier: str, system: str, user: str, purpose: str, max_tokens: int = 1500,
                  temperature: float = 0.3, reasoning: bool = False):
        text = self._call(tier, system + "\n\nRespond with ONLY valid JSON, no prose, no code fences.",
                          user, purpose, max_tokens, temperature, reasoning, use_json=True)
        try:
            return parse_json(text)
        except (json.JSONDecodeError, ValueError):
            err = ModelError("The model's answer was not valid JSON.", "bad_response")
            if self.log:
                self.log[-1].ok, self.log[-1].error = False, "bad_json"
            raise err


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:           # unterminated reasoning block = no usable answer
        text = text.split("<think>")[0]
    return text.strip()


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(1))
