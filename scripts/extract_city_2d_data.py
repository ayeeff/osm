#!/usr/bin/env python3
"""
Build the 2D dark city map datasets (places.json + demand-streets.json) for one
city from osmium-exported GeoJSONSeq.

Ported from workers/atlas-2d/src/assemble.js in ayeeff/astrogl so that the
Overpass path and this path produce the same shape. If you change a constant
here, change it there too.

  places.json         FeatureCollection<Point>  { name, rank, kind }
  demand-streets.json FeatureCollection<Line>   { id, demand }

Input (osmium export -f geojsonseq):
  places   area features, centroid in the "centroid" property
  streets  way features, geometry as LineString / MultiLineString / Polygon
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

# --------------------------------------------------------------------------
# Tag groups. Mirrors POI_GROUPS in assemble.js / queries.js.
# `rank` drives label importance, `cap` bounds the per-kind feature count.
# --------------------------------------------------------------------------

POI_GROUPS = [
    {
        "id": "air",
        "kind": "air",
        "rank": 70,
        "cap": 5,
    },
    {
        "id": "landmark",
        "kind": "landmark",
        "rank": 5,
        "cap": 600,
    },
    {
        "id": "employment",
        "kind": "employment",
        "rank": 5,
        "cap": 600,
    },
    {
        "id": "shop",
        "kind": "shop",
        "rank": 7,
        "cap": 500,
    },
    {
        "id": "edu",
        "kind": "edu",
        "rank": 12,
        "cap": 250,
    },
    {
        "id": "health",
        "kind": "health",
        "rank": 6,
        "cap": 250,
    },
    {
        "id": "night",
        "kind": "night",
        "rank": 2,
        "cap": 250,
    },
]

DISTRICT_RANK = {6: 4, 7: 4, 8: 4, 9: 5, 10: 6, 11: 6, 12: 6}

# `place=*` is a better district source than admin boundaries in much of the
# world — China's OSM coverage of admin_level 8-10 is thin, while place nodes
# and place areas are well mapped. rank 4 keeps them eligible for the
# `rank >= 4` district-label filter in the style.
# village/hamlet are deliberately absent: they would render with district
# styling (large teal dots) at a rank below the `rank >= 4` label threshold, so
# they add a dot and no label. Drop them.
PLACE_KINDS = {
    "city": 6,
    "town": 5,
    "borough": 5,
    "suburb": 5,
    "quarter": 4,
    "neighbourhood": 4,
    "district": 5,
}

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
    "cycleway": 0.45,
    "path": 0.4,
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

NAME_KEYS = ("name", "name:en", "name:zh", "name:zh-Hans", "name:ja", "name:ko")


def round5(n):
    return round(n, 5)


def name_of(props):
    for key in NAME_KEYS:
        value = props.get(key)
        if value:
            return value
    return None


def iter_geojsonseq(path):
    """Yield each feature from an osmium geojsonseq export, one JSON per line."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # A truncated final line means the export was cut short; skip it
                # rather than failing the whole city.
                continue


def classify(props):
    """Map OSM tags to one of our kinds, or None."""
    if props.get("boundary") == "administrative":
        level = props.get("admin_level")
        if level and str(level).isdigit() and int(level) in DISTRICT_RANK:
            return "district"

    # A named place node/area is a district for our purposes.
    place = props.get("place")
    if place in PLACE_KINDS and name_of(props):
        return "place"

    if props.get("aeroway") == "aerodrome":
        return "air"
    if props.get("station") == "airport" and props.get("railway") == "station":
        return "air"

    tourism = props.get("tourism")
    if tourism in {
        "museum", "attraction", "artwork", "viewpoint", "gallery",
        "theme_park", "zoo", "aquarium",
    }:
        return "landmark"
    if props.get("historic"):
        return "landmark"
    if props.get("man_made") in {"tower", "lighthouse", "bridge", "obelisk", "statue"}:
        return "landmark"
    if props.get("leisure") in {"stadium", "arena", "park"} and name_of(props):
        return "landmark"

    if props.get("amenity") in {"bank", "stock_exchange", "courthouse", "townhall"}:
        return "employment"
    if props.get("building") == "office" and name_of(props):
        return "employment"
    if props.get("office"):
        return "employment"

    if props.get("shop"):
        return "shop"

    if props.get("amenity") in {"school", "college", "university", "kindergarten", "library"}:
        return "edu"

    if props.get("healthcare"):
        return "health"
    if props.get("amenity") in {"hospital", "clinic", "doctors", "pharmacy"}:
        return "health"

    if props.get("amenity") in {
        "restaurant", "bar", "pub", "cafe", "fast_food", "nightclub", "wine_bar",
    }:
        return "night"
    if props.get("leisure") in {"nightclub", "bar", "pub"}:
        return "night"

    return None


