"""SiteMortem web app: dashboard + streaming run endpoint.

Security notes (see README "Security"):
  * No CORS middleware is installed, so browsers only allow same-origin calls to the API. Do not add a
    wildcard origin: /api/run can spend model credits.
  * API keys live only in server-side environment variables and are never sent to the browser.
  * Unexpected exceptions are logged server-side (secrets redacted) and shown to the user as a reference id.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402

from . import pipeline  # noqa: E402
from .agents import NemotronAgent, ScriptedAgent  # noqa: E402
from .llm import ModelError  # noqa: E402
from .safety import UserError, redact  # noqa: E402
from .safezip import extract_zip  # noqa: E402

log = logging.getLogger("sitemedic")
ROOT = Path(__file__).resolve().parent.parent
STATIC, DEMO = ROOT / "static", ROOT / "demo_site"
MAX_ZIP = 8 * 1024 * 1024
RUN_TIMEOUT = float(os.getenv("SITEMEDIC_RUN_TIMEOUT", "900"))       # seconds per run
LIVE_RUNS_PER_HOUR = int(os.getenv("SITEMEDIC_LIVE_RUNS_PER_HOUR", "20"))   # protects credits on public demos

app = FastAPI(title="SiteMortem", docs_url=None, redoc_url=None)
_busy = threading.Semaphore(1)  # one run at a time: each run drives a real browser
_live_runs: deque[float] = deque()
RUN_TTL = 1800          # seconds a finished run's events stay available for resume
MAX_EVENTS = 4000


class Run:
    """In-memory event log of one run. Lets a dropped browser connection resume from the last seq it saw."""

    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False
        self.cancel = threading.Event()
        self.created = time.time()

    def emit(self, ev: dict | None):
        with self.cond:
            if ev is None:
                self.done = True
            elif len(self.events) < MAX_EVENTS:
                ev.update({"run": self.id, "seq": len(self.events), "ts": round(time.time(), 3)})
                self.events.append(ev)
            self.cond.notify_all()

    def stream(self, start: int = 0):
        i = start
        while True:
            with self.cond:
                if i >= len(self.events) and not self.done:
                    self.cond.wait(timeout=15)
                batch, finished = self.events[i:], self.done and i >= len(self.events)
            if batch:
                for ev in batch:
                    yield f"data: {json.dumps(ev)}\n\n"
                i += len(batch)
            elif finished:
                return
            else:
                yield ": keep-alive\n\n"


_runs: dict[str, Run] = {}


def _expire_runs():
    now = time.time()
    for rid in [r for r, v in _runs.items() if v.done and now - v.created > RUN_TTL]:
        _runs.pop(rid, None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"          # always revalidate the UI files, so updates show immediately
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
    return resp


def keys_present() -> bool:
    return bool(os.getenv("NEBIUS_API_KEY"))


def default_mode() -> str:
    return "live" if os.getenv("SITEMEDIC_MODE", "demo").strip().lower() == "live" else "demo"


@app.get("/api/health")
def health():
    return {"ok": True, "live_available": keys_present(), "default_mode": default_mode(),
            "tavily": bool(os.getenv("TAVILY_API_KEY")), "busy": _busy._value == 0}


async def read_upload(site: UploadFile) -> bytes:
    buf, size = bytearray(), 0
    while True:
        chunk = await site.read(256 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_ZIP:
            raise HTTPException(400, "Zip is larger than 8 MB.")
        buf += chunk
    return bytes(buf)


def live_budget_ok() -> bool:
    now = time.time()
    while _live_runs and now - _live_runs[0] > 3600:
        _live_runs.popleft()
    if len(_live_runs) >= LIVE_RUNS_PER_HOUR:
        return False
    _live_runs.append(now)
    return True


def safe_message(e: Exception) -> tuple[str, str, str]:
    """(message, kind, ref) that is safe to show. Unknown errors are logged and replaced by a reference id."""
    ref = uuid.uuid4().hex[:8]
    if isinstance(e, (ModelError, UserError)):
        log.warning("run stopped [%s]: %s", ref, redact(e))
        return redact(e), getattr(e, "kind", "error"), ref
    if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
        log.error("browser missing [%s]", ref)
        return "Chromium is not installed for Playwright. Run: playwright install chromium", "browser", ref
    log.error("run failed [%s]: %s", ref, redact(repr(e)), exc_info=False)
    return f"Internal error. Reference {ref}; details are in the server log.", "internal", ref


@app.post("/api/run")
async def run(request: Request, mode: str = Form("demo"), engine: str = Form(""), scripted: bool = Form(False),
              site: UploadFile | None = File(None)):
    if mode not in ("demo", "upload"):
        raise HTTPException(400, "mode must be 'demo' or 'upload'.")
    engine = (engine or default_mode()).lower()
    if scripted:
        engine = "demo"
    if engine not in ("demo", "live"):
        raise HTTPException(400, "engine must be 'demo' or 'live'.")
    if engine == "live" and not keys_present():
        raise HTTPException(400, "Live mode needs NEBIUS_API_KEY. Add it to .env (see .env.example) and restart, "
                                 "or switch to Demo mode. SiteMortem will not fall back to scripted fixes in live mode.")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_ZIP + 1024 * 1024:
        raise HTTPException(413, "Upload is too large.")

    extracted = None
    if mode == "upload":
        if site is None:
            raise HTTPException(400, "Choose a .zip of a static site.")
        try:
            extracted = extract_zip(await read_upload(site))
        except UserError as e:
            raise HTTPException(400, redact(e))
        source = extracted.root
    else:
        source = DEMO

    def cleanup():
        if extracted:
            shutil.rmtree(extracted.tmp, ignore_errors=True)

    if not _busy.acquire(blocking=False):
        cleanup()
        raise HTTPException(429, "Another run is in progress. Try again in a minute.")
    if engine == "live" and not live_budget_ok():
        _busy.release()
        cleanup()
        raise HTTPException(429, "Live-run limit reached for this hour. Use Demo mode or try later.")

    _expire_runs()
    run_obj = Run()
    _runs[run_obj.id] = run_obj

    def worker():
        try:
            run_obj.emit({"type": "run", "engine": engine, "source": mode, "limits": {"timeout_s": RUN_TIMEOUT},
                          "skipped_files": extracted.skipped if extracted else 0})
            agent = NemotronAgent() if engine == "live" else ScriptedAgent()   # live preflight happens here
            for ev in pipeline.run(source, agent, deadline_s=RUN_TIMEOUT, should_stop=run_obj.cancel.is_set):
                run_obj.emit(ev)
        except Exception as e:
            msg, kind, ref = safe_message(e)
            run_obj.emit({"type": "error", "message": msg, "kind": kind, "ref": ref})
        finally:
            run_obj.emit(None)
            cleanup()
            _busy.release()

    threading.Thread(target=worker, daemon=True).start()
    return StreamingResponse(run_obj.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = -1):
    """Resume a run's event stream after the last `seq` the client has seen."""
    r = _runs.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown or expired run.")
    return StreamingResponse(r.stream(max(after + 1, 0)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs/{run_id}/cancel")
def run_cancel(run_id: str):
    """Ask a run to stop after its current step. Verified fixes made so far are kept; nothing is half-applied."""
    r = _runs.get(run_id)
    if not r:
        raise HTTPException(404, "Unknown or expired run.")
    r.cancel.set()
    return {"ok": True, "done": r.done}


@app.get("/api/demo-site.zip")
def demo_zip():
    """The bundled broken site as a zip, so people can try the upload path too."""
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "brewhouse-demo.zip"
    with zipfile.ZipFile(path, "w") as z:
        for p in DEMO.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(DEMO))
    return FileResponse(path, filename="brewhouse-demo.zip",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
