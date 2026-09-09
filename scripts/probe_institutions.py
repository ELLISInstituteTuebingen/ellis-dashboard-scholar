#!/usr/bin/env python3
"""
probe_institutions.py  (v8 — cheaper title route + empty-preprint rescue)

READ-ONLY probe. Does NOT modify the pipeline or publications.json.

Changes from v7, all aimed at resolving MORE papers safely and cheaply:

  * CHEAPER title lookups. v7 used full-text search= ($0.001/call). v8 uses the
    title.search FILTER ($0.0001/call — 10x cheaper), so a full run costs ~$0.06
    instead of ~$0.60 and fits a nearly-spent daily budget. The strict title +
    year check still validates every candidate, so match quality is unchanged.

  * PUBLISHED-TWIN RESCUE. Many arXiv records carry no affiliations. For those,
    v8 pulls extra title candidates (per_page 25), reads each record's 'type',
    and prefers the richest PUBLISHED ('article') twin over the empty preprint —
    recovering institutions the preprint lacked. Reports how many it rescued.

  * RETRY SWEEP. Any lookup that hard-fails (transient 429/network) is retried
    once more at the end instead of being silently lost (v7 lost ~39 this way).

  * Free singleton DOI/arXiv lookups, author cap, and the budget readout are
    unchanged from v7.

Setup: needs OPENALEX_API_KEY in your environment (see prior version / your ~/.zshrc).

Usage (from repo root):
    python3 scripts/probe_institutions.py --all
    python3 scripts/probe_institutions.py --all --author-cap 25
    python3 scripts/probe_institutions.py --limit 120
"""

import argparse
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

OPENALEX = "https://api.openalex.org"
WORKS_URL = f"{OPENALEX}/works"
RATELIMIT_URL = f"{OPENALEX}/rate-limit"
SELECT = "title,publication_year,doi,type,authorships"
MIN_INTERVAL = 0.04
WORKERS = 6

API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()
HEADERS = {"User-Agent": "ellis-tuebingen-dashboard (mailto:contact@example.org)"}

OWN_INSTITUTION_IDS = {"I4405263145"}  # ELLIS Institute Tübingen (OpenAlex)
OWN_INSTITUTION_NAME_HINTS = ("ellis institute", "ellis institut")

ARXIV_IN_VENUE = re.compile(r"arxiv:\s*([0-9]{4}\.[0-9]{4,5})", re.I)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PUBS = SCRIPT_DIR.parent / "docs" / "data" / "publications.json"
DEFAULT_REPORT = SCRIPT_DIR.parent / "docs" / "data" / "institution_probe_report.json"

_throttle_lock = threading.Lock()
_fail_lock = threading.Lock()
_last_req = [0.0]
_failures = [0]
_done = [0]


def _normalize_title(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _titles_match(a, b):
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.9


def _year_ok(pub, cand):
    py = cand.get("publication_year")
    if pub.get("year") and py and abs(int(pub["year"]) - int(py)) > 1:
        return False
    return True


def _is_own(inst):
    oaid = (inst.get("id") or "").rsplit("/", 1)[-1]
    if oaid in OWN_INSTITUTION_IDS:
        return True
    name = (inst.get("display_name") or "").lower()
    return any(h in name for h in OWN_INSTITUTION_NAME_HINTS)


def _norm_doi(doi):
    if not doi:
        return None
    return doi.strip().replace("https://doi.org/", "").replace("http://doi.org/", "").lower()


def _canonical_doi(pub):
    d = _norm_doi(pub.get("doi"))
    if d:
        return d
    m = ARXIV_IN_VENUE.search(pub.get("venue") or "")
    if m:
        return f"10.48550/arxiv.{m.group(1).lower()}"
    return None


def _external_institutions(work):
    out = {}
    for a in work.get("authorships") or []:
        for institution in a.get("institutions") or []:
            if _is_own(institution):
                continue
            oaid = (institution.get("id") or "").rsplit("/", 1)[-1]
            if oaid:
                out[oaid] = institution
    return out


def _richness(work):
    return sum(1 for a in (work.get("authorships") or []) if a.get("institutions"))


def _score(work):
    """Rank candidates: most affiliation data first, then prefer a published
    article over a preprint (the published twin usually has the institutions)."""
    published = 0 if (work.get("type") == "preprint") else 1
    return (_richness(work), published)


def _throttle():
    with _throttle_lock:
        dt = time.monotonic() - _last_req[0]
        if dt < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - dt)
        _last_req[0] = time.monotonic()


