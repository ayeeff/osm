#!/usr/bin/env python3
"""
Fill neighborhoods.json with population and a photo, from Wikidata.

    python scripts/enrich_neighborhoods.py \
        --in work/out/<slug>/neighborhoods.json \
        --out work/out/<slug>/neighborhoods.json \
        --cache work/wd-cache.json

Why Wikidata and not the OSM tags: OSM carries a population tag on a small
minority of relations. On Berlin's 721 neighborhood polygons only a handful have
one, but 622 have a wikidata= QID, so the join rate is high.

Why this is allowed where Overpass is not: this is the Wikidata REST
`wbgetentities` entity lookup, not a public *query* service. We issue ~13
batched GETs for Berlin, we cache every answer in R2 forever, and we only ever
ask for QIDs that already appear in our own output. Overpass is banned because
it is an open, abuse-prone query surface that 429s under load (see AGENTS.md);
`wbgetentities` is neither.

Only P1082 (population) and P18 (image) are read, plus P131 to sanity-check
that the Wikidata item is the same kind of place. Anything ambiguous is left
null rather than guessed.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

API = "https://www.wikidata.org/w/api.php"
UA = "ayeeff/geo-atlas-2d (neighborhood enrichment; contact via repo)"

# A sane ceiling. The largest city district on earth is a few million; anything
# above this is a metro area, a country, or a bad parse.
MAX_POPULATION = 25_000_000

# Prefer the most recent statement when an item carries several populations.
TIME_RE = re.compile(r"^[+-](\d{1,4})-\d{2}-\d{2}T")


def api_get(params, timeout=30):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def claim_values(entity, prop):
    """All values for one property, as a list of {value, rank, time}."""
    out = []
    for claim in (entity.get("claims") or {}).get(prop, []) or []:
        snak = claim.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        when = None
        for qual in claim.get("qualifiers", {}).get("P585", []) or []:
            dv = (qual.get("datavalue") or {}).get("value") or {}
            when = dv.get("time")
        out.append(
            {
                "value": snak.get("datavalue", {}).get("value"),
                "rank": claim.get("rank", "normal"),
                "time": when,
            }
        )
    return out


def parse_time(t):
    if not t:
        return None
    m = TIME_RE.match(t)
    return int(m.group(1)) if m else None


def pick_population(entity):
    """Best (value, year) for P1082: preferred > normal, then most recent year."""
    cands = claim_values(entity, "P1082")
    vals = []
    for c in cands:
        v = c["value"]
        if isinstance(v, dict) and "amount" in v:
            try:
                v = int(v["amount"])
            except (TypeError, ValueError):
                continue
        if not isinstance(v, (int, float)):
            continue
        v = int(v)
        if v <= 0 or v > MAX_POPULATION:
            continue
        vals.append((c["rank"], parse_time(c["time"]), v))
    if not vals:
        return None, None
    rank_order = {"preferred": 0, "normal": 1, "deprecated": 2}
    best = min(vals, key=lambda t: (rank_order.get(t[0], 1), -(t[1] or 0), t[2]))
    return best[2], best[1]


def pick_image(entity):
    """
    A Commons file name from P18, or a Commons category from P373 as a weaker
    fallback (the client renders a category through Special:FilePath's search
    redirect, so only P18 is trusted for the actual <img>).
    """
    for rank in ("preferred", "normal", "deprecated"):
        for c in claim_values(entity, "P18"):
            if c["rank"] == rank and isinstance(c["value"], str) and c["value"]:
                return c["value"], "P18"
    return None, ""


def commons_url(filename, width=480):
    name = filename.replace(" ", "_")
    return (
        "https://commons.wikimedia.org/wiki/Special:FilePath/"
        + urllib.parse.quote(name)
        + f"?width={width}"
    )


def fetch_missing(qids, cache, log):
    """Fill the cache for any QID we have not looked up yet."""
    todo = [q for q in qids if q and q not in cache]
    todo = sorted(set(todo))
    if not todo:
        return 0
    log(f"  looking up {len(todo)} new Wikidata items")
    for i in range(0, len(todo), 50):
        batch = todo[i : i + 50]
        for attempt in range(4):
            try:
                data = api_get(
                    {
                        "action": "wbgetentities",
                        "ids": "|".join(batch),
                        "props": "claims",
                        "format": "json",
                    }
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry any transport error
                if attempt == 3:
                    log(f"  !! batch {i} failed after 4 attempts: {exc}")
                    data = {}
                else:
                    time.sleep(2 * (attempt + 1))
        for q in batch:
            ent = (data.get("entities") or {}).get(q)
            if not ent or "missing" in ent:
                cache[q] = {}
                continue
            pop, year = pick_population(ent)
            img, img_prop = pick_image(ent)
            cache[q] = {
                "residents": pop,
                "populationYear": year,
                "image": commons_url(img) if img else "",
                "imageSource": img_prop,
            }
        time.sleep(0.4)  # be polite
    return len(todo)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--cache", dest="cache_path", required=True)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    def log(msg):
        if not args.quiet:
            print(msg, flush=True)

    with open(args.in_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    if os.path.exists(args.cache_path):
        with open(args.cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    else:
        cache = {}

    feats = doc.get("features", [])
    qids = [f.get("properties", {}).get("wikidata") for f in feats]
    log(f"enriching {len(feats)} polygons, {sum(1 for q in qids if q)} with a wikidata id")
    fetch_missing(qids, cache, log)

    hits = {"osm": 0, "wikidata": 0, "photo": 0, "none": 0}
    for f in feats:
        p = f.setdefault("properties", {})
        wd = (cache.get(p.get("wikidata")) or {})
        if p.get("residents") is None and wd.get("residents") is not None:
            p["residents"] = wd["residents"]
            p["populationYear"] = wd.get("populationYear")
        if not p.get("image") and wd.get("image"):
            p["image"] = wd["image"]
            p["imageSource"] = wd.get("imageSource", "")
        if p.get("residents") is not None:
            hits["wikidata" if wd.get("residents") is not None else "osm"] += 1
        else:
            hits["none"] += 1
        if p.get("image"):
            hits["photo"] += 1

    os.makedirs(os.path.dirname(os.path.abspath(args.out_path)) or ".", exist_ok=True)
    with open(args.out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    with open(args.cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"))

    log(
        f"  population: {hits['wikidata']}/{len(feats)}  "
        f"photo: {hits['photo']}/{len(feats)}  cache: {len(cache)} entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
