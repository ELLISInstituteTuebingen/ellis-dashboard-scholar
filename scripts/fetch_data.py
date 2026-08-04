#!/usr/bin/env python3
"""
fetch_data.py — pulls publication data for ELLIS Institute Tübingen scientists
from OpenAlex, detects collaborations with other ELLIS Units, and writes a
single JSON file the dashboard reads.

Usage:
    python scripts/fetch_data.py

Requires:
    pip install requests
"""
import json
import time
import sys
import html
import re
import os
import unicodedata
from pathlib import Path
from collections import defaultdict

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OUT_PATH = ROOT / "docs" / "data" / "publications.json"

OPENALEX_BASE = "https://api.openalex.org"
# OpenAlex asks for a contact email in the User-Agent as a courtesy (polite pool = faster, more reliable).
HEADERS = {"User-Agent": "ellis-tuebingen-dashboard (mailto:contact@example.org)"}

# Semantic Scholar: use an API key if available (much higher rate limits,
# no more 429s). Falls back to unauthenticated (slow, easily rate-limited)
# if the env var isn't set — set SEMANTIC_SCHOLAR_API_KEY locally or as a
# GitHub Actions repository secret.
_S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")

_SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY")
if not _SERPAPI_KEY:
    print("[warn] SERPAPI_API_KEY not set — Google Scholar h-index data will be skipped.",
          file=sys.stderr)
S2_HEADERS = dict(HEADERS)
if _S2_API_KEY:
    S2_HEADERS["x-api-key"] = _S2_API_KEY
else:
    print("[warn] SEMANTIC_SCHOLAR_API_KEY not set — using unauthenticated Semantic "
          "Scholar requests, which are slow and easily rate-limited.", file=sys.stderr)


def load_config():
    team = json.loads((CONFIG_DIR / "team.json").read_text())
    known_venues_path = CONFIG_DIR / "known_venues.json"
    known_venues = json.loads(known_venues_path.read_text()) if known_venues_path.exists() else {"papers": []}
    members_path = CONFIG_DIR / "ellis_members.json"
    members = json.loads(members_path.read_text()) if members_path.exists() else []
    budget_path = CONFIG_DIR / "budget.json"
    budget = json.loads(budget_path.read_text()) if budget_path.exists() else {"budget_by_year": {}, "partial_years": {}}
    return team, known_venues, members, budget


SERPAPI_QUOTA_EXHAUSTED = False  # set True once we hit a quota error, so we
                                  # stop hammering the API for the rest of
                                  # this run once the free tier is used up


def _serpapi_get(params, name):
    """Shared SerpAPI request helper with retry-on-429 and quota detection.
    Returns the parsed JSON response, or None if the request failed, was
    rate-limited past retries, or the monthly quota is exhausted."""
    global SERPAPI_QUOTA_EXHAUSTED
    if not _SERPAPI_KEY or SERPAPI_QUOTA_EXHAUSTED:
        return None

    full_params = dict(params, api_key=_SERPAPI_KEY)
    for attempt in range(3):
        try:
            resp = requests.get("https://serpapi.com/search", params=full_params, timeout=30)
        except requests.RequestException as e:
            print(f"    [warn] SerpAPI request failed for {name}: {e}", file=sys.stderr)
            return None

        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"    [warn] SerpAPI rate-limited for {name}, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue

        data = resp.json()
        error = data.get("error", "")
        if error:
            if any(kw in error.lower() for kw in ("run out of searches", "monthly", "limit")):
                print("    [warn] SerpAPI quota exhausted — skipping Scholar data for the rest of this run.",
                      file=sys.stderr)
                SERPAPI_QUOTA_EXHAUSTED = True
            else:
                print(f"    [warn] SerpAPI error for {name}: {error}", file=sys.stderr)
            return None
        return data

    return None


