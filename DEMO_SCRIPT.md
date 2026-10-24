# SiteMortem demo video script (target 2:40, hard limit 3:00)

**Before recording:** run `python scripts/live_check.py`. Record the *Live* segments ONLY if it printed PASS for the
tiers and JSON mode. If anything failed, record the whole video in Demo mode and say so on screen and aloud
("patches in this run are scripted"). Never present a scripted run as live AI. Do one practice live run first and
note how long it takes; if a full live run is longer than ~90 s, record it once and speed up the waiting part
(say "sped up" on screen).

Say "NVIDIA Nemotron" and "Nebius Token Factory" out loud (judges listen for it). Screen: 1280×720, light theme first.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:15 | Demo storefront open in a tab (Brewhouse Coffee). Tab/zoom so low-contrast text and missing labels are visible. | "Most websites ship with broken accessibility and silent JavaScript errors. Teams know, but fixing hundreds of small issues is tedious, and an AI that edits code without checking is risky. SiteMortem fixes them and proves each fix worked." |
| 0:15–0:30 | SiteMortem start page. Point at the engine toggle and the pill. | "SiteMortem audits in a real headless Chromium with axe-core and captures runtime errors. Demo mode uses scripted patches; live mode calls NVIDIA Nemotron through Nebius Token Factory. I'll show live." (Or: "I'll show demo mode; patches here are scripted.") |
| 0:30–0:50 | Click **Run**. Counters appear: accessibility and runtime totals. Timeline: Audit → Triage. | "First the audit: [N] accessibility violations and [M] runtime errors. These come from axe-core and the browser, not from a language model. Then Nemotron Nano triages every issue group in parallel." |
| 0:50–1:30 | Batch cards updating: diagnosing (Ultra), patching (Super), verifying. Open one diff. If Tavily is configured, show the guidance links. | "For each group, Nemotron Ultra finds the root cause, Tavily brings in current guidance, and Nemotron Super writes a minimal search-and-replace patch. The patch is validated, applied to a temporary copy, and the browser audit runs again." |
| 1:30–1:55 | A card showing **retrying** or a rejected attempt in the evidence table (only if one genuinely occurred in your recorded run; otherwise skip this row and use the 10 seconds elsewhere). | "A fix is kept only if the targeted issues are gone and nothing new appeared. Otherwise it's reverted, and Nemotron Ultra gets one retry with the failure as feedback." |
| 1:55–2:20 | Final report: counters (before → after), PR description, **Verification evidence** panel (engine, "original files unchanged: yes", accepted vs rejected attempts, warnings). Click Download patch. | "Here are the verified results: [X] of [Y] issues fixed, and what's still open is listed honestly. The original folder was never touched, and I get a patch plus a pull-request description." |
| 2:20–2:40 | Limitations box; toggle dark mode; end card with repo URL. | "Automated checks cover only part of WCAG, and generated labels need human review. SiteMortem is built on Nebius Token Factory with NVIDIA Nemotron Nano, Super and Ultra. The model proposes; software verifies." |

**Numbers to use:** read them off the screen during your recording. The bundled demo site currently reports 24 issues
(21 accessibility, 3 runtime); a live run may fix fewer, which is a fine and honest result to show.

**Do not say:** "fully WCAG compliant", "tested on real-world sites" (unless you did), any speed or accuracy figure you
didn't measure.
