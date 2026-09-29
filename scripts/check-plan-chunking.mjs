// Node mirror of scripts/neighborhood-plan.jq, to check the chunk arithmetic
// against the real registry before the runner has to discover a mistake.
//
// Mirrors:
//   [ .cities[] | select(.extract != null and .extract != "") ]
//   | sort_by(.extract) | group_by(.extract)
//   | map(. as $g | [ .[] | citypair ] as $all
//         | (($all|length)/$per|ceil) as $n
//         | [ range(0;$n) as $i
//             | { extract: $g.extract, cities: $all[$i*$per:($i+1)*$per] } ])
//   | add // []
import fs from 'node:fs';

const PER = Number(process.argv[3] || 40);
const SLUG = process.argv[4] || '';
const EXTRACT = process.argv[5] || '';

const d = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const citypair = (c) => {
  const base = `${c.slug}:${c.bbox.map(String).join(',')}`;
  const fb = c.fallback || [];
  return fb.length ? `${base}:${fb.join(',')}` : base;
};

let sel = d.cities.filter(
  (c) =>
    c.extract != null &&
    c.extract !== '' &&
    (SLUG === '' || c.slug === SLUG) &&
    (EXTRACT === '' || c.extract === EXTRACT)
);

const byExtract = new Map();
for (const c of sel.sort((a, b) => (a.extract < b.extract ? -1 : a.extract > b.extract ? 1 : 0))) {
  if (!byExtract.has(c.extract)) byExtract.set(c.extract, []);
  // Keep the object, not the string: the cluster grouping below still needs
  // `bbox` to measure the union span.
  byExtract.get(c.extract).push(c);
}

const groups = [];
// Mirrors neighborhood-plan.jq: group by extract, then by geographic cluster.
// One cluster per job is the point: the build step cuts the cluster's union bbox
// once from the region file, so a job's cities must be near each other.
for (const [extract, all] of byExtract) {
  const cl = new Map();
  for (const c of all) {
    const k = c.cluster || extract;
    if (!cl.has(k)) cl.set(k, []);
    cl.get(k).push(citypair(c));
  }
  for (const [, cities] of cl) {
    const n = Math.ceil(cities.length / PER);
    for (let i = 0; i < n; i++) {
      groups.push({ extract, cities: cities.slice(i * PER, (i + 1) * PER) });
    }
  }
}

const sizes = groups.map((g) => g.cities.length);
const emitted = groups.reduce((a, g) => a + g.cities.length, 0);
const seen = new Set(groups.flatMap((g) => g.cities));
const perCity = new Map();
for (const g of groups) for (const c of g.cities) perCity.set(c, (perCity.get(c) || 0) + 1);

console.log(`  input cities      : ${sel.length}`);
console.log(`  groups (jobs)     : ${groups.length}`);
console.log(`  cities emitted    : ${emitted}  ${emitted === sel.length ? 'OK' : 'MISMATCH!'}`);
console.log(`  unique cities     : ${seen.size}  ${seen.size === sel.length ? 'OK' : 'DUPLICATES!'}`);
console.log(`  any city twice    : ${[...perCity.values()].some((v) => v > 1)}`);
// The per-city cost: osmium extract re-reads the whole input per bbox, so what
// matters is how large the input is, not how many cities. Estimate the union span
// of each job, which is the size of the coarse cut the workflow makes once.
const spans = groups.map((g) => {
  const pairs = g.cities.map((p) => p.split(':')[1].split(',').map(Number));
  const lons = pairs.flatMap((p) => [p[0], p[2]]);
  const lats = pairs.flatMap((p) => [p[1], p[3]]);
  return {
    n: g.cities.length,
    lon: Math.max(...lons) - Math.min(...lons),
    lat: Math.max(...lats) - Math.min(...lats),
  };
});
spans.sort((a, b) => b.lon * b.lat - a.lon * a.lat);

console.log(`  jobs (groups)      : ${groups.length}`);
console.log(`  widest union       : ${spans[0].lon.toFixed(1)} x ${spans[0].lat.toFixed(1)} deg`);
console.log(`  median union       : ${spans[Math.floor(spans.length / 2)].lon.toFixed(1)} x ${spans[Math.floor(spans.length / 2)].lat.toFixed(1)} deg`);
console.log(`  largest job        : ${Math.max(...sizes)} cities  ${Math.max(...sizes) <= PER ? 'OK' : 'OVER PER'}`);
console.log(`  smallest job      : ${Math.min(...sizes)} cities`);
console.log(`  any empty job     : ${sizes.some((v) => v === 0)}`);

// The per-city cost: osmium extract + two pyosmium passes. us-west is the
// worst case, so estimate against the 360 minute hosted-runner cap.
const big = groups.slice().sort((a, b) => b.cities.length - a.cities.length)[0];
console.log(`  biggest group     : ${big.extract} with ${big.cities.length} cities`);

// Group ordering must be stable so a re-run produces the same matrix.
const order = groups.map((g) => `${g.extract}:${g.cities.length}`).join(',');
console.log(`  matrix fingerprint: ${order.length > 90 ? order.slice(0, 90) + '...' : order}`);
