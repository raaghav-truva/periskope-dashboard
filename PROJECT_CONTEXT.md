# Truva Home Loans — Periskope Lead Funnel Dashboard: Project Context

Upload this file to the "Periskope" Claude.ai project's knowledge base so any new chat in that project has full context on this dashboard without needing to re-explain it.

## What this is

A static, no-build-step dashboard tracking the Truva Home Loans lead funnel, built on top of the Periskope WhatsApp platform's contact/label data, with an optional Airtable CRM cross-check. Hosted free on GitHub Pages, refreshed automatically once a day via GitHub Actions (plus a client-side 12-hour re-fetch of the already-generated data).

- Repo: `raaghav-truva/periskope-dashboard` (GitHub)
- Live site: `https://raaghav-truva.github.io/periskope-dashboard/`
- Local clone: `~/Documents/GitHub/periskope-dashboard`
- No backend server — Python script runs in CI, writes static JSON, static HTML/CSS/JS reads it client-side.

## Architecture

1. **`fetch_periskope_data.py`** — the only "backend." Runs in GitHub Actions (or manually). Calls the Periskope REST API (`https://api.periskope.app/v1`) with `PERISKOPE_API_KEY`, pulls all org contacts and their labels, computes every stat the dashboard shows, and writes `data.json` (current snapshot) and appends to `history.json` (daily time series of label counts, used for the "daily marks by label" table).
   - Also optionally calls the Airtable REST API directly (its own lightweight urllib client, not the Airtable MCP — CI needs its own token, separate from any session-level OAuth) if `AIRTABLE_TOKEN` is set, to cross-check the "Home Loans CRM" Airtable base (`appfpELkqjpJ0wfZ2`), "Leads" table (`tbl8UiIyUJHN1lTz5`) against Periskope labels. Matching is by phone number (last 10 digits, non-digits stripped). This entire section is optional and silently skips if the token isn't configured — it never fails the build.
2. **`.github/workflows/update-dashboard.yml`** — GitHub Actions workflow. Triggers: daily cron at `23 3 * * *` UTC (~8:53am IST — deliberately off the top of the hour, since GitHub's own docs warn `:00` cron slots are the most likely to be delayed/dropped) plus manual `workflow_dispatch`. Runs the fetch script with `PERISKOPE_API_KEY` and `AIRTABLE_TOKEN` as repo secrets, then commits `data.json`/`history.json` back to `main` as the `dashboard-bot` user.
3. **Three static HTML pages**, no framework, no build step, sharing identical nav/theme/font/modal chrome:
   - `index.html` — **Overview**. Top-line stats (total contacts, per-segment counts), each stat (except "Total contacts in org") clickable to open a modal listing the actual name+phone contacts behind that number. "01 Contacts by label" grid below.
   - `message-analytics.html` — **Message Analytics**. M-series funnel (M0/M1/M2 progression), stuck-in-stage breakdown, and a day-by-day table of who got marked with which label and when (backed by `history.json`).
   - `airtable-sync.html` — **Airtable Sync**. Reconciliation table: total Airtable leads, leads missing phone numbers, matched-to-Periskope count, leads in Airtable with no Periskope match, Periskope "Home Loans" contacts missing from Airtable, and Status(Airtable)-vs-Label(Periskope) mismatches for the 4 statuses that map directly to a Periskope label. All rows with a nonzero count are clickable for the underlying contact list.
   - All three pull the same `data.json` client-side (`fetch('data.json?_=' + Date.now())`), re-fetching every 12 hours in-browser, so the dashboard also "refreshes" without a full page reload between daily CI runs (though the underlying data itself only changes once a day via the CI job).

## Design system

- Brand colors: Truva orange `#FF4802` (`--accent`), with `#FF7A40`/`#FF8A50` as a lighter accent depending on theme, black/near-black backgrounds in dark mode.
- Font: Google Fonts **Poppins**, loaded via `<link>`, used across `--font-display`, `--font-body`, `--font-mono` (replaced an earlier serif/system-mono stack).
- **Light/dark theme toggle**: persisted to `localStorage` (`truva-theme` key), defaults to `prefers-color-scheme` if unset, applied via a `data-theme` attribute on `<html>` set by an inline `<script>` at the very top of `<head>` (before first paint, to avoid a flash of the wrong theme). CSS is `:root` (light vars) + `html[data-theme="dark"]` (dark override block).
- Logo: Truva wordmark (`logo.png`, sits in repo root next to the HTML files, referenced with a graceful `onerror` fallback that hides it if missing). Header is a flex row: text block (`.header-text` — eyebrow/h1/sub/refresh-note) on the left, logo on the right, vertically centered. Logo height is currently **64px** — was iterated up to 1048px per explicit user request, then brought back down once inline-next-to-text made that unworkable and created huge negative space; open to further size tweaks.
- Shared nav bar links between all three pages, active page highighted.

## Known constraints / things ruled out

- Periskope's API has **no per-label-change timestamp** — only a whole-record `updated_at`. So "when was this label added" is a backfill estimate, not a true historical log. This was surfaced to the user as a real limitation, not a bug.
- Periskope's API has **no "Automation Rules" endpoint** (confirmed against docs.periskope.app) — so the 3 automations the user built in the Periskope UI cannot be surfaced on this dashboard; there's nothing to fetch.
- No Claude-in-Chrome browser was connected during the redesign, and `truva.in` is a client-rendered SPA that returns empty on a plain fetch — so the live truva.in site's exact visual design could not be inspected/matched pixel-for-pixel. The dashboard's card/table structure was re-skinned in brand colors/fonts as a best-effort match, not a literal clone.
- GitHub's cron scheduler is well documented to delay/drop exact-top-of-hour (`:00`) runs; this dashboard's cron was deliberately set to `:23` to avoid that.

## Recent history / decisions worth knowing

- The Airtable sync feature and per-stat contact drill-downs were both built and verified against live data (455 Airtable leads, 250 matched at last check).
- The 3-page split (Overview / Message Analytics / Airtable Sync) was a deliberate reorganization requested by the user to declutter the original single-page layout.
- The daily cron was previously never firing (all workflow runs were manual `workflow_dispatch`) — root-caused to GitHub's top-of-hour scheduling caveat and fixed by shifting off `:00`.
- Logo sizing went through several iterations at the user's explicit direction (48px → 192px → 1048px → 64px), each applied literally; the header layout itself was restructured from stacked (logo above heading) to a horizontal flex row to fix an excessive-negative-space complaint, which is what necessitated bringing the logo size back down from 1048px.

## Pending / not yet done

- Lower priority, not currently being worked on unless asked again: the `api_reported_count` diagnostic in `fetch_periskope_data.py` may produce a permanent false-positive warning based on a mistaken assumption; the legacy `"snapshots"` key in `history.json` is dead/unused data left over from an earlier iteration.
- No outstanding visual/functional bugs as of the last check — all three pages verified to share consistent header/nav/theme/modal behavior.

## How to make changes going forward

Any new chat working on this project should: read the actual files in `~/Documents/GitHub/periskope-dashboard` directly (this doc is a map, not a substitute for the code), make edits directly in that folder, then hand the user this exact push sequence (I cannot push on the user's behalf):

```
git pull --rebase origin main
git add -A
git commit -m "<describe the change>"
git push
```
