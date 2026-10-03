#!/usr/bin/env python3
"""
Extract REAL OSM road geometry for a city's popular streets from the country
PBF already staged in geo-datalake.

    python scripts/extract_street_geoms.py \
        --slug hongkong --extract hongkong \
        --pbf work/city.osm.pbf \
        --streets work/streets.json \
        --out work/out/<slug>/street-geoms.json

Why this exists. /api/street-geoms.json used to source geometry live from public
Overpass mirrors, which AGENTS.md bans outright ("CRITICAL - No public Overpass
API. Ever."). Where it had nothing cached it returned count:0 and the map drew
a stand-in: Hong Kong's 30 popular streets all rendered as identical short
diagonal stubs, because there was no geometry to draw. Melbourne looked correct
only because its 329 streets had already been cached from an earlier Overpass
run. That is a cache lottery, not a pipeline.

geo-datalake holds every country's extract under sources/osm/<cc>-latest.osm.pbf,
and the city pipeline has already cut the city bbox to work/city.osm.pbf with
`-s complete_ways`. So the geometry can come from the same sanctioned source the
boundaries already come from, in the same pass, with no new download.

Output is byte-compatible with the existing /api/street-geoms.json shape, keyed
by canonical street name, because the harness looks up
[streetKey, canonicalName, canonicalRaw] in that order - a name-only key is
enough.

canonical_street_key() is a deliberate port of canonicalStreetKey() in
src/pages/api/street-geoms.json.ts. If the JS side changes, change both.
"""

import argparse
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request

import osmium
from osmium.osm import Way

# Same table as the JS side, and the same order.
ABBREVIATIONS = [
    (r"\bave\b", "avenue"),
    (r"\brd\b", "road"),
    (r"\bst\b", "street"),
    (r"\bdr\b", "drive"),
    (r"\bblvd\b", "boulevard"),
    (r"\bln\b", "lane"),
    (r"\bct\b", "court"),
    (r"\bhwy\b", "highway"),
    (r"\bcres\b", "crescent"),
    (r"\bpl\b", "place"),
    (r"\bwy\b", "way"),
]

MAX_COORDS_PER_STREET = 600
ROUND = 6

# Keep only way fragments near the street's own seed point. Without this, a name
# like "Hauptstrasse" collects every fragment with that name across the whole
# city and the street ends up scattered over the map - which is also what makes
# the file large. The old Overpass implementation pinned its per-street query to
# a tiny bbox for the same reason; this is the offline equivalent.
SEED_RADIUS_M = 2500.0

# Address datasets and OSM spell the same street differently often enough to
# matter: "Tiergartenstrasse" vs "TiergartenstraÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¸e", "GÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¶teborgsgatan" vs
# "Goteborgsgatan". Folding these before matching recovered most of the misses.
TRANSLITERATE = {
    "ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¸": "ss", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¦": "ae", "ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ": "ae", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¸": "o", "ÃƒÆ’Ã†â€™Ãƒâ€¹Ã…â€œ": "o",
    "ÃƒÆ’Ã¢â‚¬Å¾ÃƒÂ¢Ã¢â€šÂ¬Ã‹Å“": "d", "ÃƒÆ’Ã¢â‚¬Å¾Ãƒâ€šÃ‚Â": "d", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°": "d", "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â": "d", "ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡": "l", "ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â": "l",
    "ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¾": "th", "ÃƒÆ’Ã†â€™Ãƒâ€¦Ã‚Â¾": "th", "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢": "'", "'": "'",
}


def canonical_street_key(name):
    if not name:
        return ""
    k = unicodedata.normalize("NFD", str(name).lower())
    k = "".join(ch for ch in k if not unicodedata.combining(ch))
    for src, dst in TRANSLITERATE.items():
        k = k.replace(src, dst)
    k = re.sub(r"['ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢.]", "", k)
    k = re.sub(r"\s+", " ", k).strip()
    for pattern, full in ABBREVIATIONS:
        k = re.sub(pattern, full, k)
    return k


def simplify(coords, min_m=2.0):
    """
    Drop consecutive points closer than ~2 m.

    OSM ways carry a node at every corner, kerb and driveway entrance, so the
    raw geometry is several times larger than the shape needs. Berlin's 71
    streets came to 6.6 KB each before this.
    """
    if len(coords) < 3:
        return coords
    out = [coords[0]]
    lat0 = coords[0][1]
    mx = 111320.0 * max(0.05, math.cos(math.radians(lat0)))
    my = 110574.0
    for pt in coords[1:]:
        prev = out[-1]
        dx = (pt[0] - prev[0]) * mx
        dy = (pt[1] - prev[1]) * my
        if (dx * dx + dy * dy) >= min_m * min_m:
            out.append(pt)
    if len(out) >= 2 and out[0] != out[-1]:
        out.append(out[0])
    return out


