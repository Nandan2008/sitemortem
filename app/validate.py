"""Strict validation of everything a model returns, before any of it touches a file.

The models never reference issue IDs (grouping is deterministic code), so there are no IDs to hallucinate.
What they DO return is triage labels, a diagnosis and patches; all three are checked here.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

MAX_EDITS = 8
MAX_SEARCH = 4000
MAX_REPLACE = 8000
MAX_TOTAL = 24000
EDITABLE_EXT = {".html", ".htm", ".css", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
PROTECTED = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
URL = re.compile(r"https?://[^\s\"'<>)\\]+", re.I)


class PatchError(Exception):
    """A patch was rejected before or while applying. The message is safe to show."""


class ModelOutputError(Exception):
    """A model returned data that does not match the required shape."""


def _text(v, name, limit, required=True):
    if not isinstance(v, str):
        raise ModelOutputError(f"'{name}' must be a string")
    v = v.strip() if name in ("summary", "user_impact", "root_cause", "plan", "risks") else v
    if required and not v:
        raise ModelOutputError(f"'{name}' is empty")
    return v[:limit]


def validate_triage(raw) -> dict:
    if not isinstance(raw, dict):
        raise ModelOutputError("triage must be a JSON object")
    try:
        prio = int(raw.get("priority"))
    except (TypeError, ValueError):
        raise ModelOutputError("priority must be an integer 1-5")
    if not 1 <= prio <= 5:
        raise ModelOutputError("priority must be 1-5")
    cx = raw.get("complexity")
    if cx not in ("simple", "complex"):
        raise ModelOutputError("complexity must be 'simple' or 'complex'")
    return {"priority": prio, "complexity": cx, "summary": _text(raw.get("summary", ""), "summary", 300, False),
            "user_impact": _text(raw.get("user_impact", ""), "user_impact", 300, False)}


def validate_diagnosis(raw) -> dict:
    if not isinstance(raw, dict):
        raise ModelOutputError("diagnosis must be a JSON object")
    return {"root_cause": _text(raw.get("root_cause"), "root_cause", 800),
            "plan": _text(raw.get("plan"), "plan", 800),
            "risks": _text(raw.get("risks", ""), "risks", 400, False)}


def check_rel_path(rel) -> str:
    """Purely lexical checks on a model-supplied path. Returns a normalised posix path."""
    if not isinstance(rel, str) or not rel.strip():
        raise PatchError("edit has no target file")
    if "\x00" in rel or "\\" in rel:
        raise PatchError(f"unsafe path: {rel!r}")
    if rel.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", rel):
        raise PatchError(f"absolute paths are not allowed: {rel!r}")
    parts = PurePosixPath(rel).parts
    if ".." in parts:
        raise PatchError(f"path traversal is not allowed: {rel!r}")
    parts = tuple(p for p in parts if p != ".")
    if not parts:
        raise PatchError("edit has no target file")
    if any(p.startswith(".") for p in parts) or parts[-1].lower() in PROTECTED:
        raise PatchError(f"protected file: {rel!r}")
    if "node_modules" in parts or "vendor" in parts:
        raise PatchError(f"vendored/third-party files are not editable: {rel!r}")
    p = PurePosixPath(*parts)
    if p.suffix.lower() not in EDITABLE_EXT:
        raise PatchError(f"file type not allowed for edits: {rel!r}")
    return str(p)


def validate_edits_shape(raw) -> list[dict]:
    """Check structure, sizes, paths and content rules. Does not touch the filesystem."""
    edits = raw.get("edits") if isinstance(raw, dict) else None
    if not isinstance(edits, list) or not edits:
        raise PatchError("the model returned no edits")
    if len(edits) > MAX_EDITS:
        raise PatchError(f"too many edits ({len(edits)} > {MAX_EDITS})")
    out, seen, total = [], set(), 0
    for e in edits:
        if not isinstance(e, dict):
            raise PatchError("each edit must be an object")
        rel = check_rel_path(e.get("file"))
        search, replace = e.get("search"), e.get("replace")
        if not isinstance(search, str) or not isinstance(replace, str):
            raise PatchError(f"{rel}: 'search' and 'replace' must be strings")
        if not search:
            raise PatchError(f"{rel}: empty search string")
        if search == replace:
            raise PatchError(f"{rel}: edit changes nothing")
        if len(search) > MAX_SEARCH or len(replace) > MAX_REPLACE:
            raise PatchError(f"{rel}: edit is too large")
        total += len(search) + len(replace)
        if total > MAX_TOTAL:
            raise PatchError("patch is too large")
        if (rel, search) in seen:
            raise PatchError(f"{rel}: duplicate edit")
        seen.add((rel, search))
        new_urls = set(URL.findall(replace)) - set(URL.findall(search))
        if new_urls:
            raise PatchError(f"{rel}: patch adds an external URL ({sorted(new_urls)[0][:60]}), which is not allowed")
        out.append({"file": rel, "search": search, "replace": replace})
    return out
