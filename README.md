# SiteMortem

**Find what's broken. Fix it. Prove it worked.**

SiteMortem is an agent that audits a website for **accessibility barriers** and **runtime errors**, patches the
code, **re-tests every fix in a real browser**, and keeps only the fixes that verifiably work. You get a patch
and a pull-request description.

Built for the **Nebius × NVIDIA Global AI Hackathon** · Track: **Coding and Agentic Engineering**

---

## Why

Roughly one in six people lives with a disability, and most sites still fail basic checks: images with no alt
text, unlabeled form fields, unreadable contrast. JavaScript errors and broken API calls quietly break features.
Teams know, but fixing hundreds of small issues is tedious, and an AI that "fixes" code without checking is a
liability. SiteMortem's answer: the model proposes, **software verifies**.

## How it works

```
 axe-core + Playwright (real Chromium, local-only network)   <- finds issues; the ground truth
        |
 Nemotron Nano   triage every issue group, in parallel (cheap, fast)
        |
 for each group, highest impact first:
   Tavily          current fix guidance (WCAG, MDN, web.dev), shown as untrusted reference text
   Nemotron Ultra  root-cause analysis (reasoning on) for runtime / complex groups
   Nemotron Super  writes a minimal search/replace patch
   validate        paths, sizes, file types, URLs, exact-once match; then apply to a COPY of the site
   RE-AUDIT        same audit as before
        |-- targeted issues gone AND nothing new appeared  -> keep the fix
        '-- otherwise -> revert, then retry ONCE with Ultra + the failure as feedback; then give up
        |
 Nemotron Ultra  writes the pull-request description from the verified results
```

Grouping into issue groups is deterministic code (by rule and message). Nano labels each group (priority,
complexity, summary); it never references issue IDs, so there are none to hallucinate. If a triage call fails or
returns invalid JSON, SiteMortem falls back to deterministic labels **and says so** in the UI and the report.

### Why you can trust a result

- The audit is *not* an LLM opinion. axe-core and the browser's own error events decide what is broken.
- A patch is kept only if the re-audit shows the targeted issues are gone **and** no new issue fingerprints appeared.
  Otherwise it is reverted automatically.
- Patches are exact-match search/replace edits that must match once. All-or-nothing: one bad edit rolls back the rest.
- Model output is validated before use: allowed file types only (HTML/CSS/JS family), no absolute paths, `..`, dotfiles,
  `package.json`, vendored files or symlinks; size limits; no newly introduced external URLs.
- The original folder is never modified. A hash of it is checked before and after every run and shown in the report.
- Rejected attempts are listed in the report and are not part of the patch.

### Engines: Demo vs Live

| | Demo engine | Live engine |
|---|---|---|
| Patches written by | scripted code (bundled demo site only) | NVIDIA Nemotron via Nebius Token Factory |
| Keys needed | none | `NEBIUS_API_KEY` (+ optional `TAVILY_API_KEY`) |
| Audit + verification | real | real |

Live mode never silently falls back to scripted fixes. If the key, a model ID or the connection is wrong, the run
stops with an actionable message. The dashboard pill and the report both state which engine ran.

### How NVIDIA Nemotron is used (on Nebius Token Factory)

| Tier | Job |
|---|---|
| **Nemotron Nano** | triage of every issue group, in parallel |
| **Nemotron Super** | first-attempt patch for each group |
| **Nemotron Ultra** | root-cause analysis (reasoning on), second-attempt patches, PR write-up |

All calls go through Token Factory's OpenAI-compatible API (`https://api.tokenfactory.nebius.com/v1/`) with the
`openai` SDK, in one module (`app/llm.py`). Model IDs come from `NEMOTRON_*_MODEL` or are discovered from
`GET /v1/models` by name; a missing tier is an error, never a substitution. Retry policy: transient errors (429,
5xx, timeouts) are retried up to 2 times with backoff; bad key, no credits and unknown model are fatal.
Token counts shown in the dashboard are only what the provider reports.

### How Tavily is used
Before patching a group, SiteMortem sends a short generic query (rule name or normalised error text, never source code
or HTML) to Tavily, restricted to w3.org, MDN, web.dev, Deque and Stack Overflow. Results are trimmed and passed to
the model inside a block labelled untrusted. If Tavily is not configured, empty or failing, the step is shown as
**skipped / empty / error** with the reason, and repair continues without it.

## Run it

```bash
git clone <your-repo-url> && cd sitemedic
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                                   # Windows: copy .env.example .env
uvicorn app.main:app --reload
```

Open http://localhost:8000, choose what stage your project is at, and press **Run audit**:

- **Still building**: paste a public GitHub repository URL. SiteMortem downloads a copy (up to 25 MB, your repo is never
  modified) and runs the full audit, patch, verify loop. A site inside `public/`, `docs/`, `dist/`, `build/`, `site/`,
  `www/`, `src/` or `static/` is found automatically.
- **Already built and live**: paste your website URL. SiteMortem audits up to 6 linked pages in a real browser and returns
  the issues with suggested fixes. Report only: no model calls and nothing is changed.
- **Upload a .zip** of a static site, or try the bundled **demo storefront**.

