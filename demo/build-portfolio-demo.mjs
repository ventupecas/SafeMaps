// =====================================================================
// Builds the static Safe Route Planner demo for the portfolio site (GitHub Pages).
//
//   node demo/build-portfolio-demo.mjs [recorded folder] [output folder]
//
// Defaults: demo/recorded (made by demo/record-trips.mjs) and ../Portfolio-Site/safemaps.
// It copies the web page in frontend/, leaves out the map key file, the offline
// service worker and the expo-only pages (stats, answers, control), adds
// demo/static/demo.js (which answers the page's server requests with the
// recorded answers) and the recordings, and checks that no key or secret
// went along.
// =====================================================================

import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const FRONTEND = join(ROOT, 'frontend');
const RECORDED = resolve(process.argv[2] || join(ROOT, 'demo', 'recorded'));
const OUT = resolve(process.argv[3] || join(ROOT, '..', 'Portfolio-Site', 'safemaps'));

// Never copied: the browser's map key, the offline copy, and the expo's own screens
const SKIP = new Set(['config.js', 'config.example.js', 'sw.js', 'stats.html', 'stats.js', 'answers.html', 'answers.js', 'control.html', 'control.js']);
const TEXT = new Set(['.js', '.css', '.html', '.json', '.md', '.svg', '.txt']);

if (!existsSync(join(RECORDED, 'trips'))) throw new Error(`No recorded trips in ${RECORDED}: run demo/record-trips.mjs first.`);

function patch(text, from, to, file) {
  if (!text.includes(from)) throw new Error(`${file}: the text to patch was not found: ${from.slice(0, 90)}`);
  return text.replace(from, to);
}
function edit(file, ...pairs) {
  const path = join(OUT, file);
  let text = readFileSync(path, 'utf8').replace(/\r\n/g, '\n');
  for (let i = 0; i < pairs.length; i += 2) text = patch(text, pairs[i], pairs[i + 1], file);
  writeFileSync(path, text);
}
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out); else out.push(p);
  }
  return out;
}

// ---------------------------------------------------------------
// Copy
// ---------------------------------------------------------------
if (existsSync(OUT)) rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });
for (const name of readdirSync(FRONTEND)) {
  if (!SKIP.has(name)) cpSync(join(FRONTEND, name), join(OUT, name), { recursive: true });
}
mkdirSync(join(OUT, 'demo', 'data'), { recursive: true });
cpSync(join(ROOT, 'demo', 'static', 'demo.js'), join(OUT, 'demo', 'demo.js'));
for (const name of ['trips', 'profiles.json', 'bus-routes.json.gz']) cpSync(join(RECORDED, name), join(OUT, 'demo', 'data', name), { recursive: true });
const places = JSON.parse(readFileSync(join(RECORDED, 'places.json'), 'utf8'));
const examples = JSON.parse(readFileSync(join(RECORDED, 'examples.json'), 'utf8'));

// ---------------------------------------------------------------
// The planner: the stand-in server first, a welcome, and the parts that need the live server
// ---------------------------------------------------------------
const link = ({ from, to, mode }) => {
  const a = places[from], b = places[to];
  const q = new URLSearchParams({ from: `${a.lat},${a.lng}`, fromName: a.label, to: `${b.lat},${b.lng}`, toName: b.label });
  q.set('mode', mode || 'car'); // always: otherwise the mode last used in this browser applies
  return `./?${q}`;
};
const exampleItems = examples.map(e =>
  `<li><a href="${link(e).replace(/&/g, '&amp;')}">${places[e.from].label} → ${places[e.to].label}</a><span>${e.note}</span></li>`).join('\n            ');

