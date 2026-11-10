// =====================================================================
// Records the routes server's answers between the demo's 12 sample places, for the static
// portfolio demo (demo/build-portfolio-demo.mjs). One gzipped bundle per ordered pair:
// every car profile, leave times, detour limits, bike, walk and bus trips, and the stop
// signs and lights along each car route. Resumable: finished pairs are skipped.
//
// Record from a server with ROUTES_MAP=osm and no Google, TomTom or 511 keys, so that only
// OpenStreetMap, public crash data, WZDx work zones and the bus schedule go into the
// recordings (their terms allow it; Google's and TomTom's don't). For example:
//
//   docker run -d --name routes-demo-recorder -p 127.0.0.1:3144:3143 --env-file backend/.env \
//     -e GOOGLE_MAPS_API_KEY= -e WI511_API_KEY= -e TOMTOM_API_KEY= -e ROUTES_CACHE_MODE=off \
//     -e ROUTES_ROAD_EVENT_SOURCES=wzdx -v <repo>/backend/data:/srv/routes/backend/data:ro safe-route-planner
//   node demo/record-trips.mjs demo/recorded 4
//
//   node demo/record-trips.mjs <out dir> [concurrency] [limitPairs]
// =====================================================================
import { mkdirSync, existsSync, writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

const API = 'http://127.0.0.1:3144';
const OUT = process.argv[2];
const CONCURRENCY = Number(process.argv[3] || 4);
const LIMIT = Number(process.argv[4] || Infinity);
mkdirSync(join(OUT, 'trips'), { recursive: true });

export const PLACES = [
  { label: 'Fiserv Forum', lat: 43.0451, lng: -87.9172 },
  { label: 'Bay View', lat: 42.9978, lng: -87.8954 },
  { label: 'American Family Field', lat: 43.028, lng: -87.9712 },
  { label: 'Bayshore', lat: 43.118, lng: -87.91 },
  { label: 'Lakefront Brewery', lat: 43.0546, lng: -87.9055 },
  { label: 'Mitchell Park Domes', lat: 43.0255, lng: -87.9455 },
  { label: 'Harley-Davidson Museum', lat: 43.0317, lng: -87.9165 },
  { label: 'Mayfair Mall', lat: 43.0605, lng: -88.048 },
  { label: 'State Fair Park', lat: 43.0225, lng: -88.013 },
  { label: 'Shorewood', lat: 43.089, lng: -87.887 },
  { label: 'Southridge Mall', lat: 42.946, lng: -88.01 },
  { label: 'UW-Milwaukee', lat: 43.0766, lng: -87.8814 },
];
const CAR_PROFILES = ['safe-economy', 'fastest', 'highway-averse', 'scenic', 'calm', 'eco', 'new-driver'];
const BIKE_PROFILES = ['everyday', 'family', 'confident', 'ebike'];
const WALK_PROFILES = ['everyday', 'careful', 'stepfree'];

async function post(path, body) {
  for (let attempt = 1; ; attempt++) {
    try {
      const r = await fetch(API + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const text = await r.text();
      let json; try { json = JSON.parse(text); } catch { json = { detail: text.slice(0, 300) }; }
      if (r.status >= 500 && r.status !== 503 && attempt < 3) throw new Error(`HTTP ${r.status}`);
      return { status: r.status, body: json };
    } catch (e) {
      if (attempt >= 3) return { status: 599, body: { detail: String(e.message) } };
      await new Promise(res => setTimeout(res, 2000 * attempt));
    }
  }
}
const pt = p => ({ lat: p.lat, lng: p.lng });
const car = (a, b, extra = {}) => ({ origin: pt(a), destination: pt(b), stops: [], profile: 'safe-economy', country_code: 'US', live_traffic: false, use_router: true, road_events: 'wzdx', departure_offset_min: 0, counted: false, ...extra });
export const hashLine = s => createHash('sha1').update(s).digest('hex').slice(0, 16);

async function recordPair(i, j) {
  const a = PLACES[i], b = PLACES[j];
  const bundle = { from: i, to: j, recordedAt: new Date().toISOString(), car: {}, carLeave: {}, carExtra: {}, leaveTimes: null, bike: {}, walk: {}, transit: null, controls: {} };
  for (const p of CAR_PROFILES) bundle.car[p] = await post('/route', car(a, b, { profile: p }));
  for (const m of [15, 30, 60]) bundle.carLeave[m] = await post('/route', car(a, b, { departure_offset_min: m }));
  for (const x of [5, 10, 15]) bundle.carExtra[x] = await post('/route', car(a, b, { max_extra_min: x }));
  bundle.leaveTimes = await post('/leave-times', car(a, b));
  for (const p of BIKE_PROFILES) bundle.bike[p] = await post('/bike-route', { origin: pt(a), destination: pt(b), stops: [], profile: p, departure_offset_min: 0 });
  for (const p of WALK_PROFILES) bundle.walk[p] = await post('/walk-route', { origin: pt(a), destination: pt(b), stops: [], profile: p, departure_offset_min: 0 });
  bundle.transit = await post('/transit-route', { origin: pt(a), destination: pt(b), departure_offset_min: 0 });
  // Stop signs and lights along every car route drawn (the 3D drive preview asks for them)
  const lines = new Set();
  for (const answer of [...Object.values(bundle.car), ...Object.values(bundle.carLeave), ...Object.values(bundle.carExtra)]) {
    for (const r of answer.body?.plan?.candidates || []) if (r.encoded_polyline) lines.add(r.encoded_polyline);
  }
  for (const line of lines) {
    const res = await post('/road-controls', { encoded_polyline: line });
    if (res.status === 200) bundle.controls[hashLine(line)] = res.body;
  }
  writeFileSync(join(OUT, 'trips', `${i}-${j}.json.gz`), gzipSync(JSON.stringify(bundle), { level: 9 }));
  return bundle;
}

const pairs = [];
for (let i = 0; i < PLACES.length; i++) for (let j = 0; j < PLACES.length; j++) if (i !== j) pairs.push([i, j]);
const todo = pairs.filter(([i, j]) => !existsSync(join(OUT, 'trips', `${i}-${j}.json.gz`))).slice(0, LIMIT);
const started = Date.now();
let done = 0;
async function worker() {
  while (todo.length) {
    const [i, j] = todo.shift();
    const b = await recordPair(i, j);
    done++;
    const bad = [...Object.entries(b.car), ...Object.entries(b.bike), ...Object.entries(b.walk)].filter(([, r]) => r.status !== 200 && r.status !== 422).map(([k, r]) => `${k}:${r.status}`);
    console.log(`${done} ${i}-${j} ${((Date.now() - started) / 1000).toFixed(0)}s transit:${b.transit.status} leave:${b.leaveTimes.status} controls:${Object.keys(b.controls).length}${bad.length ? ' BAD ' + bad.join(',') : ''}`);
  }
}
await Promise.all(Array.from({ length: CONCURRENCY }, worker));

// The places, profiles and bus lines the page asks for once
const get = async p => (await fetch(API + p)).json();
writeFileSync(join(OUT, 'places.json'), JSON.stringify(PLACES));
writeFileSync(join(OUT, 'profiles.json'), JSON.stringify(await get('/profiles')));
writeFileSync(join(OUT, 'bus-routes.json.gz'), gzipSync(JSON.stringify(await get('/bus-routes')), { level: 9 }));
console.log('done in', ((Date.now() - started) / 1000).toFixed(0), 's');