def fetch_scholar_h_index(scholar_id, name):
    """Fetches h-index directly from a person's real Google Scholar profile
    via SerpAPI. Google Scholar indexes much more broadly than OpenAlex
    (workshop papers, technical reports, less strict deduplication), so its
    h-index is typically noticeably higher than OpenAlex's for the same
    person — that's expected, not an error."""
    if not scholar_id:
        return None
    data = _serpapi_get({"engine": "google_scholar_author", "author_id": scholar_id}, name)
    if not data:
        return None
    table = data.get("cited_by", {}).get("table", [])
    for row in table:
        if "h_index" in row:
            return row["h_index"].get("all")
    return None


MAX_SCHOLAR_PAGES = 5  # safety cap (≈500 articles) so one very prolific
                        # person can't exhaust the whole monthly quota alone


def fetch_scholar_articles(scholar_id, name, join_year):
    """Pages through a person's Google Scholar publication list (newest
    first) via SerpAPI, stopping as soon as we see an article older than
    their join year — since results are sorted by publication date, every
    subsequent article will also be too old, so we can stop paginating
    early rather than fetching (and paying for) their entire career.
    Capped at MAX_SCHOLAR_PAGES regardless, to bound worst-case API cost
    for very prolific authors."""
    if not scholar_id:
        return []

    articles = []
    start = 0
    page_size = 100
    for page in range(MAX_SCHOLAR_PAGES):
        data = _serpapi_get({
            "engine": "google_scholar_author",
            "author_id": scholar_id,
            "sort": "pubdate",
            "start": start,
            "num": page_size,
        }, name)
        if not data:
            break
        page_articles = data.get("articles", [])
        if not page_articles:
            break
        articles.extend(page_articles)

        oldest_year_on_page = None
        for a in page_articles:
            y = a.get("year")
            if y:
                try:
                    y = int(y)
                    if oldest_year_on_page is None or y < oldest_year_on_page:
                        oldest_year_on_page = y
                except ValueError:
                    pass

        if len(page_articles) < page_size:
            break  # last page
        if join_year and oldest_year_on_page and oldest_year_on_page < join_year:
            break  # everything from here on will be even older — stop early

        start += page_size
        time.sleep(0.3)

    return articles


def simplify_scholar_article(article, scientist_name):
    """Converts a SerpAPI Google Scholar article entry into our internal
    publication shape. Several fields OpenAlex could give us are simply not
    available from Scholar: no DOI, no per-paper institution IDs, no
    open-access flag, and no way to verify per-paper ELLIS affiliation —
    these are known, accepted tradeoffs of using Scholar as the primary
    source instead of OpenAlex."""
    title = article.get("title", "")
    year = None
    if article.get("year"):
        try:
            year = int(article["year"])
        except ValueError:
            year = None
    authors_str = article.get("authors", "") or ""
    authors = [a.strip() for a in authors_str.split(",") if a.strip()]
    venue_str = article.get("publication", "") or ""
    cited_by = (article.get("cited_by") or {}).get("value", 0) or 0

    # Scholar has no stable numeric work ID like OpenAlex — use the
    # citation_id it provides, or fall back to a hash of the title.
    raw_id = article.get("citation_id") or re.sub(r"[^a-z0-9]", "", title.lower())[:60]

    return {
        "id": f"scholar-{raw_id}",
        "title": title,
        "year": year,
        "venue": venue_str,
        "cited_by_count": cited_by,
        "doi": None,  # not available from Google Scholar
        "authors": authors,  # often abbreviated first names — a known Scholar limitation
        "institution_ids": [],  # not available from Google Scholar
        "scientist": scientist_name,
        "confirmed_ellis_affiliation": False,  # can't verify without per-paper institution data
        "venue_category": classify_venue_string(venue_str),
        "is_oa": False,  # not available from Google Scholar
    }


CORE_VENUE_PATTERNS = {
    "ICML": ["international conference on machine learning", "icml"],
    "NeurIPS": ["neural information processing systems", "neurips", "nips.cc"],
    "ICLR": ["international conference on learning representations", "iclr"],
}

