// Picks the demo's "Try a trip" examples from the recordings: car trips where the recommended
// route avoids the most compared with the standard one, and a bike and a bus trip.
//   node demo/pick-examples.mjs [recorded folder]   -> writes examples.json there
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = resolve(process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), 'recorded'));
const places = JSON.parse(readFileSync(join(DIR, 'places.json'), 'utf8'));
const rows = [];
for (const name of readdirSync(join(DIR, 'trips'))) {
  const b = JSON.parse(gunzipSync(readFileSync(join(DIR, 'trips', name))));
  const car = b.car['safe-economy'];
  if (car.status !== 200) continue;
  const plan = car.body.plan;
  const standard = plan.candidates.find(c => c.is_standard) || null;
  const w = plan.winner;
  const gain = standard && standard.id !== w.id ? w.safety_score - standard.safety_score : 0;
  const mins = r => r.features?.duration_min ?? r.features?.duration_s / 60 ?? 0;
  rows.push({ from: b.from, to: b.to, gain, winner: w.safety_score, standard: standard?.safety_score, extra: standard ? mins(w) - mins(standard) : 0,
    bike: b.bike.everyday.status === 200 ? b.bike.everyday.body.plan : null, transit: b.transit.status === 200 ? b.transit.body.plan : null });
}
rows.sort((a, b) => b.gain - a.gain);
const used = new Set();
const pick = [];
for (const r of rows) {
  if (pick.length >= 2) break;
  if (used.has(r.from) || used.has(r.to)) continue;
  if (r.gain <= 0) break;
  used.add(r.from); used.add(r.to);
  pick.push({ from: r.from, to: r.to, mode: 'car', note: `By car: the safer route scores ${r.winner} for safety against the standard route's ${r.standard}${r.extra > 0.5 ? `, for about ${Math.round(r.extra)} more minutes` : ''}.` });
}
const bike = rows.filter(r => r.bike && !pick.some(p => p.from === r.from && p.to === r.to)).sort((a, b) => Math.abs(distance(a) - 5) - Math.abs(distance(b) - 5))[0];
if (bike) pick.push({ from: bike.from, to: bike.to, mode: 'bike', note: 'By bike: the lowest-stress streets and trails, with busy crossings marked.' });
const bus = rows.find(r => r.transit && r.transit.winner?.transfers === 0 && r.transit.winner?.duration_min < 45 && !pick.some(p => p.from === r.from && p.to === r.to));
if (bus) pick.push({ from: bus.from, to: bus.to, mode: 'transit', note: 'By bus: walk, ride and walk, with the safest option first.' });
writeFileSync(join(DIR, 'examples.json'), JSON.stringify(pick, null, 1));
console.log(pick.map(p => `${places[p.from].label} → ${places[p.to].label} (${p.mode}): ${p.note}`).join('\n'));

function distance(r) {
  const a = places[r.from], b = places[r.to];
  return Math.hypot((a.lat - b.lat) * 69, (a.lng - b.lng) * 50.5);
}