### The dashboard
- **Dashboard**: launch panel (GitHub repo, live site URL, ZIP or demo storefront, with validation), then a live run view: metric cards (accessibility, JavaScript, network, resolved, remaining, verified/rejected patches, each with before values and a "how calculated" note), a repair-pipeline timeline driven by real backend events, an activity log, and three tabs.
- **Issues** tab: filters (category, severity, resolved/unresolved/rejected), search, sort, expandable technical details.
- **Patches** tab: per-patch evidence label (PROPOSED / VERIFYING / VERIFIED / ROLLED BACK / REJECTED), diff viewer, reason and verification outcome. VERIFIED appears only when the backend's re-audit confirmed it; there are no AI "confidence" numbers.
- **Summary** tab: severity before/after, patch outcomes, repair time per group, verification evidence, PR text, downloads.
- **History**: past runs, saved on the server under an anonymous HttpOnly cookie (no account, no login; only run summaries are stored, in `data/history/`). **Help**: how to read the results.
- Stop button cancels a run; if the connection drops the page resumes the event stream without duplicating events.
- Light/dark theme, keyboard accessible, respects reduced motion; axe-core is run against SiteMortem's own UI in the tests.
- Not implemented: a sandboxed live preview of the patched site (download the patch instead).

### Demo mode (no keys)
Leave `.env` keys empty. Pick **Demo engine**. The audit and every verification are real; patches are scripted and
labelled "Scripted (demo)".

### Live mode
1. Put `NEBIUS_API_KEY` (and optionally `TAVILY_API_KEY`) in `.env`.
2. `python scripts/list_models.py` and pin the Nano / Super / Ultra IDs in `.env`.
3. `python scripts/live_check.py` makes a few tiny real calls and prints PASS / FAIL / SKIP for each (model listing,
   each tier, JSON mode, patch-shaped output, reasoning, Tavily). Run this before relying on live mode.
4. Pick **Live · Nemotron** in the UI (or set `SITEMEDIC_MODE=live` as the default).

### Environment variables
See [.env.example](.env.example): `SITEMEDIC_MODE`, `NEBIUS_API_KEY`, `NEBIUS_BASE_URL`, `NEMOTRON_NANO_MODEL`,
`NEMOTRON_SUPER_MODEL`, `NEMOTRON_ULTRA_MODEL`, `NEMOTRON_REASONING_MODE`, `NEBIUS_TIMEOUT`, `TAVILY_API_KEY`,
`SITEMEDIC_RUN_TIMEOUT`, `SITEMEDIC_LIVE_RUNS_PER_HOUR`.

### API
`GET /api/health` (ok, live_available, default_mode, tavily, busy) · `POST /api/run` (form: mode = demo | upload | repo | url, engine, site, repo_url, site_url) streams events · `GET` / `DELETE /api/history` ·
`GET /api/runs/{id}/events?after=N` resumes a stream · `POST /api/runs/{id}/cancel` · `GET /api/demo-site.zip`.
Every event carries `run`, `seq` and `ts`.

### Tests
```bash
python -m pytest -q                 # everything (~5 min; the slow ones drive real Chromium)
python -m pytest -q -m "not slow"   # fast unit tests only
```
No test calls the real Nebius or Tavily APIs: model traffic goes through a mocked HTTP transport, and keys are
cleared for every test. What is covered: Nebius client errors/retries/malformed output, Tavily errors, patch
validation and path safety, ZIP upload security, rollback and retry, a full live-path run against a mocked
provider, SSRF blocking, run deadline, streaming failures, and browser tests of the dashboard (`tests/test_ui.py`, needs Chromium: layout at 5 viewport sizes, axe on every view in both themes, keyboard, stream resume, cancel).
`scripts/live_check.py` is the only thing that talks to the real services, and only when you run it.

### Docker
```bash
docker build -t sitemedic .
docker run --rm -p 8000:8000 --env-file .env sitemedic
```
The image is based on Playwright's Python image (Chromium libraries included) and runs `playwright install chromium`
so the browser matches the installed Playwright version.

### Deploy for free (Render backend + Vercel frontend)
- **Backend on Render:** New → Blueprint → pick this repo. `render.yaml` creates `sitemortem-api` on the free Docker plan
  (health check `/api/health`, low-memory Chromium flags, live mode capped at 5 runs/hour). Enter `NEBIUS_API_KEY`
  (and optionally `TAVILY_API_KEY`) when Render asks for them.
- **Frontend on Vercel:** import this repo with no build step. `vercel.json` serves `static/` and proxies `/api/*` to the
  Render service, so the browser only talks to one origin (no CORS, first-party history cookie).
- Render's free plan sleeps after 15 minutes idle. The page shows "Waking up the server…" and retries for up to a minute.
  It has 512 MB of RAM: the bundled demo peaks around 400 MB; very heavy sites may not fit.

### Deploy (Nebius Serverless Endpoint)
Every push to `main` builds the Docker image in GitHub Actions, smoke-tests it (server starts, Chromium audits the demo
site end to end) and publishes it to `ghcr.io/nandan2008/sitemortem:latest`.

