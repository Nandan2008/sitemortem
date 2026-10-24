/* Runs an audit and keeps the event stream alive. If the connection drops, it resumes from the last event seen
   (GET /api/runs/{id}/events?after=seq). It never invents progress: it only forwards events the server sent. */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const TERMINAL = new Set(["done", "error"]);

export class Runner {
  constructor({ onEvent, onConn }) { this.onEvent = onEvent; this.onConn = onConn; this.ctrl = null; this.run = null; this.seq = -1; this.finished = false; }

  /** Returns {ok:true} once the server accepted the run, or {error:{status,detail}} if it refused it. */
  async start(form) {
    this.abort(); this.ctrl = new AbortController(); this.finished = false; this.run = null; this.seq = -1;
    let res;
    try { res = await fetch("/api/run", { method: "POST", body: form, signal: this.ctrl.signal }); }
    catch { return { error: { status: 0, detail: "Could not reach the SiteMortem server. Is it still running?" } }; }
    if (!res.ok) {
      let d = res.statusText;
      try { const j = await res.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* keep statusText */ }
      return { error: { status: res.status, detail: d } };
    }
    this.pump(res);
    return { ok: true };
  }

  async pump(first) {
    let res = first, tries = 0;
    const signal = this.ctrl.signal;
    while (!this.finished) {
      try { await this.read(res); } catch { if (signal.aborted) return; }
      if (this.finished || signal.aborted) return;
      if (!this.run) { this.emit({ type: "error", kind: "network", message: "The connection dropped before the run started." }); return; }
      for (;;) {                                           // stream ended without a terminal event: resume
        if (++tries > 6) { this.onConn("failed"); return; }
        this.onConn("reconnecting", tries);
        await sleep(Math.min(1000 * 2 ** (tries - 1), 8000));
        if (signal.aborted) return;
        try {
          res = await fetch(`/api/runs/${this.run}/events?after=${this.seq}`, { signal });
          if (res.status === 404) { this.onConn("expired"); return; }
          if (res.ok) { this.onConn("ok"); tries = 0; break; }
        } catch { if (signal.aborted) return; }
      }
    }
  }

  async read(res) {
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        let ev; try { ev = JSON.parse(chunk.slice(6)); } catch { continue; }      // ignore malformed frames
        this.emit(ev);
      }
    }
  }

  emit(ev) {
    if (!ev || typeof ev !== "object") return;
    if (ev.run) this.run = ev.run;
    if (typeof ev.seq === "number") { if (ev.seq <= this.seq) return; this.seq = ev.seq; }   // drop duplicates after a resume
    if (TERMINAL.has(ev.type)) this.finished = true;
    this.onEvent(ev);
  }

  async cancel() {
    if (!this.run) return false;
    try { return (await fetch(`/api/runs/${this.run}/cancel`, { method: "POST" })).ok; } catch { return false; }
  }
  abort() { this.finished = true; this.ctrl?.abort(); }
}