# Broader set of other widely-recognized top-tier AI/ML/CV/NLP/Robotics venues,
# tracked separately from the core three so we don't blur that specific stat.
BROADER_VENUE_PATTERNS = {
    "AAAI": ["aaai conference on artificial intelligence"],
    "IJCAI": ["international joint conference on artificial intelligence", "ijcai"],
    "UAI": ["uncertainty in artificial intelligence"],
    "AISTATS": ["artificial intelligence and statistics"],
    "CVPR": ["computer vision and pattern recognition", "cvpr"],
    "ICCV": ["international conference on computer vision", "iccv"],
    "ECCV": ["european conference on computer vision", "eccv"],
    "ACL": ["association for computational linguistics"],
    "EMNLP": ["empirical methods in natural language processing", "emnlp"],
    "NAACL": ["north american chapter of the association for computational linguistics", "naacl"],
    "KDD": ["knowledge discovery and data mining", "kdd"],
    "RSS": ["robotics: science and systems"],
    "CoRL": ["conference on robot learning", "corl"],
    "ICRA": ["international conference on robotics and automation", "icra"],
    "Nature": ["nature"],
}

ALL_VENUE_PATTERNS = {**CORE_VENUE_PATTERNS, **BROADER_VENUE_PATTERNS}

SEMANTIC_SCHOLAR_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"


def classify_venue_string(venue_str):
    """Classify a raw venue string (e.g. from Semantic Scholar) against our
    known top-venue patterns (core 3 + broader set)."""
    if not venue_str:
        return None
    v = venue_str.lower()
    for venue_label, patterns in ALL_VENUE_PATTERNS.items():
        if any(p in v for p in patterns):
            return venue_label
    return None


ARXIV_DOI_PATTERN = re.compile(r"10\.48550/arxiv\.(.+)", re.IGNORECASE)


def fetch_semantic_scholar_venues(publications_by_id):
    """Batch-look-up venue info from Semantic Scholar, which tags ML
    conference papers (NeurIPS/ICML/ICLR/etc.) far more reliably than
    OpenAlex, even when a paper's only OpenAlex location is an arXiv preprint.

    For each publication we try BOTH its DOI and (if the DOI is an
    arXiv-style DOI like 10.48550/arXiv.XXXX) its raw arXiv ID, since
    Semantic Scholar frequently indexes a paper's canonical record under the
    arXiv ID rather than that DOI — looking up by DOI alone silently misses
    a lot of real matches.

    publications_by_id: {work_id: publication_dict}
    Returns {work_id: venue_label_or_None}. Skips silently on any failure —
    this is a bonus enrichment, not something that should crash the whole
    pipeline if Semantic Scholar is down or rate-limits us."""
    id_entries = []  # (external_id_string, work_id)
    for wid, pub in publications_by_id.items():
        doi = pub.get("doi")
        if not doi:
            continue
        stripped = doi.replace("https://doi.org/", "")
        id_entries.append((f"DOI:{stripped}", wid))
        m = ARXIV_DOI_PATTERN.match(stripped)
        if m:
            id_entries.append((f"ARXIV:{m.group(1)}", wid))

    results = {}
    batch_size = 500
    for i in range(0, len(id_entries), batch_size):
        chunk = id_entries[i:i + batch_size]
        ids = [e[0] for e in chunk]
        papers = None
        max_retries = 4
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    SEMANTIC_SCHOLAR_BATCH_URL,
                    params={"fields": "venue,publicationVenue,externalIds"},
                    json={"ids": ids},
                    headers=S2_HEADERS,
                    timeout=30,
                )
                if resp.status_code == 429:
                    wait_s = int(resp.headers.get("Retry-After", 10 * (attempt + 1)))
                    print(f"[warn] Semantic Scholar rate-limited, waiting {wait_s}s before retry "
                          f"({attempt + 1}/{max_retries})...", file=sys.stderr)
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                papers = resp.json()
                break
            except requests.RequestException as e:
                print(f"[warn] Semantic Scholar lookup error: {e}", file=sys.stderr)
                time.sleep(5)

        if papers is None:
            print(f"[warn] Semantic Scholar batch failed after {max_retries} retries, skipping "
                  f"{len(chunk)} lookups.", file=sys.stderr)
            continue

        for (_, wid), paper in zip(chunk, papers):
            if not paper or wid in results:
                continue  # already have a result for this paper from another id
            venue_str = paper.get("venue") or ""
            pub_venue = (paper.get("publicationVenue") or {}).get("name") or ""
            label = classify_venue_string(venue_str) or classify_venue_string(pub_venue)
            if label:
                results[wid] = label
        time.sleep(1.5)  # be politer between batches to avoid tripping the rate limit
    return results