Create the endpoint in the Nebius console (Serverless AI → Endpoints → Create endpoint → Custom):
- **Image path:** `ghcr.io/nandan2008/sitemortem:latest` · **Port:** `8000` · **Authentication:** none (judges open it in a browser)
- **Secret environment variables:** `NEBIUS_API_KEY` (and optionally `TAVILY_API_KEY`)
- **Environment variables:** `SITEMEDIC_MODE=live`, `SITEMEDIC_LIVE_RUNS_PER_HOUR=5`
- **Compute:** a CPU platform (for example `cpu-d3`, preset `4vcpu-16gb`), disk 20 GiB+

Or with the CLI:
```bash
nebius ai endpoint create --name sitemortem --image ghcr.io/nandan2008/sitemortem:latest --container-port 8000 \
  --env "SITEMEDIC_MODE=live,SITEMEDIC_LIVE_RUNS_PER_HOUR=5" --env-secret NEBIUS_API_KEY=<secret> \
  --platform cpu-d3 --preset 4vcpu-16gb --disk-size 20Gi --subnet-id <subnet_id>
```
One audit runs at a time per instance. Live runs spend your Token Factory credits, so keep the hourly cap low.
History is stored inside the container and resets when the endpoint restarts.

## Security

- **Uploads:** size, entry-count and expanded-size limits enforced on bytes actually written; absolute paths, `..`,
  backslashes, drive letters, symlinks and encrypted entries rejected; only web file types accepted; dotfiles are never
  extracted; temp files are removed on success and on failure.
- **SSRF:** an uploaded site's JavaScript runs in headless Chromium on the server. Every request except to the local copy
  of that site is aborted, and DNS is disabled in the browser as a second layer. Blocked hosts are reported as a limitation.
- **URL inputs:** repository URLs must be `https://github.com/owner/repo`; the zip comes from `codeload.github.com` only
  (no redirects followed), size-capped while streaming, then goes through the same safe extractor. Live-site URLs must be
  http(s) on port 80/443 with a hostname (no raw IPs); every resolved address must be public (loopback, private,
  link-local/metadata, CGNAT and reserved ranges are refused), and the audit browser is pinned to the checked addresses
  so DNS cannot change between the check and the visit. Other hosts are blocked during the audit.
- **Patches:** validated, applied only to a temporary copy, all-or-nothing, never executed as commands; no dependency installs.
- **Prompt injection:** site content and search results are treated as untrusted data in the prompts; a model reply can
  only ever produce validated text edits, which are then verified by the audit.
- **Web app:** no CORS (same-origin only), CSP and other security headers, keys stay server-side and are redacted from
  errors and logs, unexpected errors reach the browser as a reference id only, one run at a time, a per-run time limit,
  and an hourly cap on live runs.
- **Not covered:** this is a hackathon-grade service. There is no authentication, per-user rate limiting or sandboxing
  beyond the browser and the checks above; do not expose a live-mode instance with valuable credits to the open internet
  without adding them. Uploaded source is sent to the model provider in live mode.

## Limitations (stated plainly)

- Automated testing finds only part of WCAG. A clean result is **not** a compliance certificate.
- Generated alt text, labels and colours need human review before merging.
- Works on **static sites** (HTML, CSS, JS). Live-site audits are report-only: suggestions are not verified fixes. Server-side bugs, auth-protected pages and framework build pipelines are out of scope.
- Only the pages reachable by links (up to 6) are crawled, and only their initial load is audited (no interaction scripts).
- External resources (CDNs, fonts) are blocked during the audit for safety, so issues that depend on them are not seen.
- It does not judge whether alt text is *good*, only that it exists.
- The pull request is produced as a `.diff` and a description. You apply it and open the PR yourself.
- Demo-mode patches are scripted and work only for the bundled demo site.

## Project layout
```
app/audit.py       Chromium + axe-core audit, runtime error capture, network lock-down, fingerprints
app/pipeline.py    the agent loop: triage, diagnose, patch, apply, re-audit, keep or revert, evidence
app/agents.py      Nemotron agent (prompts per tier) and the scripted demo agent
app/llm.py         the only Nebius Token Factory client: tiers, preflight, retries, usage, errors
app/tavily.py      Tavily client with explicit ok/empty/skipped/error results
app/validate.py    strict validation of triage, diagnosis and patches
app/repo.py        working copy, safe search/replace, rollback, diffs, source hash
app/safezip.py     safe ZIP extraction
app/remote.py      GitHub repo download and live-site URL checks (SSRF)
app/siteaudit.py   report-only audit of a live website
app/history.py     anonymous cookie-based history store
app/main.py        FastAPI app, run store + event stream, limits, headers
static/            dashboard (no build step): index.html, styles.css, js/ (ES modules: main, state, stream, render, history, util)
                   static/vendor/axe.min.js is axe-core
demo_site/         a small coffee-shop site with planted accessibility and runtime bugs
scripts/           list_models.py, live_check.py
tests/             pytest suite
```

## Third-party
axe-core by Deque Systems, MPL-2.0 (`static/vendor/`). Everything else is MIT, see [LICENSE](LICENSE).