def _request(url, params, retries=6):
    """Returns (parsed_json | None, ok_flag). ok_flag False means a hard failure
    (worth retrying) rather than a legitimate 404/empty."""
    for attempt in range(retries + 1):
        _throttle()
        try:
            resp = requests.get(url, params=_params(params), headers=HEADERS, timeout=30)
            if resp.status_code == 404:
                return None, True
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 20))
                continue
            resp.raise_for_status()
            return resp.json(), True
        except Exception:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 20))
                continue
            with _fail_lock:
                _failures[0] += 1
            return None, False
    with _fail_lock:
        _failures[0] += 1
    return None, False


def _params(extra):
    p = dict(extra)
    if API_KEY:
        p["api_key"] = API_KEY
    return p


def singleton_by_doi(doi):
    """FREE single-entity lookup by DOI. Returns (work|None, ok)."""
    if not doi:
        return None, True
    return _request(f"{WORKS_URL}/doi:{doi}", {"select": SELECT})


def _sanitize_for_filter(title):
    # title.search is a filter value: strip characters that break filter syntax
    # (comma = filter separator, | = OR, : = field separator, etc.)
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", " ", title, flags=re.UNICODE)).strip()


def title_lookup(pub):
    """Cheap title.search filter. Returns (best_candidate|None, ok_flag)."""
    title = (pub.get("title") or "").strip()
    if not title:
        return None, True
    q = _sanitize_for_filter(title)
    if not q:
        return None, True
    data, ok = _request(WORKS_URL, {"filter": f"title.search:{q}", "select": SELECT, "per_page": 25})
    if not ok:
        return None, False
    cands = [c for c in (data or {}).get("results", [])
             if _titles_match(title, c.get("title") or "") and _year_ok(pub, c)]
    if not cands:
        return None, True
    return max(cands, key=_score), True


