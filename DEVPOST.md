# Devpost submission draft: SiteMortem

> Fill the [brackets] only with things you have actually run. Items marked VERIFY need `python scripts/live_check.py`
> (live Nebius/Tavily) or a deployed URL before you claim them. Not submitted; nothing here was posted anywhere.

**Project name:** SiteMortem

**Tagline:** An agent that fixes accessibility and runtime bugs in your site, then proves each fix worked in a real browser.

**Track:** Coding and Agentic Engineering  ·  **Also eligible for:** Best Use of Tavily (VERIFY a live Tavily call first)

## Inspiration / problem
Most sites fail basic accessibility checks (missing alt text, unlabeled fields, unreadable contrast), and JavaScript
errors and broken API calls quietly break features. Teams know, but fixing hundreds of small issues is tedious. LLM
code fixers help, but "the model said it's fixed" isn't evidence, so people don't trust them to edit real code.

## What it does
SiteMortem audits a static site in headless Chromium (axe-core for WCAG 2.0/2.1 A and AA, plus captured JS errors,
console errors and failed requests), groups the findings, fixes each group with a minimal patch, and **re-runs the same
audit** to verify. A patch is kept only if the targeted issues disappear and nothing new appears; otherwise it is reverted
and retried once with a stronger model. You receive a before/after report, the accepted and rejected attempts, a downloadable
`.patch`, and a pull-request description. A demo coffee-shop site with planted bugs is bundled; you can also upload a `.zip` of your own static site.

## How it works (agentic workflow)
Detect → Triage → Investigate → Patch → Verify → Retry (once) → Report.
- **Nemotron Nano** triages every issue group in parallel (priority, complexity, summary).
- **Tavily** fetches current guidance from WCAG/MDN/web.dev for the defect class (shown as sources; treated as untrusted).
- **Nemotron Ultra** does root-cause analysis with reasoning on for runtime/complex groups, writes the retry patch after a failed
  verification, and writes the PR description.
- **Nemotron Super** writes the first-attempt patch as exact-match search/replace edits.
- Software, not the model, decides what is kept: validation → apply to a temp copy → browser re-audit → keep or roll back.

## Use of NVIDIA Nemotron and Nebius Token Factory
All model calls go through Nebius Token Factory's OpenAI-compatible API, in a single client module with tier resolution from
the account's `/v1/models` list (or pinned IDs), a bounded retry policy, usage reporting only when the provider supplies it, and no
silent fallbacks: live mode stops with a clear error instead of switching to scripted output. Models on our account at the time of
writing: Nemotron-3 Nano 30B-A3B, Nemotron-3 Super 120B-A12B, Nemotron-3 Ultra 550B-A55B (listed by `scripts/list_models.py`).
**VERIFY:** live end-to-end behaviour on these models (run `scripts/live_check.py` and one full live run) before submitting, and add the
real numbers you observed here: [live run: X of Y fixed, Z s].

## Agentic engineering features
- Verification loop with automatic rollback; bounded retry that escalates to a stronger model with failure feedback.
- Strict validation of all model output (paths, file types, sizes, exact-once matches, no new external URLs) before anything is written.
- Original files hashed before and after every run (shown in the report).
- Honest reporting: rejected attempts, skipped steps (e.g. Tavily not configured) and fallbacks are all surfaced in the UI.
- Security by design for uploaded sites: safe ZIP extraction, network lock-down of the audit browser (SSRF), prompt-injection-aware prompts.

## Challenges and lessons learned
- Making verification fair: the same page can be reachable as `/` and `/index.html`, and issue IDs shift between audits, so issues are
  fingerprinted to tell "newly introduced" from "already there".
- A fix that exposes a latent bug looks like a regression; the demo site is built so runtime bugs are independent, and the report explains
  every rejection.
- Uploaded sites are untrusted code run in a browser on a server, which forced a strict egress block and archive handling.
- [Add what you learned from the first real Nemotron runs: prompt tweaks, JSON behaviour, reasoning-mode behaviour.]

## Known limitations
Automated testing finds only part of WCAG; a clean result is not compliance. Generated labels and alt text need human review. Static sites only
(HTML/CSS/JS), up to 6 linked pages, initial load only. External CDNs are blocked during the audit. Demo-mode patches are scripted and only work for the
bundled site. The app produces a patch and PR text but does not open the GitHub PR itself. No authentication or per-user rate limiting beyond a global
cap on live runs.

## What's next
Open the pull request via the GitHub API; interaction scripts (keyboard traversal, forms) in the audit; framework projects (React/Vue) via build-aware
edits; a human-in-the-loop review step for generated alt text; a regression suite of real-world sites.

## Built with
Python, FastAPI, Playwright/Chromium, axe-core, NVIDIA Nemotron (Nano, Super, Ultra) on Nebius Token Factory, Tavily, Docker, pytest.

## Links to add
Public GitHub repo (MIT license visible): [VERIFY]  ·  Demo URL: [VERIFY after deploy]  ·  YouTube video (<3 min, public): [VERIFY]

## Tool feedback (for the Most Valuable Feedback prize; write only what you experienced)
- Token Factory docs list both `json_object` and `json_schema` for `response_format` but say only `json_object`/`text` are supported; the API reference
  does not document how to toggle Nemotron's reasoning mode (we use the system-prompt switch and expose `reasoning_effort` as an option). [Confirm after live_check.]
- [Add real friction/wins from your account setup, model availability, latency, rate limits.]