def simplified_pub_is_after_join_date(pub, joined_date_str):
    """True if the publication's year is on/after the scientist's join year.
    Google Scholar typically only gives a year (not a full date) per
    article, so this is necessarily year-granularity, not day-precision —
    a real precision loss compared to what OpenAlex could give us."""
    if not joined_date_str:
        return True
    join_year = int(joined_date_str[:4])
    if pub.get("year") is None:
        return False  # no date info at all -> exclude rather than guess
    return pub["year"] >= join_year


def _normalize_title(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def apply_manual_additions(all_publications, known_papers):
    """Some real papers never make it into all_publications at all — either
    the join-date grace period wasn't wide enough (a paper posted to arXiv
    just before someone's cutoff), or OpenAlex simply hasn't linked the
    paper to that person's author ID yet. known_venues.json entries can't
    fix that on their own (they only edit papers already present). Entries
    with an explicit 'scientist' field are treated as manual insertions —
    if no matching paper already exists, we build a minimal record and add
    it directly, bypassing the normal fetch/filter pipeline entirely."""
    added = 0
    existing_titles = {_normalize_title(p["title"] or "") for p in all_publications.values()}
    for entry in known_papers:
        if not entry.get("scientist"):
            continue  # a pure venue/year override, not a manual insertion
        norm_title = _normalize_title(entry["title"])
        if any(norm_title in et or et in norm_title for et in existing_titles):
            continue  # already present, nothing to add
        synthetic_id = "manual-" + re.sub(r"[^a-z0-9]", "", entry["title"].lower())[:40]
        all_publications[synthetic_id] = {
            "id": synthetic_id,
            "title": entry["title"],
            "year": entry.get("year"),
            "venue": entry.get("venue"),
            "cited_by_count": entry.get("cited_by_count", 0),
            "doi": entry.get("doi"),
            "authors": entry.get("authors", []),
            "institution_ids": [],
            "scientist": entry["scientist"],
            "confirmed_ellis_affiliation": False,
            "venue_category": entry["venue"],
            "is_oa": False,
        }
        existing_titles.add(norm_title)
        added += 1
    return added


def apply_known_venue_overrides(all_publications, known_papers):
    """Manually-curated venue tags ALWAYS win over OpenAlex/Semantic Scholar,
    since conference program pages are more authoritative than automated
    indexing (which frequently never catches up for ML conferences that
    don't publish traditional indexed proceedings). Uses fuzzy title matching
    since pasted program titles are sometimes truncated or lightly reworded.

    Also corrects the paper's 'year' field when the config entry specifies
    the true conference edition (optional 'year' key) — OpenAlex dates a
    paper by its first arXiv posting, which for ML conferences is typically
    4-6 months *before* the actual conference, so without this override a
    paper accepted at e.g. ICLR 2026 often shows up bucketed under 2025 in
    any year-based chart, understating the most recent year's real output."""
    if not known_papers:
        return 0
    norm_known = [
        (entry["title"], _normalize_title(entry["title"]), entry["venue"], entry.get("year"))
        for entry in known_papers
    ]
    overrides = 0
    for pub in all_publications.values():
        norm_pub_title = _normalize_title(pub["title"] or "")
        for orig_title, norm_known_title, venue, year in norm_known:
            if norm_pub_title in norm_known_title or norm_known_title in norm_pub_title:
                changed = False
                if pub["venue_category"] != venue:
                    pub["venue_category"] = venue
                    changed = True
                if year and pub["year"] != year:
                    pub["year"] = year
                    changed = True
                if changed:
                    overrides += 1
                break
    return overrides


def _normalize_name(name):
    """Lowercase, strip accents/periods/hyphens, collapse whitespace —
    for matching co-author names against the ELLIS members roster."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.replace(".", "").replace("-", " ")
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


def build_member_lookup(members, team):
    """Returns {normalized_name: [units]}, skipping our own tracked
    scientists (co-authoring with yourself isn't an external collaboration)."""
    own_names = {_normalize_name(s["name"]) for s in team["scientists"]}
    lookup = {}
    for m in members:
        norm = _normalize_name(m["name"])
        if norm in own_names or not m.get("units"):
            continue
        lookup[norm] = m["units"]
    return lookup


def compute_member_collaborations(all_publications, member_lookup):
    """Cross-checks every co-author name on every tracked publication against
    the real ELLIS Fellows/Scholars/Members roster. Far more precise than
    institution-level matching, since it only counts a genuine named ELLIS
    person, not just anyone at the same university.

    Returns (counts, details):
      counts:  {unit: count}, sorted descending
      details: {unit: [{title, year, scientist, co_author}, ...]}, sorted
               by year descending, for click-to-expand drilldown in the UI.
    """
    unit_counts = defaultdict(int)
    unit_papers = defaultdict(set)
    unit_details = defaultdict(list)

    for pub in all_publications.values():
        hit_units_this_paper = {}  # unit -> first matching co-author name
        for author in pub.get("authors", []):
            units = member_lookup.get(_normalize_name(author))
            if units:
                for u in units:
                    hit_units_this_paper.setdefault(u, author)
        for u, co_author in hit_units_this_paper.items():
            if pub["id"] not in unit_papers[u]:
                unit_papers[u].add(pub["id"])
                unit_counts[u] += 1
                scientist = pub.get("scientist")
                scientist_str = ", ".join(scientist) if isinstance(scientist, list) else scientist
                unit_details[u].append({
                    "title": pub.get("title"),
                    "year": pub.get("year"),
                    "scientist": scientist_str,
                    "co_author": co_author,
                    "doi": pub.get("doi"),
                })

    for u in unit_details:
        unit_details[u].sort(key=lambda p: -(p["year"] or 0))

    counts = dict(sorted(unit_counts.items(), key=lambda kv: -kv[1]))
    details = dict(unit_details)
    return counts, details


def dedupe_by_title(all_publications):
    """OpenAlex sometimes creates two separate work records for the same
    real-world paper (e.g. an arXiv preprint version and a published-venue
    version each get their own work ID). Since our main dedup only checks
    exact OpenAlex work ID, these slip through as if they were different
    papers — inflating publication counts and causing the same paper to
    show up twice in collaboration detail lists. This catches and merges
    them by normalized title, keeping whichever entry was seen first."""
    seen_titles = {}
    removed = 0
    for wid in list(all_publications.keys()):
        title = all_publications[wid].get("title") or ""
        norm = re.sub(r"[^a-z0-9]", "", title.lower())
        if not norm:
            continue
        if norm in seen_titles:
            del all_publications[wid]
            removed += 1
        else:
            seen_titles[norm] = wid
    return removed


def compute_h_index(citation_counts):
    """Standard h-index: the largest h such that at least h papers have at
    least h citations each. Computed over a researcher's FULL career (all
    fetched works, not just papers since joining ELLIS) — h-index is always
    a career-total metric, using only a subset of someone's papers would
    produce a much smaller, non-standard number that misrepresents their
    actual academic standing."""
    counts = sorted(citation_counts, reverse=True)
    h = 0
    for i, c in enumerate(counts, start=1):
        if c >= i:
            h = i
        else:
            break
    return h


def process_activities():
    """Reads config/activities.json (manually maintained — no API exists for
    talks/press/awards the way OpenAlex covers publications) and writes a
    sorted, validated copy to docs/data/activities.json for the PR &
    Activities tab. Deliberately anonymous — no scientist names are read,
    stored, or output, since the tab tracks institute-level recognition,
    not individual attribution."""
    src_path = CONFIG_DIR / "activities.json"
    dest_path = ROOT / "docs" / "data" / "activities.json"
    if not src_path.exists():
        print("    No config/activities.json found — skipping PR & Activities tab data.")
        return

    raw = json.loads(src_path.read_text())
    entries = raw.get("entries", []) if isinstance(raw, dict) else raw

    valid_types = {"talk", "press", "media", "award", "grant", "recognition", "panel", "organizing", "podcast", "startup"}
    cleaned = []
    for e in entries:
        if e.get("type") not in valid_types:
            print(f"    [warn] Skipping activity with unrecognized type '{e.get('type')}': {e.get('title')}")
            continue
        cleaned.append({
            "type": e["type"],
            "title": e.get("title", ""),
            "date": e.get("date", ""),
            "venue": e.get("venue", ""),
            "url": e.get("url", ""),
            "description": e.get("description", ""),
        })

    cleaned.sort(key=lambda e: e.get("date") or "", reverse=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(json.dumps({"entries": cleaned}, indent=2, ensure_ascii=False))
    print(f"    Wrote {dest_path} with {len(cleaned)} PR & Activities entries.")


def main():
    team, known_venues, members, budget_cfg = load_config()

    if not _SERPAPI_KEY:
        print("[error] SERPAPI_API_KEY is not set — cannot fetch any publication "
              "data, since Google Scholar (via SerpAPI) is now the sole data "
              "source. Set it locally and as a GitHub Actions secret.", file=sys.stderr)

    all_publications = {}  # keyed by our own synthetic id, to dedupe
    per_scientist_counts = defaultdict(int)
    year_counts = defaultdict(int)

    # Make sure every tracked scientist shows up even with zero matched papers.
    for scientist in team["scientists"]:
        per_scientist_counts[scientist["name"]] = 0

    h_index_values = []  # anonymized list, no names attached — for the plot

    for scientist in team["scientists"]:
        scholar_id = scientist.get("google_scholar_id")
        if not scholar_id:
            print(f"[skip] {scientist['name']} has no google_scholar_id configured "
                  f"— look up their profile at scholar.google.com and add the "
                  f"user=XXXXXXXX ID to config/team.json.", file=sys.stderr)
            continue

        joined_date = scientist.get("joined_date")
        join_year = int(joined_date[:4]) if joined_date else None

        # h-index comes directly from Scholar's own profile summary (one
        # API call, independent of the paginated article list below).
        scholar_h_index = fetch_scholar_h_index(scholar_id, scientist["name"])
        if scholar_h_index is not None:
            h_index_values.append(scholar_h_index)
            print(f"Fetching Google Scholar data for {scientist['name']} ({scholar_id})...")
            print(f"    Google Scholar h-index: {scholar_h_index}")
        else:
            h_index_values.append(0)
            print(f"Fetching Google Scholar data for {scientist['name']} ({scholar_id})...")
            print(f"    [warn] No h-index available (quota exhausted, API key missing, "
                  f"or profile fetch failed) — recorded as 0.")

        articles = fetch_scholar_articles(scholar_id, scientist["name"], join_year)
        before_count = len(articles)

        simplified_all = [simplify_scholar_article(a, scientist["name"]) for a in articles]
        kept = [p for p in simplified_all if simplified_pub_is_after_join_date(p, joined_date)]
        print(f"    kept {len(kept)} of {before_count} after join-date filter (since {joined_date})")

        for simplified in kept:
            wid = simplified["id"]
            if wid not in all_publications:
                all_publications[wid] = simplified
                if simplified["year"]:
                    year_counts[simplified["year"]] += 1
            else:
                # already seen via another scientist -> track as internal collaboration
                existing = all_publications[wid]["scientist"]
                if isinstance(existing, str):
                    all_publications[wid]["scientist"] = [existing]
                if scientist["name"] not in all_publications[wid]["scientist"]:
                    all_publications[wid]["scientist"].append(scientist["name"])

            per_scientist_counts[scientist["name"]] += 1

    removed_dupes = dedupe_by_title(all_publications)
    print(f"    Removed {removed_dupes} duplicate Google Scholar records for the same paper (matched by title)")

    manual_count = apply_manual_additions(all_publications, known_venues.get("papers", []))
    print(f"    Manually added {manual_count} papers the normal fetch pipeline missed")

    override_count = apply_known_venue_overrides(all_publications, known_venues.get("papers", []))
    print(f"    Applied {override_count} manual venue overrides from config/known_venues.json")

    member_lookup = build_member_lookup(members, team)
    member_collaborations, member_collaboration_details = compute_member_collaborations(all_publications, member_lookup)

    top_venues_of_interest = ["NeurIPS", "ICML", "ICLR", "Nature"]
    top_venues_by_year = defaultdict(lambda: defaultdict(int))
    for pub in all_publications.values():
        cat = pub.get("venue_category")
        year = pub.get("year")
        if cat in top_venues_of_interest and year:
            top_venues_by_year[str(year)][cat] += 1
    top_venues_by_year = {y: dict(v) for y, v in sorted(top_venues_by_year.items())}
    print(f"    Found real-member collaborations across {len(member_collaborations)} ELLIS Sites "
          f"(checked against {len(member_lookup)} named roster entries)")

    # Recompute venue tallies from final (possibly Semantic-Scholar-upgraded) categories.
    all_venue_counts = defaultdict(int)
    for pub in all_publications.values():
        if pub["venue_category"]:
            all_venue_counts[pub["venue_category"]] += 1

    broader_only_counts = {
        k: v for k, v in all_venue_counts.items() if k in BROADER_VENUE_PATTERNS
    }
    top_tier_total = sum(all_venue_counts.values())  # core 3 + broader set combined

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "institute": team["institute"]["name"],
        "total_publications": len(all_publications),
        "confirmed_affiliation_count": sum(
            1 for p in all_publications.values() if p.get("confirmed_ellis_affiliation")
        ),
        "open_access_count": sum(1 for p in all_publications.values() if p.get("is_oa")),
        "open_access_percent": round(
            100 * sum(1 for p in all_publications.values() if p.get("is_oa")) / max(1, len(all_publications)), 1
        ),
        "scientist_join_dates": sorted(
            s["joined_date"] for s in team["scientists"] if s.get("joined_date")
        ),
        "pi_headcount_by_year": {
            year: sum(
                1 for s in team["scientists"]
                if s.get("joined_date") and s["joined_date"] <= f"{year}-12-31"
            )
            for year in budget_cfg.get("budget_by_year", {}).keys()
        },
        "publications": sorted(
            all_publications.values(), key=lambda p: (p["year"] or 0), reverse=True
        ),
        "per_scientist_counts": dict(per_scientist_counts),
        "publications_per_year": dict(sorted(year_counts.items())),
        "top_venues_by_year": top_venues_by_year,
        "ellis_member_collaborations": member_collaborations,
        "ellis_member_collaboration_details": member_collaboration_details,
        "h_index_distribution": sorted(h_index_values),
        "budget_by_year": budget_cfg.get("budget_by_year", {}),
        "budget_partial_years": budget_cfg.get("partial_years", {}),
        "venue_counts": {v: all_venue_counts.get(v, 0) for v in CORE_VENUE_PATTERNS},
        "broader_venue_counts": dict(
            sorted(broader_only_counts.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "top_tier_total_count": top_tier_total,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUT_PATH} with {output['total_publications']} publications.")

    print("Processing PR & Activities data...")
    process_activities()


if __name__ == "__main__":
    main()