const DEMO_HEAD = `<script src="demo/demo.js"></script>
    <style>
      .chip--answers, .chip--stats, .chip--control, #bike-stress-switch, details.advanced { display: none !important; }
      #demo-toast { position: fixed; left: 50%; bottom: 18px; transform: translate(-50%, 20px); max-width: min(560px, calc(100vw - 32px));
        background: #1f2a33; color: #fff; padding: 10px 16px; border-radius: 12px; font: 14px/1.45 system-ui, sans-serif;
        box-shadow: 0 6px 24px rgba(0,0,0,.25); opacity: 0; pointer-events: none; transition: opacity .25s, transform .25s; z-index: 10000; }
      #demo-toast.demo-toast--on { opacity: 1; transform: translate(-50%, 0); }
      .demo-pill { position: fixed; right: 12px; bottom: 12px; z-index: 9000; padding: 5px 12px; border-radius: 999px;
        background: rgba(20,28,34,.82); color: #fff; font: 12px/1.4 system-ui, sans-serif; text-decoration: none; }
      .demo-pill:hover, .demo-pill:focus-visible { background: #000; }
      @media (max-width: 760px) { .demo-pill { position: static; display: block; border-radius: 0; text-align: center; padding: 8px 16px; font-size: 13px; } }
      #demo-intro { max-width: min(560px, calc(100vw - 32px)); border: 0; border-radius: 16px; padding: 22px 24px; box-shadow: 0 12px 40px rgba(0,0,0,.3);
        font: 15px/1.5 system-ui, sans-serif; color: #1f2a33; }
      #demo-intro::backdrop { background: rgba(10,20,30,.45); }
      #demo-intro h2 { margin: 0 0 6px; font-size: 20px; color: #0b7a53; }
      #demo-intro p { margin: 8px 0; }
      #demo-intro ul { list-style: none; padding: 0; margin: 10px 0 14px; display: grid; gap: 6px; }
      #demo-intro li a { font-weight: 600; color: #0b7a53; }
      #demo-intro li span { display: block; font-size: 13px; color: #55636f; }
      #demo-intro .demo-note { font-size: 13px; color: #55636f; }
      #demo-intro button { font: inherit; font-weight: 600; padding: 8px 16px; border-radius: 10px; border: 0; background: #0b7a53; color: #fff; cursor: pointer; }
    </style>`;

const DEMO_BODY = `<a class="demo-pill" href="../" title="Back to the portfolio">← Portfolio · demo with recorded routes</a>
    <dialog id="demo-intro" aria-labelledby="demo-intro-title">
      <h2 id="demo-intro-title">Safe Route Planner</h2>
      <p>Most map apps pick the <em>fastest</em> route. This one picks the <strong>safest</strong> one first, then the one that uses the least fuel, then the quickest.
        It counts what makes a drive risky: turns across traffic at busy or crash-prone junctions, school zones, railroad crossings, closures and driving into a low sun, and it shows what the safer route avoids compared with the standard one.</p>
      <p>It plans car, bike, walking and bus trips around Milwaukee on its own router over OpenStreetMap roads, using public crash data and work zones.</p>
      <p><strong>Try a trip:</strong></p>
      <ul>
            ${exampleItems}
      </ul>
      <p class="demo-note">This copy runs without its server: routes between 12 sample places were recorded from the real engine, so points you set move to the nearest sample place. The map, layers, directions and the 3D drive preview run live in your browser.</p>
      <form method="dialog"><button type="submit">Start exploring</button></form>
    </dialog>
    <script>
      (function () {
        var params = new URLSearchParams(location.search);
        var seen = false;
        try { seen = sessionStorage.getItem("safemaps-intro") === "seen"; sessionStorage.setItem("safemaps-intro", "seen"); } catch (e) {}
        var box = document.getElementById("demo-intro");
        if (!params.has("from") && !seen && box && box.showModal) box.showModal();
      })();
    </script>`;

edit('index.html',
  '<link rel="stylesheet" href="styles.css" />', `<link rel="stylesheet" href="styles.css" />\n    ${DEMO_HEAD}`,
  '<title>Safe Route Planner</title>', '<title>Safe Route Planner Demo</title>\n    <meta name="description" content="A route planner that puts safety first, then fuel, then time. A portfolio demo with routes recorded from the real engine.">',
  'Right-click (or\n            press and hold) to add a stop. ', '',
  ', S a stop.', '.',
  '<body data-mode="car">', `<body data-mode="car">\n    ${DEMO_BODY}`,
  // The expo's own screens aren't copied, so their links go
  '            <a class="chip chip--answers" href="answers.html">Visitors\' answers</a>\n            <a class="chip chip--stats" href="stats.html">Stats</a>\n            <a class="chip chip--control" href="control.html">Control</a>\n', '');
edit('index.html', '<p class="hint">\n            Search for a place, or click the map:', '<p class="hint">\n            Search for one of the 12 sample places, or click near one on the map:');