def budget():
    try:
        resp = requests.get(RATELIMIT_URL, params=_params({}), headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("rate_limit", {}).get("daily_remaining_usd")
    except Exception:
        pass
    return None


def _progress(total, every=50):
    with _fail_lock:
        _done[0] += 1
        n = _done[0]
    if n % every == 0:
        print(f"    ...{n}/{total}  ({_failures[0]} req-fails)")


def probe(pubs_path, report_path, limit, author_cap):
    if not API_KEY:
        print("WARNING: no OPENALEX_API_KEY set — you'll hit the $0.10/day tier and run out.\n")
    b0 = budget()
    if b0 is not None:
        print(f"OpenAlex budget remaining: ${b0:.4f}\n")

    data = json.loads(Path(pubs_path).read_text())
    pubs = data.get("publications", [])
    if limit:
        pubs = pubs[:limit]
    total = len(pubs)
    print(f"Probing {total} papers (free DOI singletons + cheap title.search; cap {author_cap})\n")

    # ---- Phase 1: FREE singleton lookups ----
    canon = {id(p): _canonical_doi(p) for p in pubs}
    wanted = sorted({d for d in canon.values() if d})
    print(f"  Phase 1 (free): resolving {len(wanted)} DOIs/arXiv-ids...")
    _done[0] = 0

    def f1(d):
        w, ok = singleton_by_doi(d)
        _progress(len(wanted))
        return d, w, ok

    doi_map = {}
    retry_dois = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for d, w, ok in ex.map(f1, wanted):
            if w:
                doi_map[d] = w
            elif not ok:
                retry_dois.append(d)
    for d in retry_dois:  # sweep
        w, ok = singleton_by_doi(d)
        if w:
            doi_map[d] = w
    print(f"  Phase 1 done: {len(doi_map)}/{len(wanted)} resolved ({_failures[0]} req-fails)\n")

    # ---- Phase 2: cheap title.search for papers still needing it ----
    need_title = [p for p in pubs
                  if not (doi_map.get(canon[id(p)]) and _external_institutions(doi_map[canon[id(p)]]))]
    est = len(need_title) * 0.0001
    print(f"  Phase 2 (paid ~${est:.3f}): title-searching {len(need_title)} papers...")
    _done[0] = 0

    def f2(pub):
        w, ok = title_lookup(pub)
        _progress(len(need_title))
        return id(pub), pub, w, ok

    title_map = {}
    retry_pubs = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for pid, pub, w, ok in ex.map(f2, need_title):
            if ok:
                title_map[pid] = w
            else:
                retry_pubs.append((pid, pub))
    for pid, pub in retry_pubs:  # sweep
        w, ok = title_lookup(pub)
        title_map[pid] = w if ok else None
    print(f"  Phase 2 done, {len(retry_pubs)} retried ({_failures[0]} req-fails)\n")

    # ---- Aggregate ----
    matched = with_inst = capped_out = rescued = 0
    how_counts = defaultdict(int)
    author_counts = []
    inst = defaultdict(lambda: {"name": None, "country": None, "papers": set(), "examples": []})

    for pub in pubs:
        exact = doi_map.get(canon[id(pub)]) if canon[id(pub)] else None
        exact_had = bool(exact and _external_institutions(exact))
        how = ("doi" if _norm_doi(pub.get("doi")) else "arxiv") if exact else None
        if exact_had:
            work = exact
        else:
            twin = title_map.get(id(pub))
            cands = [c for c in [exact, twin] if c is not None]
            if cands:
                work = max(cands, key=_score)
                if work is twin and twin is not None:
                    how = "title"
                if exact is not None and not exact_had and _external_institutions(work):
                    rescued += 1
            else:
                work = None
        if not work:
            continue
        matched += 1
        how_counts[how] += 1
        n_authors = len(work.get("authorships") or [])
        author_counts.append(n_authors)
        externals = _external_institutions(work)
        if author_cap and n_authors > author_cap:
            capped_out += 1
            continue
        if externals:
            with_inst += 1
        for oaid, institution in externals.items():
            rec = inst[oaid]
            rec["name"] = institution.get("display_name")
            rec["country"] = institution.get("country_code")
            rec["papers"].add(pub.get("id"))
            if len(rec["examples"]) < 3:
                rec["examples"].append({"title": pub.get("title"), "year": pub.get("year")})

    ranked = sorted(
        ({"openalex_id": k, "name": v["name"], "country": v["country"],
          "paper_count": len(v["papers"]), "examples": v["examples"]}
         for k, v in inst.items()),
        key=lambda r: -r["paper_count"],
    )

    edges = [5, 10, 20, 30, 50, 100]
    ahist = Counter()
    for x in author_counts:
        for e in edges:
            if x <= e:
                ahist[e] += 1
                break
        else:
            ahist["more"] += 1

    b1 = budget()
    report = {
        "probed_papers": total,
        "matched_in_openalex": matched,
        "match_rate_percent": round(100 * matched / total, 1) if total else 0,
        "matched_by": dict(how_counts),
        "empty_preprints_rescued": rescued,
        "request_failures": _failures[0],
        "author_cap": author_cap,
        "papers_capped_out": capped_out,
        "papers_with_external_institution_after_cap": with_inst,
        "distinct_institutions_after_cap": len(ranked),
        "author_count_histogram": {str(k): ahist[k] for k in edges + ["more"]},
        "budget_remaining_usd": b1,
        "institutions_ranked": ranked,
    }
    Path(report_path).write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print("=" * 66)
    print(f"COVERAGE:     matched {matched}/{total} ({report['match_rate_percent']}%)")
    print(f"              via {how_counts['doi']} DOI / {how_counts['arxiv']} arXiv / "
          f"{how_counts['title']} title")
    print(f"AFFILIATIONS: {with_inst} papers carried institutions (after cap)")
    print(f"RESCUED:      {rescued} empty preprints recovered via published twin")
    print(f"CAP:          removed {capped_out} papers with > {author_cap} authors")
    print(f"INSTITUTIONS: {len(ranked)} distinct (after cap)")
    print(f"REQ FAILURES: {_failures[0]}")
    if b1 is not None:
        print(f"BUDGET LEFT:  ${b1:.4f}")
    print("=" * 66)
    labels = {5: "  1-5", 10: "  6-10", 20: " 11-20", 30: " 21-30",
              50: " 31-50", 100: "51-100", "more": "  100+"}
    print("\nAuthors-per-paper (matched):")
    for k in edges + ["more"]:
        print(f"  {labels[k]}: {ahist[k]}")
    print("\nTOP 30 INSTITUTIONS (after cap), by shared paper count:\n")
    for r in ranked[:30]:
        cc = f" [{r['country']}]" if r["country"] else ""
        print(f"  {r['paper_count']:>3}  {r['name']}{cc}")
    print(f"\nReport: {report_path}")
    print("Send me that JSON — I'll confirm the cap and build the graph.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pubs", nargs="?", default=str(DEFAULT_PUBS))
    ap.add_argument("--report", default=str(DEFAULT_REPORT))
    ap.add_argument("--author-cap", type=int, default=30,
                    help="exclude papers with more than N authors from the ranking (default 30)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="probe every paper")
    g.add_argument("--limit", type=int, default=120, help="probe only first N papers (default 120)")
    args = ap.parse_args()
    probe(args.pubs, args.report, 0 if args.all else args.limit, args.author_cap)


if __name__ == "__main__":
    main()
