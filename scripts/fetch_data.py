#!/usr/bin/env python3
"""
fetch_data.py — pulls publication data for ELLIS Institute Tübingen scientists
from Google Scholar (via SerpAPI) as the primary source, enriches open-access
status / DOIs from OpenAlex on a best-effort per-title basis, detects
collaborations with other ELLIS Units, and writes a single JSON file the
dashboard reads.

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


_CONSECUTIVE_FULL_FAILURES = 0  # tracks repeated total-retry-exhaustion across
                                 # DIFFERENT calls — if this keeps happening,
                                 # it's a real quota limit, not a momentary blip


def _serpapi_get(params, name):
    """Shared SerpAPI request helper with retry-on-429 and quota detection.
    Returns the parsed JSON response, or None if the request failed, was
    rate-limited past retries, or the monthly quota is exhausted."""
    global SERPAPI_QUOTA_EXHAUSTED, _CONSECUTIVE_FULL_FAILURES
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
            # The 429 response body sometimes contains the actual quota
            # message — check it before assuming this is just a momentary
            # rate limit worth retrying.
            try:
                body_error = resp.json().get("error", "")
            except ValueError:
                body_error = ""
            if any(kw in body_error.lower() for kw in ("run out of searches", "monthly", "limit", "quota")):
                print("    [warn] SerpAPI quota exhausted — skipping Scholar data for the rest of this run.",
                      file=sys.stderr)
                SERPAPI_QUOTA_EXHAUSTED = True
                return None

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
        _CONSECUTIVE_FULL_FAILURES = 0  # a real success resets the counter
        return data

    # Exhausted all 3 retries without success. If this keeps happening
    # across different calls (not just one flaky request), it's almost
    # certainly the monthly quota, not a passing rate-limit blip — stop
    # entirely rather than burning the same futile retry cycle on every
    # single remaining paper.
    _CONSECUTIVE_FULL_FAILURES += 1
    if _CONSECUTIVE_FULL_FAILURES >= 2:
        print("    [warn] Repeated rate-limiting across multiple requests — treating as quota "
              "exhaustion and stopping Scholar calls for the rest of this run.", file=sys.stderr)
        SERPAPI_QUOTA_EXHAUSTED = True
    return None


def fetch_precise_publication_date(scholar_id, citation_id, name):
    """Fetches the exact publication date (often day-precision, e.g.
    '2023/2/23') for a single paper via SerpAPI's citation-detail view.
    Only called for papers dated in a person's exact join year, where
    day-precision determines whether they should actually count — costs
    one extra API call per paper checked, so used sparingly rather than
    for every single paper."""
    if not scholar_id or not citation_id:
        return None
    data = _serpapi_get({
        "engine": "google_scholar_author",
        "author_id": scholar_id,
        "view_op": "view_citation",
        "citation_id": citation_id,
    }, name)
    if not data:
        return None
    date_str = (data.get("citation") or {}).get("publication_date")
    if not date_str:
        return None
    parts = date_str.split("/")
    try:
        from datetime import date
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def fetch_scholar_profile_stats(scholar_id, name):
    """Fetches h-index AND citations-received-per-year from a person's real
    Google Scholar profile, in a single API call (Google Scholar indexes
    much more broadly than OpenAlex, so h-index is typically noticeably
    higher than OpenAlex's for the same person — that's expected, not an
    error). Returns (h_index, citation_history) where citation_history is a
    list of {"year": Y, "citations": C} — citations RECEIVED that year
    across their full career, not cumulative and not limited to their time
    at the Institute."""
    if not scholar_id:
        return None, []
    data = _serpapi_get({"engine": "google_scholar_author", "author_id": scholar_id}, name)
    if not data:
        return None, []
    h_index = None
    for row in data.get("cited_by", {}).get("table", []):
        if "h_index" in row:
            h_index = row["h_index"].get("all")
            break
    citation_history = data.get("cited_by", {}).get("graph", [])
    return h_index, citation_history


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
        "citation_id": article.get("citation_id"),  # internal use only, for precise-date lookups
        "authors": authors,  # often abbreviated first names — a known Scholar limitation
        "institution_ids": [],  # not available from Google Scholar
        "scientist": scientist_name,
        "confirmed_ellis_affiliation": False,  # can't verify without per-paper institution data
        "venue_category": classify_venue_string(venue_str),
        "is_oa": False,  # not available from Google Scholar; may be filled by OpenAlex enrichment
        "oa_url": None,  # direct free-PDF URL, filled by OpenAlex enrichment when the paper is open access
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
            "oa_url": None,
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


def _split_initials_and_last_name(normalized_name):
    """Splits a normalized name into (initials, last_name). Works for both
    full names ('andreas krause' -> ('a', 'krause')) and Google Scholar's
    abbreviated author format ('a krause' -> ('a', 'krause'), or
    'tk buening' -> ('tk', 'buening')) — both reduce to the same shape once
    every given-name token is collapsed to its first letter."""
    tokens = normalized_name.split()
    if len(tokens) < 2:
        return "", normalized_name
    last_name = tokens[-1]
    initials = "".join(t[0] for t in tokens[:-1] if t)
    return initials, last_name


def build_member_lookup(members, team):
    """Returns (exact_lookup, fuzzy_lookup):
      exact_lookup: {normalized_full_name: [units]}
      fuzzy_lookup: {last_name: [(initials, full_name, units), ...]}
    Both skip our own tracked scientists (co-authoring with yourself isn't
    an external collaboration). The fuzzy index exists because Google
    Scholar abbreviates author first names to initials (e.g. 'A Krause'
    instead of 'Andreas Krause'), which would never match the exact-name
    lookup OpenAlex-based matching relied on."""
    own_names = {_normalize_name(s["name"]) for s in team["scientists"]}
    exact_lookup = {}
    fuzzy_lookup = defaultdict(list)
    for m in members:
        norm = _normalize_name(m["name"])
        if norm in own_names or not m.get("units"):
            continue
        exact_lookup[norm] = m["units"]
        initials, last_name = _split_initials_and_last_name(norm)
        if last_name:
            fuzzy_lookup[last_name].append((initials, norm, m["units"]))
    return exact_lookup, dict(fuzzy_lookup)


def _fuzzy_match_author(author_norm, fuzzy_lookup):
    """Tries to match an abbreviated Scholar author name ('a krause') against
    the roster via last-name + initials-compatibility. Only returns a match
    if exactly one roster candidate is plausible — if two different people
    share both a last name AND compatible initials, we skip rather than
    risk crediting the wrong person."""
    author_initials, author_last = _split_initials_and_last_name(author_norm)
    if not author_last:
        return None
    candidates = fuzzy_lookup.get(author_last, [])
    if not candidates:
        return None

    def initials_compatible(a, b):
        if not a or not b:
            return True  # missing initials on either side — don't rule it out
        return a.startswith(b) or b.startswith(a)

    matches = [(full_name, units) for (cand_initials, full_name, units) in candidates
               if initials_compatible(author_initials, cand_initials)]
    if len(matches) == 1:
        return matches[0]
    return None  # zero or ambiguous multiple matches — don't guess


def compute_member_collaborations(all_publications, exact_lookup, fuzzy_lookup):
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
            author_norm = _normalize_name(author)
            units = exact_lookup.get(author_norm)
            matched_name = author
            if not units:
                fuzzy_result = _fuzzy_match_author(author_norm, fuzzy_lookup)
                if fuzzy_result:
                    matched_name_norm, units = fuzzy_result
                    matched_name = f"{author} (matched: {matched_name_norm})"
            if units:
                for u in units:
                    hit_units_this_paper.setdefault(u, matched_name)
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


def _merge_scientist_credit(keep_pub, dupe_pub):
    """Before discarding a duplicate entry, merge its scientist attribution
    into the surviving one — otherwise a paper genuinely co-authored by two
    tracked PIs would silently lose credit for whichever one's copy got
    deleted, even though the overall publication count is correctly
    deduplicated."""
    existing = keep_pub.get("scientist")
    existing_list = existing if isinstance(existing, list) else [existing] if existing else []
    incoming = dupe_pub.get("scientist")
    incoming_list = incoming if isinstance(incoming, list) else [incoming] if incoming else []
    merged = list(dict.fromkeys(existing_list + incoming_list))  # preserve order, drop exact dupes
    keep_pub["scientist"] = merged[0] if len(merged) == 1 else merged
    # Prefer whichever record has richer citation data, since Scholar's
    # per-profile citation counts for the same paper can differ slightly.
    if (dupe_pub.get("cited_by_count") or 0) > (keep_pub.get("cited_by_count") or 0):
        keep_pub["cited_by_count"] = dupe_pub["cited_by_count"]


def _venue_rank(pub):
    """Preference order when the same paper turns up under more than one
    venue: a published top-tier / categorised venue (2) beats another named
    venue (1), which beats an arXiv/preprint or blank venue (0). Used so that
    when a preprint and its published version are merged, the PUBLISHED venue
    is the label that survives."""
    if pub.get("venue_category"):
        return 2
    v = (pub.get("venue") or "").lower()
    if not v.strip() or "arxiv" in v or "biorxiv" in v or "medrxiv" in v:
        return 0
    return 1


def _resolve_duplicate(all_publications, kept_wid, dupe_wid):
    """Two records represent the same real-world paper. Keep whichever has the
    better (more published) venue, fold the other's scientist credit and
    richer citation count into it, delete the loser, and return the surviving
    id. This is what ensures a preprint that also appears under its published
    top-tier venue collapses onto the published record, not the arXiv one."""
    kept, dupe = all_publications[kept_wid], all_publications[dupe_wid]
    if _venue_rank(dupe) > _venue_rank(kept):
        winner_wid, loser_wid = dupe_wid, kept_wid
    else:
        winner_wid, loser_wid = kept_wid, dupe_wid
    _merge_scientist_credit(all_publications[winner_wid], all_publications[loser_wid])
    del all_publications[loser_wid]
    return winner_wid


def dedupe_by_title(all_publications):
    """Google Scholar sometimes indexes the same real-world paper twice under
    slightly different titles — once per co-author's profile, occasionally
    with words reordered or lightly reworded (e.g. 'X is Required for Y' vs
    'Is X Required for Y?'). Two passes: first an exact-substring match
    (catches near-identical titles), then a same-word-set match (catches
    reordered/rephrased duplicates the first pass misses). When a match is
    found the records are merged, keeping the one with the more-published
    venue (see _resolve_duplicate) and carrying scientist attribution across
    so co-authorship credit isn't silently lost."""
    seen_titles = {}
    removed = 0
    for wid in list(all_publications.keys()):
        title = all_publications[wid].get("title") or ""
        norm = re.sub(r"[^a-z0-9]", "", title.lower())
        if not norm:
            continue
        if norm in seen_titles:
            seen_titles[norm] = _resolve_duplicate(all_publications, seen_titles[norm], wid)
            removed += 1
        else:
            seen_titles[norm] = wid

    seen_word_sets = {}
    for wid in list(all_publications.keys()):
        title = all_publications[wid].get("title") or ""
        words = frozenset(re.sub(r"[^a-z0-9 ]", "", title.lower()).split())
        if not words:
            continue
        if words in seen_word_sets:
            seen_word_sets[words] = _resolve_duplicate(all_publications, seen_word_sets[words], wid)
            removed += 1
        else:
            seen_word_sets[words] = wid

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