def load_street_names(path, limit):
    """
    Pull the streets to resolve out of a city-streets payload - either the full
    API document or a bare list. Returns (seeds, names) where each seed carries
    the canonical key, the display name and the lat/lon the dataset placed it at.
    Best-known streets first, deduplicated on the canonical key.
    """
    with open(path, encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    rows = doc.get("streets") if isinstance(doc, dict) else doc
    if not rows:
        return [], []
    scored = []
    for row in rows:
        name = row.get("n") or row.get("name") or row.get("raw")
        if not name:
            continue
        la, lo = row.get("la"), row.get("lo")
        if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)):
            continue
        scored.append((canonical_street_key(name), name, int(row.get("p") or 0), la, lo))
    scored.sort(key=lambda t: -t[2])
    seen = set()
    seeds = []
    for key, name, _p, la, lo in scored:
        if key in seen:
            continue
        seen.add(key)
        seeds.append((key, name, la, lo))
    if limit:
        seeds = seeds[:limit]
    return seeds, [s[1] for s in seeds]


class WayCollector(osmium.SimpleHandler):
    def __init__(self, seeds):
        super().__init__()
        self.seeds = {k: (la, lo) for k, _n, la, lo in seeds}
        self.display = {k: n for k, n, _la, _lo in seeds}
        self.wanted = set(self.seeds)
        self.geoms = {}
        self.matched = set()

    def way(self, w):
        tags = w.tags
        if not tags:
            return
        raw = tags.get("name")
        if not raw:
            return
        key = canonical_street_key(raw)
        seed = self.seeds.get(key)
        if seed is None:
            return
        coords = []
        for nd in w.nodes:
            if nd.location.valid():
                coords.append([round(nd.location.lon, ROUND), round(nd.location.lat, ROUND)])
        if len(coords) < 2:
            return
        seed_lat, seed_lon = seed
        mx = 111320.0 * max(0.05, math.cos(math.radians(seed_lat)))
        mid = coords[len(coords) // 2]
        dx = (mid[0] - seed_lon) * mx
        dy = (mid[1] - seed_lat) * 110574.0
        if (dx * dx + dy * dy) > SEED_RADIUS_M * SEED_RADIUS_M:
            return
        coords = simplify(coords)
        if len(coords) < 2:
            return
        self.matched.add(key)
        if key not in self.geoms:
            self.geoms[key] = []
        if len(self.geoms[key]) < MAX_COORDS_PER_STREET:
            self.geoms[key].append(coords)


def fetch_street_payload(slug, explicit=None, timeout=60):
    """The list of streets to resolve. Prefers a local file, else the public API."""
    if explicit:
        with open(explicit, encoding="utf-8-sig") as fh:
            return json.load(fh)
    url = f"https://preview-geo-astro-site.foodstarmelbourne.workers.dev/api/city-streets.json?city={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "ayeeff/geo-atlas-2d/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--streets", help="city-streets JSON; fetched from the API if omitted")
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    payload = fetch_street_payload(args.slug, args.streets)
    seeds, names = load_street_names(args.streets or payload, args.limit)
    if not seeds:
        print(json.dumps({"slug": args.slug, "count": 0, "geoms": {},
                          "note": "no street names to resolve"}))
        return 0

    collector = WayCollector(seeds)
    collector.apply_file(args.pbf, locations=True)

    geoms = {}
    for key, lines in collector.geoms.items():
        geoms[key] = {"n": collector.display.get(key, key), "lines": lines}

    out = {
        "slug": args.slug,
        "v": 1,
        "generated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                      .isoformat().replace("+00:00", "Z"),
        "source": "geo-datalake country pbf (no overpass)",
        "wanted": len(seeds),
        "count": len(geoms),
        "geoms": geoms,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))

    missing = [n for k, n, _la, _lo in seeds if k not in geoms]
    size = os.path.getsize(args.out)
    print(json.dumps({
        "slug": args.slug,
        "wanted": len(seeds),
        "matched": len(geoms),
        "missing": len(missing),
        "bytes": size,
        "sampleMissing": missing[:5],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
