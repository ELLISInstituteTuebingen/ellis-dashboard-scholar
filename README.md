# ELLIS Institute Tübingen — Dashboard (Google Scholar Edition)

**Status as of August 2026: fully live and running weekly automation.**

This is an **experimental**, separate version of the main ELLIS Institute Tübingen research dashboard, built to use **Google Scholar (via the SerpAPI service)** as its data source instead of OpenAlex. It was created as a side-by-side alternative so the original, stable OpenAlex-based dashboard would never be put at risk while testing this approach.

The original dashboard lives at: https://github.com/ELLISInstituteTuebingen/ellis-dashboard

## What this dashboard does

Tracks publications, citation counts, h-index, and collaboration data for the Institute's Principal Investigators and project leaders, pulling everything directly from each person's real Google Scholar profile.

## Why a separate repo instead of just switching the original

Google Scholar and OpenAlex are fundamentally different data sources with different tradeoffs (see below). Keeping them as two separate dashboards means:
- The original, proven OpenAlex-based dashboard is never at risk
- Both can be compared side by side
- Either can be fully abandoned without affecting the other

## Known limitations vs. the OpenAlex-based dashboard

- **No DOIs, no per-paper institution IDs, no open-access flag** — Google Scholar simply doesn't expose this data the way OpenAlex does.
- **Broader, messier indexing** — Scholar counts workshop papers, technical reports, and preprints more liberally, and doesn't always cleanly deduplicate a preprint from its later published version. Expect meaningfully higher publication counts than the OpenAlex dashboard for the same people.
- **Year-level date precision by default** — the bulk article list Google Scholar provides only includes a publication *year*, not a full date. To work around this, the pipeline does an extra **day-precision check**, but *only* for papers dated in a person's exact join year (the only ones where year-level filtering could be wrong) — see "Precise date checking" below.
- **Institution-based ELLIS Site collaboration detection is gone** — the real-member-name-based collaboration matching (cross-checked against the official ELLIS roster) still works fine, since it only needs co-author names.

## SerpAPI quota — the main practical constraint

Every API call (per-person h-index, per-person article-list pages, and each individual precise-date check) counts as one "search" against your SerpAPI plan.

**Confirmed real cost per full run, for all 16 tracked people: ~330 searches.**

Currently on SerpAPI's **Developer plan** ($75/month, 5,000 searches/month), which comfortably supports:
- **Weekly automated runs**: ~330 × ~4.3 weeks ≈ 1,420 searches/month (~28% of quota)
- Plenty of headroom left over for ad-hoc manual runs, testing, or adding new PIs mid-month

**This adds up to ~330 searches for one complete run.** Because of this:
- The GitHub Actions workflow is scheduled **weekly** (every Monday, `.github/workflows/update-dashboard.yml`)
- It does **not** auto-trigger on every config/code push (unlike the original dashboard) — trigger it manually from the Actions tab when you want an extra ad-hoc refresh
- If usage patterns change (e.g. more tracked people, more join-year-boundary papers), re-check actual quota usage in the SerpAPI dashboard and adjust the schedule/plan accordingly

If the quota runs out **mid-run**, the pipeline is built to fail safely:
- Repeated rate-limiting is detected and treated as quota exhaustion, stopping further calls rather than retrying every remaining paper forever
- A safety check refuses to overwrite good existing data with an obviously-degraded result (fewer than half as many publications as before) — the run will simply fail loudly (visible as a red ❌ in the Actions tab) instead of silently corrupting the live dashboard

## Precise date checking

For each person, papers dated in their *exact join year* get one extra API call each, fetching the real day-level publication date (Google Scholar does store this — it's just not in the bulk list). Anything from a clearly later year is trusted as-is; anything from a clearly earlier year is already excluded. Only the ambiguous "same calendar year" papers get the extra check.

## Repository structure

```
config/
  team.json              — tracked PIs, their Google Scholar profile IDs and join dates
  known_venues.json       — manually curated venue/year overrides (shared concept with the OpenAlex dashboard)
  ellis_members.json      — official ELLIS Fellows/Scholars/Members roster, for collaboration matching
  budget.json              — institute budget by year
  activities.json          — PR & Activities entries (talks, press, awards — anonymized on the public dashboard)
scripts/
  fetch_data.py            — the whole pipeline: fetches from Google Scholar, applies overrides, writes docs/data/*.json
docs/
  index.html, style.css, dashboard.js — the dashboard itself (same visual design as the original)
  data/                    — generated output (publications.json, activities.json) — do not hand-edit
.github/workflows/
  update-dashboard.yml     — scheduled + manual GitHub Actions automation
```

## Running it locally

```
export SERPAPI_API_KEY="your_key_here"
python3 scripts/fetch_data.py
```

Then commit and push `docs/data/*.json` as usual.

## Current known state (August 2026)

The live data reflects a complete, clean run: **615 publications** across all 16 tracked people, with precise-date refinement successfully applied to every join-year-boundary paper (no rate-limiting or quota issues). Weekly automation is enabled and running normally.
Deploy retry marker
Testing after fixing visibility and Pages source
Testing after full Pages reset