// The game and the model city ask the server too
for (const page of ['game.html', 'city.html']) {
  edit(page, '<link rel="icon" href="icons/icon.svg" type="image/svg+xml" />', '<link rel="icon" href="icons/icon.svg" type="image/svg+xml" />\n    <script src="demo/demo.js"></script>');
}

// No map key file here (the OpenStreetMap map needs none), so the page doesn't ask for it
for (const page of ['index.html', 'game.html']) {
  edit(page, '    <script src="config.js" onerror="window.ROUTES_CONFIG_MISSING = true"></script>\n', '');
}
// The game pairs only the recorded places
edit('game.js', 'const pool = [...PLACES, ...DEMO.trips.flatMap((trip) => [trip.from, trip.to])];', 'const pool = [...PLACES];');

// No offline copy on the static host
edit('app.js', '  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch((error) => console.warn("No offline copy:", error));\n', '');

// Points move to the nearest sample place; stops and following the device need the live server
edit('editing.js',
  'export async function setPoint(kind, point, { fromSearch = false, onMap = false } = {}) {\n',
  'export async function setPoint(kind, point, { fromSearch = false, onMap = false } = {}) {\n  if (window.SAFEMAPS_DEMO) point = window.SAFEMAPS_DEMO.snap(point);\n',
  'export async function addStop({ point = null, index = state.stops.length, id = state.nextStopId++ } = {}) {\n',
  'export async function addStop({ point = null, index = state.stops.length, id = state.nextStopId++ } = {}) {\n  if (window.SAFEMAPS_DEMO) return window.SAFEMAPS_DEMO.say("Stops need the live routing server. This demo plans trips straight between its 12 sample places.");\n',
  'export function addStopOnMap(point) {\n  if (!mapTouchAllowed()) return;\n',
  'export function addStopOnMap(point) {\n  if (!mapTouchAllowed()) return;\n  if (window.SAFEMAPS_DEMO && state.origin && state.destination) return window.SAFEMAPS_DEMO.say("Stops need the live routing server. This demo plans trips straight between its 12 sample places.");\n');
edit('drive.js',
  'export async function startDriving() {\n',
  'export async function startDriving() {\n  if (window.SAFEMAPS_DEMO) return window.SAFEMAPS_DEMO.say("Following your location needs the live routing server. Press Preview the drive to watch this trip played back, with spoken directions.", 8000);\n');

// The add-to-home-screen manifest stays inside this folder
const manifest = JSON.parse(readFileSync(join(OUT, 'manifest.json'), 'utf8'));
if (manifest.start_url !== './' || manifest.scope !== './') throw new Error('manifest.json: start_url and scope should be ./');

// ---------------------------------------------------------------
// Checks: no key or secret, no absolute links that would leave the folder
// ---------------------------------------------------------------
const problems = [];
const SECRET = /AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|api[_-]?key["']?\s*[:=]\s*["'][A-Za-z0-9_-]{16,}|ROUTES_ACCESS_CODE=|ROUTES_ADMIN_CODE=/;
for (const file of walk(OUT)) {
  if (!TEXT.has(extname(file)) || file.includes(join('vendor', ''))) continue;
  const text = readFileSync(file, 'utf8');
  const m = text.match(SECRET);
  if (m) problems.push(`${relative(OUT, file)}: looks like a key: ${m[0].slice(0, 12)}…`);
  for (const a of text.matchAll(/(?:href|src)="(\/[^"/][^"]*)"/g)) problems.push(`${relative(OUT, file)}: absolute link ${a[1]}`);
}
for (const gz of walk(join(OUT, 'demo', 'data'))) {
  if (!gz.endsWith('.gz')) continue;
  const { gunzipSync } = await import('node:zlib');
  const text = gunzipSync(readFileSync(gz)).toString('utf8');
  const m = text.match(SECRET) || text.match(/"google":\{"live_requests":[1-9]/);
  if (m) problems.push(`${relative(OUT, gz)}: ${m[0].slice(0, 30)}`);
}
if (problems.length) {
  console.error(`✋ ${problems.length} problem(s):\n${problems.slice(0, 30).join('\n')}`);
  process.exit(1);
}
const files = walk(OUT);
const size = files.reduce((s, f) => s + statSync(f).size, 0);
console.log(`✅ Built ${files.length} files (${(size / 1e6).toFixed(1)} MB) in ${OUT}; no keys found.`);
