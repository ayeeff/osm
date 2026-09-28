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

def citypair: "\(.slug):\(.bbox | map(tostring) | join(","))";

# Only cities that name a geofabrik extract can be built here. The rest have no
# staged PBF and must not silently fall back to Overpass.
[ .cities[]
  | select(.extract != null and .extract != "")
  | select($slug == "" or .slug == $slug)
  | select($extract == "" or .extract == $extract)
]
| sort_by(.extract)
| group_by(.extract)
| map({ extract: .[0].extract, cities: [ .[] | citypair ] })
