#!/usr/bin/env python3
"""Recompute ONLY the ELLIS Sites collaboration network from the current
roster (config/ellis_members.json) against the already-fetched papers in
docs/data/publications.json.

No SerpAPI, no OpenAlex — it reuses the co-authors already saved in
publications.json, so it costs nothing and runs in seconds. Use this
whenever you update the roster; a full `fetch_data.py` run is only needed
when you want fresh publications/citations.

    python3 scripts/recompute_members.py
"""
import json, importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PUBS = ROOT / "docs" / "data" / "publications.json"

# reuse the exact matching logic from the main pipeline
spec = importlib.util.spec_from_file_location("fd", HERE / "fetch_data.py")
fd = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(fd)
except SystemExit:
    pass

team = json.loads((ROOT / "config" / "team.json").read_text())
members = json.loads((ROOT / "config" / "ellis_members.json").read_text())

data = json.loads(PUBS.read_text())
pubs = {p["id"]: p for p in data["publications"]}

exact_lookup, fuzzy_lookup = fd.build_member_lookup(members, team)
counts, details = fd.compute_member_collaborations(pubs, exact_lookup, fuzzy_lookup)

data["ellis_member_collaborations"] = counts
data["ellis_member_collaboration_details"] = details
PUBS.write_text(json.dumps(data, ensure_ascii=False, indent=2))

shown = [k for k in counts if "Tübingen" not in k]
print(f"Recomputed against {len(exact_lookup)} roster entries.")
print(f"{len(shown)} ELLIS Sites on the graph (Tübingen home excluded by design).")
print("Top:", dict(sorted(((k, v) for k, v in counts.items() if 'Tübingen' not in k), key=lambda kv: -kv[1])[:6]))