def warn_on_future_dated(all_publications):
    """Flag any paper dated beyond the current year. Google Scholar sometimes
    indexes a paper under a future year (a late-year NeurIPS paper shown as
    next year's edition, or an arXiv id mis-parsed as a future date) — this
    has produced a real, confirmed mis-dating on this dashboard before.

    This only WARNS in the run log; it deliberately does NOT drop or alter
    anything, since a genuinely future-dated preprint is legitimate. The point
    is to put a human's eyes on suspicious dates rather than silently trust
    them."""
    import datetime
    current_year = datetime.datetime.utcnow().year
    suspects = [p for p in all_publications.values()
                if p.get("year") and int(p["year"]) > current_year]
    if suspects:
        print(f"    [warn] {len(suspects)} publication(s) dated beyond {current_year} — "
              f"possible Google Scholar mis-dating, worth a manual check:", file=sys.stderr)
        for p in sorted(suspects, key=lambda p: p["year"], reverse=True)[:20]:
            print(f"            {p['year']}  {p['title'][:72]}", file=sys.stderr)
    else:
        print(f"    Date sanity check: no publications dated beyond {current_year}.")


OPENALEX_WORKS_URL = f"{OPENALEX_BASE}/works"


def _titles_match(a, b):
    """Strong title match: normalized equality, or ≥0.9 token-set Jaccard.
    Kept deliberately strict so we never attach the wrong paper's metadata."""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.9


