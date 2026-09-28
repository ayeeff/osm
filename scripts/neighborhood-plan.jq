# Build the per-extract neighbourhood matrix.
#
# One jq invocation, no shell variables and no herestrings in between: the
# previous version chained four jq calls through <<<"$ALL" carrying ~93 KB of
# JSON, and a failure anywhere in that chain exited 1 with no message at all.
#
# Each output group is one geofabrik extract with its full city list, so the
# build job's matrix fans out per country file instead of per city. Each city is
# pre-joined to "slug:minLng,minLat,maxLng,maxLat" so the build job can read it
# out of an env var and split on the first colon; doing the join here avoids a
# second matrix dimension.
#
# Usage: jq -f neighborhood-plan.jq [--arg slug S] [--arg extract E] registry.json

# "slug:minLng,minLat,maxLng,maxLat" and, when the city has alternates,
# ":fallbackExtract1,fallbackExtract2". The build step splits on the first colon
# for the bbox, then on the first comma to separate any trailing fallback list.
# Keeping it one env var avoids a second matrix dimension.
def citypair:
  "\(.slug):\(.bbox | map(tostring) | join(","))"
  + (if (.fallback // []) | length > 0
     then ":" + (.fallback | join(","))
     else "" end);

# GitHub-hosted runners cap a job at 360 minutes, and each city costs one
# `osmium extract` plus two pyosmium passes. us-west is 328 cities in one
# country file, which does not fit, so each extract is chunked by $per. This
# also spreads the largest country across more runners instead of serialising it.
#
# Usage: jq -f neighborhood-plan.jq [--arg slug S] [--arg extract E] [--argjson per N] registry.json

# Only cities that name a geofabrik extract can be built here. The rest have no
# staged PBF and must not silently fall back to Overpass.
[ .cities[]
  | select(.extract != null and .extract != "")
  | select($slug == "" or .slug == $slug)
  | select($extract == "" or .extract == $extract)
]
| sort_by(.extract)
| group_by(.extract)
| map(
    . as $g
    | [ .[] | citypair ] as $all
    | (($all | length) / $per | ceil) as $n
    | [ range(0; $n) as $i
        | { extract: $g[0].extract, cities: $all[$i * $per : ($i + 1) * $per] } ]
  )
| add // []


