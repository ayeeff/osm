#!/usr/bin/env python3
"""
Build the 2D dark city map datasets (places.json + demand-streets.json) for one
city from its OSM PBF extract.

Reads the PBF directly with pyosmium in a single pass. An earlier version drove
`osmium export` with a per-object-type config, which silently dropped every open
highway way unless `linear_tags` was declared as a list of tag filters — and
osmium export is tag-driven, so anything matching neither list is simply not
written. Parsing the PBF ourselves removes that whole class of failure and lets
`with_locations()` resolve the node references ways need.

  places.json         FeatureCollection<Point>  { name, rank, kind }
  demand-streets.json FeatureCollection<Line>   { id, demand }

Ported from workers/atlas-2d/src/assemble.js in ayeeff/astrogl so the Overpass
path and this path produce the same shape. Change a constant in one, change it
in both.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import osmium
from osmium.osm import Node, Relation, Way

# --------------------------------------------------------------------------
# Tag groups. Mirrors POI_GROUPS in assemble.js / queries.js.
# `rank` drives label importance, `cap` bounds the per-kind feature count.
# --------------------------------------------------------------------------

POI_GROUPS = [
    {"id": "air", "kind": "air", "rank": 70, "cap": 5},
    {"id": "landmark", "kind": "landmark", "rank": 5, "cap": 600},
    {"id": "employment", "kind": "employment", "rank": 5, "cap": 600},
    {"id": "shop", "kind": "shop", "rank": 7, "cap": 500},
    {"id": "edu", "kind": "edu", "rank": 12, "cap": 250},
    {"id": "health", "kind": "health", "rank": 6, "cap": 250},
    {"id": "night", "kind": "night", "rank": 2, "cap": 250},
]
GROUP_BY_KIND = {g["kind"]: g for g in POI_GROUPS}

DISTRICT_RANK = {6: 4, 7: 4, 8: 4, 9: 5, 10: 6, 11: 6, 12: 6}

# Named place nodes/areas make a better district source than admin boundaries in
# much of the world — China's OSM coverage of admin_level 8-10 is thin, while
# place tagging is dense. village/hamlet are excluded: they fall below the
# style's rank>=4 label threshold, so they would draw a district dot with no
# label.
PLACE_KINDS = {
    "city": 6,
    "town": 5,
    "borough": 5,
    "suburb": 5,
    "quarter": 4,
    "neighbourhood": 4,
    "district": 5,
}

# Deliberately the same three classes atlas-2d-worker uses. footway, path and
# cycleway are excluded on both sides: they dominate the way count in any city
# and carry no demand signal.
STREET_CLASSES = {
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "living_street",
    "service", "pedestrian", "track",
}

NAME_KEYS = ("name", "name:en", "name:zh", "name:zh-Hans", "name:ja", "name:ko")

# --------------------------------------------------------------------------
# Demand scoring constants — identical to assemble.js
# --------------------------------------------------------------------------

ROAD_BOOST = {
    "motorway": 1.8,
    "trunk": 1.7,
    "primary": 1.6,
    "secondary": 1.35,
    "tertiary": 1.2,
    "residential": 1.0,
    "unclassified": 0.9,
    "living_street": 0.85,
    "pedestrian": 0.7,
    "track": 0.5,
    "service": 0.4,
}

KIND_WEIGHT = {
    "shop": 1,
    "edu": 1.5,
    "health": 1.5,
    "night": 2,
    "landmark": 2,
    "employment": 2.5,
    "air": 3,
    "district": 0.4,
}

RADIUS_DEG = 0.0015  # ~150 m of latitude
CELL = RADIUS_DEG * 2
PCT = 0.985
# District output is uncapped, so cap what goes into the scoring grid — enough to
# cover any realistic city centre, and it keeps the grid build bounded.
DISTRICT_GRID_CAP = 4000


def round5(n):
    return round(n, 5)


def name_of(tags):
    for key in NAME_KEYS:
        value = tags.get(key)
        if value:
            return value
    return None


def classify(tags):
    """Map OSM tags to one of our kinds, or None."""
    if tags.get("boundary") == "administrative":
        level = tags.get("admin_level")
        if level and str(level).isdigit() and int(level) in DISTRICT_RANK:
            return "district"
    place = tags.get("place")
    if place in PLACE_KINDS and name_of(tags):
        return "place"
    if tags.get("aeroway") == "aerodrome":
        return "air"
    if tags.get("station") == "airport" and tags.get("railway") == "station":
        return "air"

    if tags.get("tourism") in {
        "museum", "attraction", "artwork", "viewpoint", "gallery",
        "theme_park", "zoo", "aquarium",
    }:
        return "landmark"
    if tags.get("historic"):
        return "landmark"
    if tags.get("man_made") in {"tower", "lighthouse", "bridge", "obelisk", "statue"}:
        return "landmark"
    if tags.get("leisure") in {"stadium", "arena", "park"} and name_of(tags):
        return "landmark"

    if tags.get("amenity") in {"bank", "stock_exchange", "courthouse", "townhall"}:
        return "employment"
    if tags.get("building") == "office" and name_of(tags):
        return "employment"
    if tags.get("office"):
        return "employment"
    if tags.get("shop"):
        return "shop"
    if tags.get("amenity") in {"school", "college", "university", "kindergarten", "library"}:
        return "edu"
    if tags.get("healthcare"):
        return "health"
    if tags.get("amenity") in {"hospital", "clinic", "doctors", "pharmacy"}:
        return "health"
    if tags.get("amenity") in {
        "restaurant", "bar", "pub", "cafe", "fast_food", "nightclub", "wine_bar",
    }:
        return "night"
    if tags.get("leisure") in {"nightclub", "bar", "pub"}:
        return "night"
    return None


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.areas = defaultdict(list)  # kind -> [(name, lon, lat, rank)]
        self.streets = []               # (score, highway, osm_id, [[lon, lat], ...])
        self.counts = defaultdict(int)
        self._grid = None
        self._nearby = None
        self.ways_seen = 0

    # The POI grid is built lazily on the first way. OSM PBF is node-sorted, so
    # every node — and therefore every POI — has already been seen by the time
    # the first way arrives, which is what makes streaming scoring possible.
    def _ensure_grid(self):
        if self._grid is not None:
            return
        grid = defaultdict(list)
        for kind, items in self.areas.items():
            weight = KIND_WEIGHT.get(kind, 1)
            # Districts are uncapped in the output, so cap the grid snapshot to
            # keep it bounded; the per-kind caps match the final output.
            cap = DISTRICT_GRID_CAP if kind == "district" else GROUP_BY_KIND[kind]["cap"]
            for _name, lon, lat, _rank in items[:cap]:
                grid[(math.floor(lon / CELL), math.floor(lat / CELL))].append((lon, lat, weight))
        self._grid = grid

        r2 = RADIUS_DEG * RADIUS_DEG
        cache = {}

        def nearby(lon, lat):
            # Way vertices repeat heavily along a street; memoise on a coarse key.
            key = (round(lon, 4), round(lat, 4))
            hit = cache.get(key)
            if hit is not None:
                return hit
            total = 0
            gx = math.floor(lon / CELL)
            gy = math.floor(lat / CELL)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for px, py, weight in grid.get((gx + dx, gy + dy), ()):
                        ddx = px - lon
                        ddy = py - lat
                        if ddx * ddx + ddy * ddy <= r2:
                            total += weight
            cache[key] = total
            return total

        self._nearby = nearby

    def _add_area(self, tags, lon, lat):
        kind = classify(tags)
        if kind is None:
            return
        name = name_of(tags)
        if not name:
            return
        if kind == "district":
            level = tags.get("admin_level")
            rank = DISTRICT_RANK.get(int(level), 5) if level and str(level).isdigit() else 5
            self.areas["district"].append((name, lon, lat, rank))
        elif kind == "place":
            self.areas["district"].append((name, lon, lat, PLACE_KINDS[tags["place"]]))
        else:
            group = GROUP_BY_KIND[kind]
            self.areas[kind].append((name, lon, lat, group["rank"]))
        self.counts[kind] += 1

    def node(self, n):
        if not n.tags:
            return
        loc = n.location
        if not loc.valid():
            return
        self._add_area(n.tags, round5(loc.lon), round5(loc.lat))

    def way(self, w):
        highway = w.tags.get("highway") if w.tags else None
        wants_street = highway in STREET_CLASSES
        wants_area = bool(w.tags) and w.tags.get("boundary") == "administrative"
        if not wants_street and not wants_area:
            return
        if wants_street:
            self.ways_seen += 1

        coords = []
        for nd in w.nodes:
            loc = nd.location
            if loc.valid():
                coords.append((round5(loc.lon), round5(loc.lat)))

        if wants_street and len(coords) >= 2:
            # Score now and keep only the streets that actually have POI signal.
            # Retaining every way in a big city exhausts runner memory; this is
            # the same reason atlas-2d-worker scores per tile instead of in one
            # assemble pass.
            self._ensure_grid()
            weight_sum = 0.0
            for lon, lat in coords:
                weight_sum += self._nearby(lon, lat)
            if weight_sum > 0:
                boost = ROAD_BOOST.get(highway, 1.0)
                score = (weight_sum / len(coords)) * boost
                self.streets.append((score, highway, w.id, coords))
        if wants_area and coords:
            self._add_area(w.tags, *centroid(coords))

    def relation(self, r):
        # Only reached with with_areas() enabled, so an administrative boundary
        # arrives with its multipolygon already assembled. RelationMember has no
        # .location in pyosmium, so member coordinates are not an option here.
        if not r.tags or r.tags.get("boundary") != "administrative":
            return
        geometry = getattr(r, "geometry", None)
        if geometry is None:
            return
        ring = exterior_ring(geometry)
        if not ring:
            return
        lon, lat = centroid(ring)
        self._add_area(r.tags, round5(lon), round5(lat))


def exterior_ring(geometry):
    """First exterior ring of a Polygon/Multipolygon geometry, as [(lon, lat)]."""
    kind = geometry.type
    if kind == "Polygon":
        rings = list(geometry)
    elif kind == "MultiPolygon":
        rings = list(geometry)
        if not rings:
            return []
        rings = list(rings[0])
    else:
        return []
    if not rings:
        return []
    return [(p.lon, p.lat) for p in rings[0] if p.valid()]


def centroid(coords):
    """Shoelace centroid of a ring, falling back to the vertex mean."""
    if len(coords) < 3:
        n = len(coords) or 1
        return (
            sum(c[0] for c in coords) / n,
            sum(c[1] for c in coords) / n,
        )
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(coords) - 1):
        x0, y0 = coords[i]
        x1, y1 = coords[i + 1]
        cross = x0 * y1 - x1 * y0
        area2 += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if area2 != 0:
        return cx / (3.0 * area2), cy / (3.0 * area2)
    n = len(coords)
    return sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n


def build_places(collector):
    features = []
    counts = {}
    seen = set()

    for name, lon, lat, rank in collector.areas.get("district", []):
        key = ("district", name, round5(lat), round5(lon))
        if key in seen:
            continue
        seen.add(key)
        features.append(
            {
                "type": "Feature",
                "properties": {"name": name, "rank": rank, "kind": "district"},
                "geometry": {"type": "Point", "coordinates": [round5(lon), round5(lat)]},
            }
        )
    counts["district"] = len(collector.areas.get("district", []))

    for group in POI_GROUPS:
        kind = group["kind"]
        picked = collector.areas.get(kind, [])
        # Prefer specific names when the cap bites, same as normalizePois().
        picked.sort(key=lambda item: (len(item[0]), item[0]))
        kept = 0
        for name, lon, lat, rank in picked[: group["cap"]]:
            key = (kind, name, round5(lat), round5(lon))
            if key in seen:
                continue
            seen.add(key)
            features.append(
                {
                    "type": "Feature",
                    "properties": {"name": name, "rank": rank, "kind": kind},
                    "geometry": {"type": "Point", "coordinates": [round5(lon), round5(lat)]},
                }
            )
            kept += 1
        counts[kind] = kept

    return features, counts


def build_demand(streets, max_streets):
    """Normalise the already-scored streets and emit the top `max_streets`."""
    if not streets:
        return []

    positives = sorted(s[0] for s in streets)
    cutoff = positives[min(len(positives) - 1, int(len(positives) * PCT))]

    streets.sort(key=lambda item: -item[0])
    out = []
    for score, _highway, osm_id, coords in streets[:max_streets]:
        demand = min(1.0, score / cutoff) if cutoff > 0 else 0.0
        out.append(
            {
                "type": "Feature",
                "properties": {"id": osm_id, "demand": round5(demand)},
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in coords]},
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--pbf", required=True, help="city .osm.pbf from osmium extract")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--extract", default="", help="source extract slug, for the manifest")
    parser.add_argument("--max-streets", type=int, default=4000)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    collector = Collector()
    # with_areas() assembles boundary multipolygons so relations carry geometry;
    # with_locations() resolves the node references ways are built from.
    for obj in osmium.FileProcessor(args.pbf).with_areas().with_locations():
        if isinstance(obj, Node):
            collector.node(obj)
        elif isinstance(obj, Way):
            collector.way(obj)
        elif isinstance(obj, Relation):
            collector.relation(obj)

    places, counts = build_places(collector)
    demand = build_demand(collector.streets, args.max_streets)

    places_doc = {"type": "FeatureCollection", "features": places}
    demand_doc = {"type": "FeatureCollection", "features": demand}

    places_path = os.path.join(args.out_dir, "places.json")
    demand_path = os.path.join(args.out_dir, "demand-streets.json")
    with open(places_path, "w", encoding="utf-8") as handle:
        json.dump(places_doc, handle, separators=(",", ":"))
    with open(demand_path, "w", encoding="utf-8") as handle:
        json.dump(demand_doc, handle, separators=(",", ":"))

    max_demand = max((f["properties"]["demand"] for f in demand), default=0)
    manifest = {
        "slug": args.slug,
        "extract": args.extract,
        "places": len(places),
        "byKind": counts,
        "streetWaysScanned": collector.ways_seen,
        "streetsWithSignal": len(collector.streets),
        "streets": len(demand),
        "maxDemand": max_demand,
        "bytes": {
            "places.json": os.path.getsize(places_path),
            "demand-streets.json": os.path.getsize(demand_path),
        },
    }
    with open(os.path.join(args.out_dir, "_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)

    print(json.dumps(manifest))
    if not places:
        print(f"warning: no places extracted for {args.slug}", file=sys.stderr)
        return 1
    if not demand:
        print(f"warning: no demand streets extracted for {args.slug}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
