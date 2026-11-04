// Keeps the web map working when the network drops, as an expo hall's Wi-Fi does.
//
// The page's own files and its server's settings are kept from the first visit
// and served when the server can't be reached (on the expo computer the server
// is local, so planning still works). The free map services' images are kept as
// they're viewed, up to MAX_TILES, and asked for again after TILE_MAX_AGE: their
// terms allow keeping what's been viewed, never downloading areas ahead. Google's
// map isn't kept (its terms don't allow it), and neither is live traffic.

const PAGE = "page-v8"; // a new name when the page's files change: the old copy is dropped
const TILES = "tiles-v1";
const MAX_TILES = 4000; // about 60 MB: the metro at the zooms people use, several times over
const TILE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const TILE_HOSTS = /(^|\.)(tile\.openstreetmap\.org|arcgisonline\.com|opentopomap\.org|openrailwaymap\.org|tile-cyclosm\.openstreetmap\.fr)$/;

// Everything the page needs to open. A missing one (config.js, say) doesn't stop the rest.
const PAGE_FILES = [
  "./",
  "styles.css",
  "config.js",
  "app.js",
  "core.js",
  "editing.js",
  "results.js",
  "drive.js",
  "drive3d.js",
  "layers.js",
  "trips.js",
  "expo.js",
  "maps.js",
  "i18n.js",
  "bike.js",
  "transit.js",
  "control.html",
  "control.js",
  "stats.html",
  "stats.js",
  "answers.html",
  "answers.js",
  "game.html",
  "game.js",
  "manifest.json",
  "results.json",
  "icons/icon-192.png",
  "vendor/leaflet/leaflet.js",
  "vendor/leaflet/leaflet.css",
  "vendor/leaflet/images/layers.png",
  "vendor/leaflet/images/layers-2x.png",
  "vendor/markercluster/leaflet.markercluster.js",
  "vendor/markercluster/MarkerCluster.css",
  "vendor/qrcode-generator/qrcode.js",
  "vendor/maplibre/maplibre-gl.js",
  "vendor/maplibre/maplibre-gl.css",
  "app-config",
  "profiles",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(PAGE);
      await Promise.all(PAGE_FILES.map((file) => cache.add(file).catch(() => {})));
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys()) if (name !== PAGE && name !== TILES) await caches.delete(name);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return; // routes are planned fresh, or not at all
  const url = new URL(request.url);
  if (TILE_HOSTS.test(url.hostname)) event.respondWith(tile(request));
  else if (url.origin === self.location.origin && !url.pathname.startsWith("/traffic-tiles/")) event.respondWith(page(request));
});

// The network first, so a changed page shows at once; what was kept when it doesn't answer.
async function page(request) {
  const cache = await caches.open(PAGE);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  } catch (error) {
    // The page itself, opened from a link with a code or a trip in it, is the same page. ignoreVary:
    // the server's answers vary by Origin, which modules send and the copies made at install didn't.
    const kept = await cache.match(request, { ignoreSearch: request.mode === "navigate", ignoreVary: true });
    if (kept) return kept;
    throw error;
  }
}

let keptSinceTrim = 0;

// A kept map image while it's fresh; otherwise the network, and the old one if that fails.
async function tile(request) {
  const cache = await caches.open(TILES);
  const kept = await cache.match(request, { ignoreVary: true });
  if (kept && Date.now() - Number(kept.headers.get("x-kept-at")) < TILE_MAX_AGE_MS) return kept;
  try {
    const response = await fetch(request);
    // Only images the page may read (CORS): an opaque one would take megabytes of the browser's storage.
    if (response.ok && response.type === "cors") await keep(cache, request, response.clone());
    return response;
  } catch (error) {
    if (kept) return kept;
    throw error;
  }
}

async function keep(cache, request, response) {
  const headers = new Headers(response.headers);
  headers.set("x-kept-at", String(Date.now()));
  await cache.put(request, new Response(await response.blob(), { status: response.status, headers }));
  if (++keptSinceTrim < 100) return;
  keptSinceTrim = 0;
  const keys = await cache.keys(); // oldest first
  await Promise.all(keys.slice(0, Math.max(0, keys.length - MAX_TILES)).map((key) => cache.delete(key)));
}
