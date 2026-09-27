#!/usr/bin/env python3
"""
Fill in the missing city bboxes in layer/anchor-pmtiles-manifest.json.

    python scripts/fill_city_bboxes.py --cities cities.json --out bboxes.json
    python scripts/fill_city_bboxes.py --cities cities.json --resolve-from city-bboxes.json

655 of the 1228 city-atlas slugs have no bbox in the manifest, so they cannot be
cut from a country extract at all. This resolves them from OSM — the same
sanctioned source as everything else (AGENTS.md: no public Overpass). For each
city it searches its country's extract for a named populated place or an
administrative boundary and takes that object's real bbox.

Two passes, because OSM PBF orders nodes -> ways -> relations:

  1. names    - collect candidate objects that carry a `name` matching the city.
                Nodes and ways (place=*) and relations (boundary=administrative,
                admin_level 6-8, i.e. a district or better) both qualify.
  2. geometry - second pass over the same file, this time recording the bbox of
                the matched ids. Relations are matched by id; a relation's
                geometry is assembled by the caller from its member ways, so we
                report the member ways' bbox, which is the real extent.

Everything is streamed, so a 3.5 GB country extract is fine.

Resolution is deliberately conservative: if the best match is ambiguous (two
candidates of similar size in different places, or nothing within the country's
plausible range) the city is reported unresolved rather than guessed. A wrong
bbox silently produces a map of the wrong place — which is exactly the bug that
made berlin.pmtiles an extract of New Jersey.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

import osmium
from osmium.osm import Node, Relation, Way

# Only these administrative levels are a "city" for our purposes:
# 4 state/region, 5, 6 county/district, 7, 8 municipality/city borough.
ADMIN_LEVELS = {"4", "5", "6", "7", "8"}

PLACE_KINDS = {
    "city", "town", "borough", "municipality", "suburb", "quarter",
    "neighbourhood", "village", "hamlet", "isolated_dwelling", "city_block",
}


def norm(value):
    """Fold accents and punctuation so 'São Paulo' == 'sao paulo'."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [round(min(xs), 5), round(min(ys), 5), round(max(xs), 5), round(max(ys), 5)]


def span_ok(bbox, country_hint=None):
    """Reject anything implausible for a single city extent."""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w <= 0 or h <= 0:
        return False
    # A city extent is not a whole country and not a single building.
    if w > 6.0 or h > 6.0:
        return False
    if w < 0.01 and h < 0.01:
        return False
    return True


class CandidateScan(osmium.SimpleHandler):
    """Pass 1: note every object whose name matches one of the wanted cities."""

    def __init__(self, wanted):
        super().__init__()
        self.wanted = wanted          # norm-name -> [slugs]
        self.node_ids = defaultdict(list)
        self.way_ids = defaultdict(list)
        self.rel_ids = defaultdict(list)

    def _offer(self, obj, tags, bucket):
        name = norm(tags.get("name") or tags.get("official_name"))
        if not name:
            return
        slugs = self.wanted.get(name)
        if not slugs:
            # Accept "X" for "City of X" and bare-name forms.
            for variant in (name, re.sub(r"^(city|town|municipality) of ", "", name)):
                slugs = self.wanted.get(variant)
                if slugs:
                    break
        if not slugs:
            return
        for slug in slugs:
            bucket[slug].append((obj.id, tags.get("place") or tags.get("boundary"), tags.get("admin_level")))

    def node(self, n):
        tags = n.tags
        if not tags:
            return
        kind = tags.get("place")
        if kind not in PLACE_KINDS:
            return
        self._offer(n, tags, self.node_ids)

    def way(self, w):
        tags = w.tags
        if not tags:
            return
        kind = tags.get("place")
        level = tags.get("admin_level")
        if kind not in PLACE_KINDS and not (tags.get("boundary") == "administrative" and level in ADMIN_LEVELS):
            return
        self._offer(w, tags, self.way_ids)

    def relation(self, r):
        tags = r.tags
        if not tags:
            return
        kind = tags.get("place")
        level = tags.get("admin_level")
        if kind not in PLACE_KINDS and not (tags.get("boundary") == "administrative" and level in ADMIN_LEVELS):
            return
        # Remember the member ways; a relation has no coordinates of its own.
        members = [m.ref for m in r.members if m.type == "w"]
        self._offer(r, tags, self.rel_ids)
        if members:
            self.rel_ids.setdefault("_members", {})[r.id] = members


class GeometryScan(osmium.SimpleHandler):
    """Pass 2: bbox of every id we shortlisted."""

    def __init__(self, node_ids, way_ids):
        super().__init__()
        self.node_ids = node_ids
        self.way_ids = way_ids
        self.node_box = {}
        self.way_box = {}

    def node(self, n):
        if n.id in self.node_ids and n.location.valid():
            self.node_box[n.id] = (n.location.lon, n.location.lat)

    def way(self, w):
        if w.id not in self.way_ids:
            return
        pts = []
        for nd in w.nodes:
            if nd.location.valid():
                pts.append((nd.location.lon, nd.location.lat))
        if len(pts) >= 2:
            self.way_box[w.id] = bbox_of(pts)