def representative_point(feature):
    """
    A single lon/lat for an area feature.

    `osmium export` only emits a "centroid" property when the config asks for
    it, and it is not emitted for every geometry type, so never rely on it —
    fall back to the geometry itself. Getting this wrong silently yields an
    empty places.json, which is exactly what happened on the first CI run.
    """
    centroid = feature.get("centroid")
    if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
        return float(centroid[0]), float(centroid[1])

    geometry = feature.get("geometry") or {}
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    if gtype == "Point" and coords:
        return float(coords[0]), float(coords[1])
    if gtype == "LineString" and coords:
        mid = coords[len(coords) // 2]
        return float(mid[0]), float(mid[1])
    if gtype in ("Polygon", "MultiPolygon") and coords:
        # Polygon -> coords[0] is the outer ring; MultiPolygon -> coords[0][0] is it.
        ring = coords[0] if gtype == "Polygon" else coords[0][0]
        if ring and isinstance(ring[0], (list, tuple)):
            # Area centroid of the outer ring (shoelace), falling back to the
            # vertex mean for degenerate rings.
            area2 = 0.0
            cx = 0.0
            cy = 0.0
            for i in range(len(ring) - 1):
                x0, y0 = ring[i][0], ring[i][1]
                x1, y1 = ring[i + 1][0], ring[i + 1][1]
                cross = x0 * y1 - x1 * y0
                area2 += cross
                cx += (x0 + x1) * cross
                cy += (y0 + y1) * cross
            if area2 != 0:
                return cx / (3.0 * area2), cy / (3.0 * area2)
            return (
                sum(p[0] for p in ring) / len(ring),
                sum(p[1] for p in ring) / len(ring),
            )
    return None


def build_places(path):
    """Read the area export and return (features, kind_counts)."""
    by_kind = defaultdict(list)
    skipped = 0
    keys_seen = defaultdict(int)

    for feature in iter_geojsonseq(path):
        props = feature.get("properties") or {}
        for key in ("boundary", "admin_level", "place", "tourism", "amenity", "shop", "office"):
            if key in props:
                keys_seen[key] += 1
        point = representative_point(feature)
        if point is None:
            skipped += 1
            continue
        lon, lat = point

        kind = classify(props)
        if kind is None:
            continue
        name = name_of(props)
        if not name:
            continue

        if kind == "district":
            level = props.get("admin_level")
            rank = DISTRICT_RANK.get(int(level), 5) if level and str(level).isdigit() else 5
            by_kind["district"].append((name, lon, lat, rank))
        elif kind == "place":
            by_kind["district"].append((name, lon, lat, PLACE_KINDS[props["place"]]))
        else:
            group = next(g for g in POI_GROUPS if g["kind"] == kind)
            by_kind[kind].append((name, lon, lat, group["rank"]))

    if skipped:
        print(f"note: {skipped} area features had no usable geometry", file=sys.stderr)
    print("tag coverage: " + json.dumps(dict(sorted(keys_seen.items()))), file=sys.stderr)

    features = []
    counts = {}

    # Districts first (no cap), matching the JS buildPlaces ordering.
    seen = set()
    for name, lon, lat, rank in by_kind.get("district", []):
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
    counts["district"] = len(by_kind.get("district", []))

    for group in POI_GROUPS:
        kind = group["kind"]
        picked = by_kind.get(kind, [])
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


def line_strings(geometry):
    """Flatten any way geometry into a list of coordinate lists."""
    if not geometry:
        return []
    gtype = geometry.get("type")
    if gtype == "LineString":
        return [geometry["coordinates"]]
    if gtype == "MultiLineString":
        return [c for c in geometry["coordinates"] if len(c) > 1]
    if gtype == "Polygon":
        ring = geometry["coordinates"][0]
        return [ring] if len(ring) > 1 else []
    if gtype == "MultiPolygon":
        out = []
        for poly in geometry["coordinates"]:
            ring = poly[0]
            if len(ring) > 1:
                out.append(ring)
        return out
    return []


def build_demand(path, places, max_streets):
    """Score streets by nearby POI density and emit the top `max_streets`."""
    grid = defaultdict(list)
    for feature in places:
        props = feature["properties"]
        weight = KIND_WEIGHT.get(props["kind"], 1)
        lon, lat = feature["geometry"]["coordinates"]
        grid[(math.floor(lon / CELL), math.floor(lat / CELL))].append((lon, lat, weight))

    def nearby_weight(lon, lat):
        total = 0
        gx = math.floor(lon / CELL)
        gy = math.floor(lat / CELL)
        r2 = RADIUS_DEG * RADIUS_DEG
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py, weight in grid.get((gx + dx, gy + dy), ()):
                    ddx = px - lon
                    ddy = py - lat
                    if ddx * ddx + ddy * ddy <= r2:
                        total += weight
        return total

    scored = []
    for feature in iter_geojsonseq(path):
        props = feature.get("properties") or {}
        lines = line_strings(feature.get("geometry"))
        if not lines:
            continue

        weight_sum = 0.0
        vertex_count = 0
        for line in lines:
            for point in line:
                weight_sum += nearby_weight(float(point[0]), float(point[1]))
                vertex_count += 1
        if weight_sum <= 0:
            continue

        boost = ROAD_BOOST.get(props.get("highway", "residential"), 1.0)
        score = (weight_sum / max(1, len(lines))) * boost
        osm_id = props.get("@id") or props.get("id")
        scored.append((score, osm_id, lines))

    if not scored:
        return []

    positives = sorted(s[0] for s in scored)
    cutoff = positives[min(len(positives) - 1, int(len(positives) * PCT))]

    scored.sort(key=lambda item: -item[0])
    out = []
    for score, osm_id, lines in scored[:max_streets]:
        demand = min(1.0, score / cutoff) if cutoff > 0 else 0.0
        rounded = [[[round5(point[0]), round5(point[1])] for point in line] for line in lines]
        geometry = (
            {"type": "LineString", "coordinates": rounded[0]}
            if len(rounded) == 1
            else {"type": "MultiLineString", "coordinates": rounded}
        )
        out.append(
            {
                "type": "Feature",
                "properties": {"id": osm_id, "demand": round5(demand)},
                "geometry": geometry,
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--places", required=True, help="areas geojsonseq from osmium export")
    parser.add_argument("--streets", required=True, help="ways geojsonseq from osmium export")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--extract", default="", help="source extract slug, for the manifest")
    parser.add_argument("--max-streets", type=int, default=4000)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    places, counts = build_places(args.places)
    demand = build_demand(args.streets, places, args.max_streets)

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