def enrich_open_access(all_publications):
    """Best-effort open-access + DOI enrichment via OpenAlex title search.

    Google Scholar gives us no DOI and no open-access flag, so both fields are
    otherwise always empty. Here we look each paper up in OpenAlex by title
    (relevance-ranked search), accept the top hit ONLY on a strong title match
    — and, when both years are known, a matching year — then copy over its DOI
    and open-access status.

    Deliberately conservative: an ambiguous or weak match is left untouched
    (is_oa stays False, doi stays None) rather than risk mislabeling a paper.
    It uses OpenAlex purely as a per-title lookup, so it does NOT reintroduce
    the author-profile fragmentation problem that made author-ID-based OpenAlex
    fetching unreliable.

    Fully defensive: every request is wrapped, so network errors / rate limits
    / schema changes degrade to "no enrichment" and can never crash the run.
    Enrichment only ADDS doi/is_oa — it never removes, reorders, or filters the
    publication set. Set DISABLE_OA_ENRICHMENT in the environment to skip it."""
    if os.environ.get("DISABLE_OA_ENRICHMENT"):
        print("    [skip] Open-access enrichment disabled via DISABLE_OA_ENRICHMENT.")
        return

    pubs = list(all_publications.values())
    resolved = 0   # strong title match found
    oa_true = 0    # of those, confirmed open access
    errors = 0
    print(f"    Enriching open-access status from OpenAlex for {len(pubs)} papers "
          f"(best-effort, strong-title-match only)...")

    for i, pub in enumerate(pubs):
        title = (pub.get("title") or "").strip()
        if not title:
            continue
        try:
            resp = requests.get(
                OPENALEX_WORKS_URL,
                params={
                    "search": title,
                    "per_page": 1,
                    "select": "title,publication_year,doi,open_access",
                },
                headers=HEADERS,
                timeout=20,
            )
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as e:  # best-effort: never fatal
            errors += 1
            if errors <= 3 or errors % 50 == 0:
                print(f"    [warn] OpenAlex lookup issue (continuing): {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        if not results:
            time.sleep(0.1)
            continue

        cand = results[0]
        if not _titles_match(title, cand.get("title") or ""):
            time.sleep(0.1)
            continue

        py = cand.get("publication_year")
        if pub.get("year") and py and abs(int(pub["year"]) - int(py)) > 1:
            time.sleep(0.1)  # top hit is a different edition/paper — skip
            continue

        resolved += 1
        oa = cand.get("open_access") or {}
        pub["is_oa"] = bool(oa.get("is_oa"))
        if oa.get("oa_url"):
            pub["oa_url"] = oa["oa_url"]  # direct free-PDF link
        if not pub.get("doi") and cand.get("doi"):
            pub["doi"] = cand["doi"]
        if pub["is_oa"]:
            oa_true += 1
        time.sleep(0.1)  # polite pacing for the OpenAlex polite pool

    print(f"    Open-access enrichment: matched {resolved} of {len(pubs)} papers in OpenAlex, "
          f"{oa_true} confirmed open access"
          + (f" ({errors} lookups skipped on errors)" if errors else ""))


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
    citation_history_by_person = []  # anonymized list of per-year citation histories

    for scientist in team["scientists"]:
        scholar_id = scientist.get("google_scholar_id")
        if not scholar_id:
            print(f"[skip] {scientist['name']} has no google_scholar_id configured "
                  f"— look up their profile at scholar.google.com and add the "
                  f"user=XXXXXXXX ID to config/team.json.", file=sys.stderr)
            continue

        joined_date = scientist.get("joined_date")
        join_year = int(joined_date[:4]) if joined_date else None

        # h-index and citation history both come from Scholar's own profile
        # summary — a single API call, independent of the paginated article
        # list below.
        scholar_h_index, citation_history = fetch_scholar_profile_stats(scholar_id, scientist["name"])
        if scholar_h_index is not None:
            h_index_values.append(scholar_h_index)
            print(f"Fetching Google Scholar data for {scientist['name']} ({scholar_id})...")
            print(f"    Google Scholar h-index: {scholar_h_index}")
        else:
            h_index_values.append(0)
            print(f"Fetching Google Scholar data for {scientist['name']} ({scholar_id})...")
            print(f"    [warn] No h-index available (quota exhausted, API key missing, "
                  f"or profile fetch failed) — recorded as 0.")

        if citation_history:
            filtered_history = [pt for pt in citation_history if join_year and pt.get("year", 0) >= join_year]
            if filtered_history:
                citation_history_by_person.append(filtered_history)

        articles = fetch_scholar_articles(scholar_id, scientist["name"], join_year)
        before_count = len(articles)

        simplified_all = [simplify_scholar_article(a, scientist["name"]) for a in articles]
        kept = [p for p in simplified_all if simplified_pub_is_after_join_date(p, joined_date)]
        print(f"    kept {len(kept)} of {before_count} after join-date filter (since {joined_date})")

        # Precise day-level check — only for papers dated in the person's
        # EXACT join year, since those are the only ones where whole-year
        # granularity could be wrong (a paper from months before they
        # joined, in the same calendar year). Costs one extra API call per
        # such paper, so deliberately NOT done for every paper.
        if join_year and joined_date:
            from datetime import date as _date
            join_date_obj = _date(*(int(x) for x in joined_date.split("-")))
            borderline_count = sum(1 for p in kept if p.get("year") == join_year)
            if borderline_count:
                print(f"    Checking precise dates for {borderline_count} paper(s) dated exactly {join_year} "
                      f"(the only ones whole-year filtering can't resolve on its own)...")
            excluded_precise = 0
            still_kept = []
            for p in kept:
                if p.get("year") != join_year:
                    still_kept.append(p)
                    continue
                precise_date = fetch_precise_publication_date(scholar_id, p.get("citation_id"), scientist["name"])
                if precise_date is None:
                    still_kept.append(p)  # fail open — don't exclude on an API hiccup
                elif precise_date >= join_date_obj:
                    still_kept.append(p)
                else:
                    excluded_precise += 1
            kept = still_kept
            if excluded_precise:
                print(f"    Excluded {excluded_precise} paper(s) after precise-date check "
                      f"(published before the actual join date, same calendar year)")

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

    # Manual paper insertion (apply_manual_additions) is intentionally NOT
    # used on this dashboard. That mechanism was built for the OpenAlex-based
    # dashboard to patch specific fetch gaps, but it bypasses the
    # day-precision join-date verification this pipeline otherwise relies on
    # — which caused a real, confirmed error (a manually-inserted paper for
    # one PI turned out to predate their actual join date by several
    # months). Every paper shown here goes through the same verified
    # fetch + precise-date-check pipeline, with no manual exceptions.

    override_count = apply_known_venue_overrides(all_publications, known_venues.get("papers", []))
    print(f"    Applied {override_count} manual venue overrides from config/known_venues.json")

    # Flag any suspiciously future-dated papers (a known Google Scholar
    # indexing artifact) — warning only, changes nothing.
    warn_on_future_dated(all_publications)

    # Best-effort open-access / DOI enrichment via OpenAlex. Wrapped as an
    # extra safety net on top of the function's own per-request guards, so
    # even an unexpected failure here can't abort the run or lose data.
    try:
        enrich_open_access(all_publications)
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] Open-access enrichment step failed entirely, continuing without it: {e}",
              file=sys.stderr)

    exact_lookup, fuzzy_lookup = build_member_lookup(members, team)
    member_collaborations, member_collaboration_details = compute_member_collaborations(all_publications, exact_lookup, fuzzy_lookup)

    top_venues_of_interest = ["NeurIPS", "ICML", "ICLR", "Nature"]
    top_venues_by_year = defaultdict(lambda: defaultdict(int))
    for pub in all_publications.values():
        cat = pub.get("venue_category")
        year = pub.get("year")
        if cat in top_venues_of_interest and year:
            top_venues_by_year[str(year)][cat] += 1
    top_venues_by_year = {y: dict(v) for y, v in sorted(top_venues_by_year.items())}
    print(f"    Found real-member collaborations across {len(member_collaborations)} ELLIS Sites "
          f"(checked against {len(exact_lookup)} named roster entries)")

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
        "citation_history_by_person": citation_history_by_person,
        "budget_by_year": budget_cfg.get("budget_by_year", {}),
        "budget_partial_years": budget_cfg.get("partial_years", {}),
        "venue_counts": {v: all_venue_counts.get(v, 0) for v in CORE_VENUE_PATTERNS},
        "broader_venue_counts": dict(
            sorted(broader_only_counts.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "top_tier_total_count": top_tier_total,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Safety net: if the SerpAPI quota ran out early in this run, we'd
    # otherwise silently overwrite good existing data with a mostly-empty
    # result. Refuse to write if the new count looks suspiciously low
    # compared to what's already there.
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            existing_count = existing.get("total_publications", 0)
            new_count = output["total_publications"]
            if existing_count > 20 and new_count < existing_count * 0.5:
                print(f"[error] New publication count ({new_count}) is less than half of the "
                      f"existing count ({existing_count}) — this looks like a quota-exhausted, "
                      f"degraded run. Refusing to overwrite good data. Re-run once your SerpAPI "
                      f"quota has reset.", file=sys.stderr)
                sys.exit(1)
        except (json.JSONDecodeError, KeyError):
            pass  # existing file unreadable — proceed and write fresh

    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"Wrote {OUT_PATH} with {output['total_publications']} publications.")

    print("Processing PR & Activities data...")
    process_activities()


if __name__ == "__main__":
    main()
