# OSM → 2D city map data

Builds the `places.json` + `demand-streets.json` datasets for the site's
[2D dark city map style](https://github.com/ayeeff/astrogl/blob/preview/template/map-style-2d.md)
from OSM, using the planet/country extracts already staged in Cloudflare R2 —
no Overpass, and no bulk bytes over any local link.

## How it works

```
geo-datalake/sources/osm/<country>-latest.osm.pbf     (geofabrik, ~1-2 GB)
        │  streamed, never stored locally
        ▼
  osmium extract -b <city bbox>        →  work/city.osm.pbf
  osmium export  (areas | ways)        →  work/places.geojsonseq, work/streets.geojsonseq
        ▼
  extract_city_2d_data.py              →  places.json, demand-streets.json
        ▼
  geo-datalake/sources/osm/places/<slug>/     (datalake copy)
  globe/data/<slug>-2d/                       (what the site serves)
```

`atlas-2d-worker` (`ayeeff/astrogl`, `workers/atlas-2d/`) reads the
`globe/data/<slug>-2d/` keys through the site Worker and only falls back to
Overpass for cities that have no extract here.

The scoring constants in `extract_city_2d_data.py` are a deliberate port of
`workers/atlas-2d/src/assemble.js` — both must produce the same shape, so a
change in one needs the same change in the other.

## Why not Overpass

A city needs ~40 Overpass calls at 2×2 tiling, and the public gateways
(`overpass-api.de`) throttle hard — a cold city took 1–2 hours and needed three
bug fixes to survive. The per-country extracts are a single stream and
`osmium extract` is a local scan, so a city lands in minutes.

## Run it

```bash
# a single city
gh workflow run osm-city-2d-data.yml -f slug=beijing

# everything with an extract
gh workflow run osm-city-2d-data.yml

# what the last run produced
gh run list --workflow osm-city-2d-data.yml
```

## Secrets

| Secret | Purpose |
|---|---|
| `R2_ACCESS_KEY_ID` | R2 S3 token (writes `geo-datalake` **and** `globe`) |
| `R2_SECRET_ACCESS_KEY` | ditto |
| `CF_ACCOUNT_ID` | R2 S3 endpoint host |

## Adding a city

1. Add it to `cities.json` with its bbox (`layer/anchor-pmtiles-manifest.json` in
   astrogl is the canonical bbox source) and the `extract` slug it falls inside.
   Omit `extract` if no geofabrik extract covers it — it will keep using
   Overpass.
2. Run the workflow for that slug.
