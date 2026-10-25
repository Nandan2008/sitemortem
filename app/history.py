"""Anonymous audit history. A random id in an HttpOnly cookie names a small JSON file on the server.
No account, no email, no password: whoever holds the cookie sees that history. Only run summaries are
stored (counts, mode, target URL, timing), never the audited files or patches."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

COOKIE = "sm_uid"
MAX_ENTRIES = 50
MAX_AGE = 60 * 60 * 24 * 365
_ID = re.compile(r"^[0-9a-f]{32}$")
_lock = threading.Lock()


def data_dir() -> Path:
    d = Path(os.getenv("SITEMEDIC_DATA_DIR") or Path(__file__).resolve().parent.parent / "data") / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_id() -> str:
    return secrets.token_hex(16)


def valid(uid: str | None) -> bool:
    return bool(uid and _ID.match(uid))


def load(uid: str) -> list[dict]:
    if not valid(uid):
        return []
    try:
        data = json.loads((data_dir() / f"{uid}.json").read_text("utf8"))
        return [x for x in data if isinstance(x, dict) and x.get("run")]
    except (OSError, ValueError):
        return []


def _write(uid: str, items: list[dict]) -> None:
    d = data_dir()
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf8") as f:
        json.dump(items, f)
    os.replace(tmp, d / f"{uid}.json")


def add(uid: str, entry: dict) -> None:
    if not valid(uid):
        return
    with _lock:
        items = [x for x in load(uid) if x.get("run") != entry.get("run")]
        items.insert(0, {**entry, "ts": int(time.time() * 1000)})
        _write(uid, items[:MAX_ENTRIES])


def clear(uid: str) -> None:
    if valid(uid):
        with _lock:
            try:
                (data_dir() / f"{uid}.json").unlink()
            except OSError:
                pass


def entry_from_report(run_id: str, engine: str, mode: str, target: str, rep: dict) -> dict:
    batches = rep.get("batches") or []
    ok = sum(1 for b in batches if b.get("status") in ("fixed", "partial"))
    return {"run": run_id, "engine": engine, "mode": mode, "target": (target or "")[:120],
            "before": rep["before"]["total"], "after": rep["after"]["total"], "fixed": rep.get("fixed", 0),
            "verified": ok, "rejected": sum(1 for b in batches if b.get("status") in ("failed", "needs-review")),
            "audit_only": bool(rep.get("audit_only")), "secs": (rep.get("evidence") or {}).get("elapsed_s")}