def resolve(pbf, wanted_slugs, log):
    names = {}
    for slug in wanted_slugs:
        names.setdefault(norm(slug.replace("-", " ")), []).append(slug)

    scan = CandidateScan(names)
    scan.apply_file(pbf)
    log(f"  pass 1: matched names for {len(scan.node_ids) + len(scan.way_ids)} city slugs")

    node_ids, way_ids = set(), set()
    for ids in scan.node_ids.values():
        for oid, _p, _l in ids:
            node_ids.add(oid)
    for ids in scan.way_ids.values():
        for wid, _p, _l in ids:
            way_ids.add(wid)

    members = getattr(scan, "rel_ids", {}).get("_members", {})
    for slug, ids in scan.rel_ids.items():
        if slug == "_members":
            continue
        for rid, _p, _l in ids:
            for wid in members.get(rid, ()):
                way_ids.add(wid)

    geo = GeometryScan(node_ids, way_ids)
    geo.apply_file(pbf, locations=True)
    log(f"  pass 2: geometry for {len(geo.node_box)} nodes, {len(geo.way_box)} ways")

    resolved, unresolved, ambiguous = {}, [], []
    for slug in wanted_slugs:
        boxes = []
        for oid, _p, _l in scan.node_ids.get(slug, ()):
            pt = geo.node_box.get(oid)
            if pt:
                boxes.append(bbox_of([pt, pt]))
        for wid, _p, _l in scan.way_ids.get(slug, ()):
            b = geo.way_box.get(wid)
            if b:
                boxes.append(b)
        for rid, _p, _l in scan.rel_ids.get(slug, ()):
            member_boxes = [geo.way_box[w] for w in members.get(rid, ()) if w in geo.way_box]
            if member_boxes:
                boxes.append(bbox_of([
                    (min(b[0] for b in member_boxes), min(b[1] for b in member_boxes)),
                    (max(b[2] for b in member_boxes), max(b[3] for b in member_boxes)),
                ]))

        boxes = [b for b in boxes if span_ok(b)]
        if not boxes:
            unresolved.append(slug)
            continue
        # Smallest plausible extent wins: a city relation, not its whole region.
        boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        smallest = boxes[0]
        area = (smallest[2] - smallest[0]) * (smallest[3] - smallest[1])
        # If a much larger box of the same name also appeared, the name is
        # ambiguous (e.g. a city and a county) - take the small one but say so.
        spread = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) > area * 12]
        if spread:
            ambiguous.append((slug, smallest, len(spread)))
        resolved[slug] = smallest

    return resolved, unresolved, ambiguous


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cities", required=True, help="JSON list of {slug, extract?, bbox?}")
    ap.add_argument("--extract", help="override the country extract slug for all")
    ap.add_argument("--pbf", help="single PBF to resolve against (skips grouping)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--resolve-from", help="merge previously resolved bboxes")
    ap.add_argument("--report", help="write unresolved/ambiguous here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    def log(m):
        if not args.quiet:
            print(m, flush=True)

    # utf-8-sig: these files get edited on Windows often enough that a BOM is a
    # realistic thing to meet, and json.load rejects it outright.
    with open(args.cities, encoding="utf-8-sig") as fh:
        cities = json.load(fh)
    if isinstance(cities, dict):
        cities = cities.get("cities", [])

    resolved_all = {}
    if args.resolve_from and os.path.exists(args.resolve_from):
        with open(args.resolve_from, encoding="utf-8-sig") as fh:
            resolved_all.update(json.load(fh))
        log(f"loaded {len(resolved_all)} previously resolved bboxes")

    todo = [c for c in cities
            if not c.get("bbox")
            and (args.extract or c.get("extract"))
            and c["slug"] not in resolved_all]

    if args.pbf:
        slugs = [c["slug"] for c in todo]
        log(f"resolving {len(slugs)} cities from {args.pbf}")
        got, missing, amb = resolve(args.pbf, slugs, log)
    else:
        log("no --pbf: group the cities by their country extract and pass one at a time")
        return 1

    resolved_all.update(got)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(resolved_all, fh, indent=1, sort_keys=True)
    log(f"resolved {len(got)}  unresolved {len(missing)}  ambiguous {len(amb)}")
    if missing:
        log("  unresolved: " + ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""))
    for slug, box, n in amb[:10]:
        log(f"  ambiguous: {slug} -> {box} ({n} larger same-name candidates)")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"unresolved": missing,
                       "ambiguous": {s: b for s, b, _ in amb}}, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
