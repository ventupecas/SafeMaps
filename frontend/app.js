// The web map for the routing engine: pick a trip, draw the ranked routes, explain the choice.
//
// All routing, scoring and traffic lookups happen in the backend (POST /route).
// This page only collects the trip, draws what comes back and explains the
// ranking. The map itself is Google's or OpenStreetMap's, as the server says
// (ROUTES_MAP); maps.js hides which.
//
// While driving, the page follows the device's location: it tracks progress
// along the chosen route, switches to another drawn route if the driver takes
// it, and asks the server for a new route (starting the way the car is going)
// after leaving them all.
//
// Nothing on the page names the map provider; Google's logo and terms on its
// map, and the credits on either map, are required and stay.

import { bearing, createMapView, decodePolyline, distanceM, loadScript } from "./maps.js";

const MILWAUKEE = { lat: 43.0389, lng: -87.9065 };
const KM_TO_MI = 0.621371;
const M_PER_MI = 1609.344;
const LITERS_PER_GALLON = 3.78541;
const M_PER_DEG_LAT = 111320;
const NAV = {
  onRouteM: 40, // within this of a route counts as driving it (GPS error included)
  offRouteFixes: 2, // consecutive fixes off every route before asking for a new one
  rerouteGapMs: 20000, // at most one new route per 20 s: each one costs route data requests
  arrivedM: 40,
  worstAccuracyM: 60, // fixes less precise than this don't judge the route
  headingFromM: 10, // without a GPS heading, the bearing over this distance is the direction of travel
  followZoom: 16,
  announceFarM: 400, // spoken directions: "In a quarter mile, ..." this far before a turn
  announceNearM: 60, // and the turn itself this close
};
const COLORS = {
  winner: "#0b7a53",
  standard: "#4a5a6a",
  alternative: "#a9b2bc",
  closed: "#202124",
  lanes: "#e37400",
  slow: "#f2a600",
  jam: "#d93025",
  hazard: "#7b4fd6",
};
const CO2_KG_PER_GALLON = 8.887; // burning a gallon of gasoline (US EPA)
const MAX_STOPS = 3;
const LAYERS_KEY = "routes-layers"; // the map layers chosen, remembered in this browser
// Demo mode: after DEMO.idleMs with no one touching the page, it plans these trips in turn.
const DEMO = {
  idleMs: 45000,
  showMs: 20000,
  trips: [
    { from: { label: "Fiserv Forum", lat: 43.0451, lng: -87.9172 }, to: { label: "Bay View", lat: 42.9978, lng: -87.8954 } },
    { from: { label: "Milwaukee Public Market", lat: 43.035, lng: -87.9086 }, to: { label: "Wauwatosa Village", lat: 43.0494, lng: -88.0076 } },
    { from: { label: "UW-Milwaukee", lat: 43.0766, lng: -87.8814 }, to: { label: "Mitchell Airport", lat: 42.9476, lng: -87.8966 } },
    { from: { label: "Marquette University", lat: 43.0383, lng: -87.9286 }, to: { label: "Milwaukee County Zoo", lat: 43.032, lng: -88.0405 } },
  ],
};
// Where closures and slowdowns came from, by the server's source names.
const EVENT_SOURCES = {
  wzdx: "Wisconsin work zones (WZDx)",
  wi511: "Wisconsin 511",
  tomtom: "TomTom traffic",
  google: "traffic speeds along the routes",
  apify: "traffic tiles",
};
const QR_LIBRARY = "https://unpkg.com/qrcode-generator@1.4.4/qrcode.js";
const NARROW = "(max-width: 760px)"; // the phone layout in styles.css
const PROFILE_LABELS = {
  "safe-economy": "Safe & economical (recommended)",
  fastest: "Fastest",
  "highway-averse": "Avoid highways",
  scenic: "Scenic",
  calm: "Calm: extra wary of risky turns",
  eco: "Eco: least fuel",
  "new-driver": "New driver: avoids freeways and risky turns",
};

const state = {
  view: null, // the map (see maps.js)
  mapKind: "google", // or "osm": no Google at all; traffic from 511 and TomTom
  origin: null, // { lat, lng, label, countryCode }
  destination: null,
  pins: { origin: null, destination: null },
  undo: [], // changes made on the map, newest last: { kind, what, undo() }
  baseMap: "roadmap", // the map's look: "roadmap", "satellite" or "terrain"
  buses: { available: false, data: null, lines: [] }, // the Transit layer's bus routes (OpenStreetMap map)
  findTicket: 0, // the latest route request: answers to older ones are dropped
  refreshTimer: null, // planning the trip again after a change on the map
  noticeTimer: null, // hides a mapNotice
  stops: [], // places to stop at on the way: { id, point, pin }, in order
  nextStopId: 1,
  fuelPrice: 3.25, // US dollars a gallon (the server's ROUTES_FUEL_PRICE)
  demo: { on: false, running: false, index: 0, idleTimer: null, stepTimer: null },
  routeLines: [], // { id, lines: [line handle] }
  turnMarkers: [],
  closures: [], // closures and lane closures from the last trip
  closureLines: [], // ...drawn, when "Show closures" is on
  closureMarkers: null, // their markers, a group that clusters where crowded
  trafficEvents: [], // slow and jammed stretches (511 travel times, TomTom's jams) from the last trip
  trafficShapes: [], // ...drawn, when the Traffic layer is on and the map has no traffic layer of its own
  plan: null, // the last plan from the server
  paths: {}, // route id -> { points: [{ lat, lng }], cumulative: [m], lengthM, minutes, label }
  activeRouteId: null, // the route being followed: the winner, or one the driver picked
  nav: {
    active: false,
    watchId: null,
    car: null, // the location marker
    carArrow: null, // its direction-of-travel arrow
    following: true, // the map moves with the car until the driver drags it
    fix: null, // last position
    anchor: null, // where the current heading was measured from
    heading: null, // degrees clockwise from north, once known
    offRoute: 0,
    rerouting: false,
    lastRerouteAt: 0,
    wakeLock: null,
    spoken: new Set(), // directions already announced, so each is said once (far and near)
    stopsLeft: [], // the trip's stops not reached yet, while driving
  },
};

const $ = (id) => document.getElementById(id);

boot();

async function boot() {
  loadProfiles();
  wireControls();

  let trafficTiles = false;
  try {
    const appConfig = await (await fetch("/app-config")).json();
    state.mapKind = appConfig.map;
    trafficTiles = Boolean(appConfig.traffic_tiles);
    state.buses.available = Boolean(appConfig.bus_routes);
    if (Number.isFinite(appConfig.fuel_price)) state.fuelPrice = appConfig.fuel_price;
  } catch {
    state.mapKind = "google"; // an older server: it only knew Google's map
  }
  document.body.classList.toggle("map-osm", state.mapKind === "osm"); // shows the .osm-only credits
  const config = window.ROUTES_CONFIG || {};
  if (state.mapKind === "google" && (window.ROUTES_CONFIG_MISSING || !config.mapsApiKey)) {
    showSetup();
    return;
  }
  if (state.mapKind === "osm") useOsmControls();
  else $("road-events").querySelector('option[value="tomtom"]').remove(); // TomTom's traffic is for the other map
  state.view = createMapView(state.mapKind, {
    mapsApiKey: config.mapsApiKey,
    mapId: config.mapId,
    trafficTiles: trafficTiles ? trafficTileUrl() : null,
    onAuthFailure: () =>
      showSetup("The map key was rejected. Check which websites it allows and that the map service is enabled for it."),
  });
  try {
    await state.view.load();
  } catch (error) {
    showSetup(error.message);
    return;
  }
  await initMap();
  restoreLayers();
  openTripFromLink();
  setUpDemo();
}

// The OpenStreetMap map has no Google behind it: no Google live traffic or traffic
// layer, and no closure sources that come from Google.
function useOsmControls() {
  $("live-traffic").checked = false;
  $("live-traffic").closest("label").hidden = true;
  // The Traffic layer shows TomTom's traffic map, or without it draws the measured slowdowns.
  $("traffic-layer").checked = false;
  for (const source of ["google", "apify"]) $("road-events").querySelector(`option[value="${source}"]`).remove();
}

// TomTom's traffic map comes through our server, which holds the key. A map layer
// can't send headers, so a shared demo's access code rides in the address.
function trafficTileUrl() {
  const code = accessCode();
  return `/traffic-tiles/{z}/{x}/{y}.png${code ? `?code=${encodeURIComponent(code)}` : ""}`;
}

async function initMap() {
  await state.view.create($("map"), {
    center: MILWAUKEE,
    zoom: 11,
    onClick: (point) => {
      if (!mapTouchAllowed()) return;
      // First click sets the start; after that, clicks move the destination.
      const kind = state.origin ? "destination" : "origin";
      changeOnMap(kind, unnamed(point));
    },
    onContextMenu: addStopOnMap,
    onDragStart: () => {
      if (!state.nav.active) return;
      state.nav.following = false; // the driver is looking around; "Re-center" resumes
      $("recenter").hidden = false;
    },
  });
  await Promise.all(
    ["origin", "destination"].map((kind) =>
      state.view.search($(`${kind}-search`), kind === "origin" ? "Where from?" : "Where to?", (place) =>
        setPoint(kind, place, { fromSearch: true }),
      ),
    ),
  );
  state.closureMarkers = state.view.markerGroup();
}

// ---- Trip input

// fromSearch: picked in the search box, which already shows its name. A map click or a
// location is spelled out under the box instead, and clears the box (on the OpenStreetMap map).
// onMap: a change "Undo" can take back; any other change to the point forgets those.
async function setPoint(kind, point, { fromSearch = false, onMap = false } = {}) {
  if (!onMap) forgetUndo(kind);
  state[kind] = point;
  $(`${kind}-picked`).textContent = fromSearch ? "" : placeName(point);
  const box = $(`${kind}-search`).querySelector("input");
  if (box && !fromSearch) box.value = "";
  placePin(kind, point);
  updateFindButton();
  nameIt(point, () => {
    if (state[kind] === point) $(`${kind}-picked`).textContent = point.label;
  });
  tripChanged();
}

// A spot on the map, not yet named.
function unnamed({ lat, lng }) {
  return { lat, lng, label: null, countryCode: null };
}

function placeName(point) {
  return point.label || `${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}`;
}

// A spot picked on the map gets the nearest address, when the lookup answers. show: put it on the page.
async function nameIt(point, show) {
  if (point.label || !state.view) return;
  const place = await state.view.reverse(point);
  if (!place || !place.label || point.label) return;
  point.label = place.label;
  point.countryCode ||= place.countryCode;
  show();
}

// The start or destination taken off the map. Routes drawn to it go too.
function clearPoint(kind, { onMap = false } = {}) {
  if (!onMap) forgetUndo(kind);
  state[kind] = null;
  if (state.pins[kind]) state.pins[kind].remove();
  state.pins[kind] = null;
  $(`${kind}-picked`).textContent = "";
  const box = $(`${kind}-search`).querySelector("input");
  if (box) box.value = "";
  if (state.plan) clearTrip();
  updateFindButton();
}

// Dragging a pin moves it; clicking it removes it. A drag that isn't allowed (while driving) springs back.
function placePin(kind, point) {
  if (state.pins[kind]) state.pins[kind].remove();
  const pin = element("div", { className: `map-pin map-pin--${kind}` }, element("span", {}, kind === "origin" ? "A" : "B"));
  state.pins[kind] = state.view.marker(point, pin, {
    title: `${kind === "origin" ? "Start" : "Destination"}: drag to move, click to remove`,
    onClick: () => mapTouchAllowed() && changeOnMap(kind, null),
    onDragEnd: (to) => (mapTouchAllowed() ? changeOnMap(kind, unnamed(to)) : placePin(kind, state[kind])),
  });
}

// Start and destination trade places, and the stops turn around: the same trip, the other way.
function swapEnds() {
  if (state.nav.active || !(state.origin || state.destination)) return;
  const { origin, destination } = state;
  for (const [kind, point] of [["origin", destination], ["destination", origin]]) {
    if (point) setPoint(kind, point);
    else clearPoint(kind);
  }
  state.stops.reverse();
  for (const stop of state.stops) $("stops").append(stop.row);
  renumberStops();
}

// No accidental trip changes while driving, or from the touch that stopped the demo.
function mapTouchAllowed() {
  return !state.nav.active && Date.now() - (state.demo.stoppedAt || 0) >= 800;
}

// ---- Undo: changes made on the map (clicks, drags, pins added or removed), taken back newest first

// kind: what changed ("origin", "destination" or "stops"); what: the button's tooltip; undo: puts it back.
function remember(kind, what, undo) {
  state.undo.push({ kind, what, undo });
  updateUndoButton();
}

// point: where the start or destination goes, or null to remove it.
function changeOnMap(kind, point) {
  const before = state[kind];
  const name = kind === "origin" ? "start" : "destination";
  remember(kind, before ? `Put the ${name} back` : `Take the ${name} off the map`, () =>
    before ? setPoint(kind, before, { onMap: true }) : clearPoint(kind, { onMap: true }),
  );
  if (point) setPoint(kind, point, { onMap: true });
  else clearPoint(kind, { onMap: true });
}

function undoMapChange() {
  const change = state.undo.pop();
  if (change && !state.nav.active) change.undo();
  updateUndoButton();
}

// A place set some other way (search, location, Clear) can't be undone back to an older click.
function forgetUndo(kind) {
  state.undo = state.undo.filter((change) => change.kind !== kind);
  updateUndoButton();
}

function updateUndoButton() {
  const last = state.undo[state.undo.length - 1];
  $("undo").hidden = !last;
  $("undo").title = last ? last.what : "";
}

// A short note over the map, gone after a few seconds (the driving banner, while not driving).
function mapNotice(text) {
  if (state.nav.active) return;
  setNavStatus(text);
  clearTimeout(state.noticeTimer);
  state.noticeTimer = setTimeout(() => state.nav.active || setNavStatus(null), 3500);
}

function wireControls() {
  $("find").addEventListener("click", () => findRoute());
  $("swap").addEventListener("click", swapEnds);
  $("clear").addEventListener("click", clearAll);
  $("locate").addEventListener("click", useMyLocation);
  $("drive").addEventListener("click", () => (state.nav.active ? stopDriving() : startDriving()));
  $("recenter").addEventListener("click", recenter);
  $("undo").addEventListener("click", undoMapChange);
  $("traffic-layer").addEventListener("change", (event) => {
    if (state.view && state.view.hasTraffic) state.view.traffic(event.target.checked);
    else drawTraffic(); // no traffic layer: the measured slowdowns from the last trip
    $("legend-traffic").hidden = !event.target.checked;
  });
  for (const radio of document.querySelectorAll('input[name="base-map"]')) {
    radio.addEventListener("change", () => {
      state.baseMap = radio.value;
      if (!state.view) return;
      state.view.baseMap(radio.value);
      redrawLines();
    });
  }
  $("transit-layer").addEventListener("change", (event) => showTransit(event.target.checked));
  $("biking-layer").addEventListener("change", (event) => state.view && state.view.biking(event.target.checked));
  for (const input of document.querySelectorAll("#layers input")) input.addEventListener("change", rememberLayers);
  $("closures-layer").addEventListener("change", drawClosures);
  $("about-open").addEventListener("click", showAbout);
  $("phone-open").addEventListener("click", showPhoneLink);
  $("add-stop").addEventListener("click", () => addStop());
  // The map key starts folded on a phone, where it would cover much of the map.
  if (window.matchMedia(NARROW).matches) $("legend").open = false;
  // A screen wake lock is dropped whenever the page is hidden; take it again on return.
  document.addEventListener("visibilitychange", () => {
    if (state.nav.active && document.visibilityState === "visible") keepScreenOn();
  });
}

async function loadProfiles() {
  const select = $("profile");
  try {
    const response = await fetch("/profiles");
    const { default: chosen, profiles } = await response.json();
    for (const profile of profiles) {
      const option = element("option", { value: profile.name }, PROFILE_LABELS[profile.name] || profile.name);
      option.selected = profile.name === chosen;
      select.append(option);
    }
  } catch {
    select.append(element("option", { value: "safe-economy" }, PROFILE_LABELS["safe-economy"]));
  }
}

function updateFindButton() {
  $("find").disabled = !(state.origin && state.destination);
}

function clearAll() {
  if (state.nav.active) stopDriving();
  for (const kind of ["origin", "destination"]) clearPoint(kind);
  for (const stop of [...state.stops]) removeStop(stop.id);
  clearTrip();
}

// The drawn routes and their results, keeping the trip's places. A route request on its way is dropped.
function clearTrip() {
  state.findTicket += 1;
  clearTimeout(state.refreshTimer);
  setFinding(false);
  clearRoutes();
  state.trafficEvents = [];
  drawTraffic();
  state.plan = null;
  $("osm-credit").hidden = true;
  $("results").replaceChildren();
  $("drive").hidden = true;
  setNavStatus(null);
  updateFindButton();
}

// ---- Asking the backend

// fit: zoom the map to the routes. A trip planned again after a change on the map keeps the view.
async function findRoute({ fit = true } = {}) {
  if (!state.origin || !state.destination) return;
  const ticket = ++state.findTicket;
  setFinding(true);
  showStatus("Comparing routes for safety, fuel and time…");
  try {
    const trip = await requestTrip(state.origin, state.destination);
    if (ticket === state.findTicket) showTrip(trip, { fit });
  } catch (error) {
    if (ticket === state.findTicket) showStatus(error.message, true);
  } finally {
    if (ticket === state.findTicket) setFinding(false);
  }
}

function setFinding(on) {
  const button = $("find");
  button.classList.toggle("is-busy", on);
  button.textContent = on ? "Finding…" : "Find route";
  if (on) button.disabled = true;
  else updateFindButton();
}

// After the trip changes with its routes on the map, plan it again: once, for a burst of changes.
function tripChanged() {
  clearTimeout(state.refreshTimer);
  if (!state.plan || state.nav.active || state.demo.running) return;
  state.refreshTimer = setTimeout(() => findRoute({ fit: false }), 400);
}

// The trip as the server takes it. heading: the direction a moving car is driving, so new
// routes start that way. stops: places still to stop at (the chosen ones by default).
// demo: a sample trip, planned without live traffic so it costs no paid requests.
function tripPayload(origin, destination, { heading = null, stops = chosenStops(), demo = false } = {}) {
  const payload = {
    origin: { lat: origin.lat, lng: origin.lng },
    destination: { lat: destination.lat, lng: destination.lng },
    stops: stops.map((point) => ({ lat: point.lat, lng: point.lng })),
    profile: $("profile").value,
    country_code: origin.countryCode || destination.countryCode || null,
    live_traffic: $("live-traffic").checked && !demo,
    use_router: $("use-router").checked,
    road_events: $("road-events").value,
    departure_offset_min: Number($("leave").value) || 0,
  };
  if ($("extra-time").value) payload.max_extra_min = Number($("extra-time").value);
  if (heading !== null) payload.heading = Math.round(heading * 10) / 10;
  return payload;
}

async function requestTrip(origin, destination, options = {}) {
  return postJson("/route", tripPayload(origin, destination, options));
}

async function postJson(path, payload) {
  const headers = { "Content-Type": "application/json" };
  const code = accessCode();
  if (code) headers["X-Access-Code"] = code;
  let response;
  try {
    response = await fetch(path, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch {
    throw new Error("Can't reach the routing server. Open this page from it, e.g. http://127.0.0.1:3143");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(response.status, body));
  return body;
}

// A shared demo (see EXPO.md) may need an access code. It comes in the link (?code=...)
// and is remembered in this browser, so the QR code only has to be scanned once.
function accessCode() {
  const fromLink = new URLSearchParams(window.location.search).get("code");
  try {
    if (fromLink) localStorage.setItem("routes-access-code", fromLink);
    return fromLink || localStorage.getItem("routes-access-code");
  } catch {
    return fromLink; // storage blocked: the link's code still works for this visit
  }
}

// The server's own error details name its data sources, so only bad-input (422) details are shown as-is.
const ERROR_MESSAGES = {
  401: "This demo needs its access code: open it from the link or QR code you were given.",
  404: "No drivable route was found between these points.",
  429: "Today's live traffic limit is used up. Turn off Live traffic to keep planning, or try again tomorrow.",
  502: "The live traffic service didn't answer. Try again, or turn off Live traffic.",
  503: "The server is in offline replay mode and has no saved traffic data for this trip.",
};

function errorMessage(status, body) {
  const detail = body && body.detail;
  if (status === 422 && Array.isArray(detail)) return detail.map((item) => item.msg).join("; ");
  if (status === 422 && detail) return detail;
  return ERROR_MESSAGES[status] || `The server answered ${status}.`;
}

// Rewrite the server's notes (written for developers) into what a driver needs to know.
const NOTE_MESSAGES = [
  [/not priced/, "Some of our designs couldn't be checked against live traffic, so they're left out."],
  [/Own router skipped/, "This trip is outside the area our router covers, so only standard routes are shown."],
  [/Own router unavailable/, "Our router isn't available right now, so only standard routes are shown."],
  [/suggestions skipped/, "Standard routes weren't available for this trip, so only our designs are shown."],
];

function driverNotes(notes) {
  if (notes.length) console.info("Server notes:", notes);
  const messages = new Set();
  for (const note of notes) {
    const match = NOTE_MESSAGES.find(([pattern]) => pattern.test(note));
    if (match) messages.add(match[1]); // other notes (re-pinned, duplicate designs) are only for developers
  }
  return [...messages];
}

// ---- Drawing and explaining the result

// fit: zoom the map to the routes. Off while driving, so the map stays on the car.
function showTrip(trip, { fit = true } = {}) {
  const { plan } = trip;
  state.plan = plan;
  clearRoutes();
  drawRoutes(plan, fit);
  drawRiskyTurns(plan.winner);
  state.closures = trip.road_events.filter((event) => !isTraffic(event));
  drawClosures();
  state.trafficEvents = trip.road_events.filter(isTraffic);
  drawTraffic();

  const credit = $("osm-credit");
  credit.textContent = trip.attribution.join(" · ");
  credit.hidden = trip.attribution.length === 0;

  const standard = plan.candidates.find(isStandard);
  const notes = driverNotes(trip.notes);
  const usage = trip.google; // the server's count of route data requests
  const when = trip.leaving_at && Number($("leave").value) ? `at ${clockTime(trip.leaving_at)}` : "now";
  $("results").replaceChildren(
    ...(trip.weather_alerts || []).map(weatherAlert),
    winnerCard(plan, standard),
    element("h2", { className: "results__heading" }, "All routes, best first · pick one to follow it"),
    routeList(plan),
    leaveTimesBlock(),
    ...(notes.length ? [element("ul", { className: "notes" }, ...notes.map((note) => element("li", {}, note)))] : []),
    element("p", { className: "usage" }, roadEventsLine(trip)),
    ...(trip.school_hours ? [element("p", { className: "usage" }, `School zones count: leaving ${when} is a school drop-off or pickup time.`)] : []),
    ...(trip.low_sun ? [element("p", { className: "usage" }, `The sun is low leaving ${when}: roads heading into it count as riskier.`)] : []),
    // Route data requests are paid ones; the OpenStreetMap map never makes any.
    ...(state.mapKind === "osm"
      ? []
      : [
          element(
            "p",
            { className: "usage" },
            `Route data requests for this trip: ${usage.live_requests}` +
              (usage.daily_limit ? ` (${usage.used_today} of ${usage.daily_limit} today)` : ""),
          ),
        ]),
  );
  selectRoute(plan.winner.id);
  $("drive").hidden = false;
}

// On satellite imagery, the gray and black lines get a white edge to stand out.
function drawRoutes(plan, fit) {
  const view = state.view;
  const edged = state.baseMap === "satellite";
  const everywhere = [];
  state.paths = {};
  // Losers first, so the winner is drawn on top.
  const ordered = [...plan.candidates].reverse();
  for (const route of ordered) {
    const path = decodePolyline(route.encoded_polyline);
    everywhere.push(...path);
    state.paths[route.id] = followablePath(path, route, plan);
    const isWinner = route.id === plan.winner.id;
    const lines = [];
    if (isWinner) {
      lines.push(view.line(path, { color: "#ffffff", weight: 10, zIndex: 20 }));
      lines.push(view.line(path, { color: COLORS.winner, weight: 6, zIndex: 21 }));
    } else if (isStandard(route)) {
      if (edged) lines.push(view.line(path, { color: "#ffffff", weight: 7, opacity: 0.85, zIndex: 9 }));
      lines.push(view.line(path, { color: COLORS.standard, dashed: true, zIndex: 10 }));
    } else {
      if (edged) lines.push(view.line(path, { color: "#ffffff", weight: 8, opacity: 0.7, zIndex: 4 }));
      lines.push(view.line(path, { color: COLORS.alternative, weight: 5, opacity: 0.8, zIndex: 5 }));
    }
    state.routeLines.push({ id: route.id, lines });
  }
  if (fit) view.fit(everywhere);
}

// Which source the closures and slowdowns came from, and which were tried first but unavailable.
function roadEventsLine(trip) {
  const skipped = trip.road_events_skipped.map((name) => EVENT_SOURCES[name] || name);
  const unavailable = skipped.length ? ` (${skipped.join(", ")} unavailable)` : "";
  if (!trip.road_events_source) return `No closure or slowdown data for this trip${unavailable}.`;
  // Sources used together arrive joined with "+".
  const used = trip.road_events_source.split("+").map((name) => EVENT_SOURCES[name] || name).join(" and ");
  // A closure of both directions of a divided road arrives once per carriageway.
  const closures = trip.road_events.filter((event) => event.kind === "closed" || event.kind === "lanes");
  const shown = new Set(closures.map((event) => `${event.kind}|${event.description}`)).size;
  const counted = shown ? `; ${shown} closure${shown === 1 ? "" : "s"} nearby` : "";
  return `Closures and slowdowns from ${used}${unavailable}${counted}.`;
}

// Closed roads in black, lane closures in orange, with a dot to hover for the details.
function isTraffic(event) {
  return event.kind === "slow" || event.kind === "jam";
}

// Measured traffic (511 travel times, TomTom's jams): orange where slow, red where jammed,
// under the Traffic layer on a map without a traffic layer of its own.
function drawTraffic() {
  for (const shape of state.trafficShapes) shape.remove();
  state.trafficShapes = [];
  if (!state.view || state.view.hasTraffic || !$("traffic-layer").checked) return;
  for (const event of state.trafficEvents) {
    const color = event.kind === "jam" ? COLORS.jam : COLORS.slow;
    state.trafficShapes.push(state.view.line(decodePolyline(event.path), { color, weight: 4, opacity: 0.85, zIndex: 3 }));
  }
}

// Closed roads in black, lane closures in dashed orange, each with a marker to tap for the details.
// Crowded markers merge into a count on the OpenStreetMap map. "Show closures" hides them all.
function drawClosures() {
  for (const line of state.closureLines) line.remove();
  state.closureLines = [];
  if (!state.closureMarkers) return;
  state.closureMarkers.clear();
  if (!$("closures-layer").checked) return;
  for (const event of state.closures) {
    const path = decodePolyline(event.path);
    if (path.length > 1 && event.kind === "closed" && state.baseMap === "satellite") {
      state.closureLines.push(state.view.line(path, { color: "#ffffff", weight: 10, opacity: 0.85, zIndex: 24 }));
    }
    if (path.length > 1) {
      state.closureLines.push(
        event.kind === "closed"
          ? state.view.line(path, { color: COLORS.closed, weight: 7, zIndex: 25 })
          : event.kind === "hazard"
            ? state.view.line(path, { color: COLORS.hazard, weight: 6, opacity: 0.75, zIndex: 24 })
            : state.view.line(path, { color: COLORS.lanes, dashed: true, zIndex: 25 }),
      );
    }
    const heading = { closed: "Road closed", lanes: "Lane closure", hazard: "Weather hazard" }[event.kind] || "Road event";
    const dot = element("div", { className: `event-dot event-dot--${event.kind}` });
    const title = event.description ? `${heading}: ${event.description}` : heading;
    state.closureMarkers.add(path[0], dot, { title, zIndex: 26, popup: { heading, text: event.description } });
  }
}

// The routes and closures drawn again for a new map look (see drawRoutes), keeping the route being followed.
function redrawLines() {
  if (state.plan) {
    const active = state.activeRouteId;
    for (const entry of state.routeLines) entry.lines.forEach((line) => line.remove());
    state.routeLines = [];
    drawRoutes(state.plan, false);
    selectRoute(active);
  }
  drawClosures();
}

// What following a route needs: its points, the distance to each along it, and its expected time.
function followablePath(points, route, plan) {
  const cumulative = [0];
  for (let i = 1; i < points.length; i++) cumulative.push(cumulative[i - 1] + distanceM(points[i - 1], points[i]));
  const followable = {
    points,
    cumulative,
    lengthM: cumulative[cumulative.length - 1],
    minutes: route.features.duration_min,
    label: route.via ? `the route via ${route.via}` : routeKind(route, plan), // "Following ... instead"
  };
  // Where each turn-by-turn instruction falls along the route, to know which comes next.
  followable.maneuvers = route.maneuvers.map((maneuver) => ({
    ...maneuver,
    along: nearestOnPath(maneuver.location, followable).along,
  }));
  return followable;
}

function drawRiskyTurns(route) {
  for (const turn of route.features.crossing_turns) {
    if (!(turn.busy || turn.hotspot) || !turn.location) continue;
    const why = turn.hotspot ? "a crash-prone junction" : "a busy junction";
    const dot = element("div", { className: "turn-dot" });
    const text = `The recommended route turns across oncoming traffic at ${why}${turn.hotspot ? ", where fatal crashes have happened" : ""}.`;
    state.turnMarkers.push(
      state.view.marker(turn.location, dot, { title: `Turn across traffic at ${why}`, zIndex: 30, popup: { heading: "Risky turn", text } }),
    );
  }
}

// Make one route the one to follow, and bring it to the front without changing how it's drawn.
function selectRoute(id) {
  state.activeRouteId = id;
  for (const entry of state.routeLines) {
    for (const line of entry.lines) line.front(entry.id === id);
  }
  for (const item of document.querySelectorAll(".route")) {
    item.classList.toggle("route--active", item.dataset.id === id);
  }
}

function clearRoutes() {
  for (const entry of state.routeLines) entry.lines.forEach((line) => line.remove());
  for (const marker of state.turnMarkers) marker.remove();
  state.routeLines = [];
  state.turnMarkers = [];
  state.closures = [];
  drawClosures();
  state.paths = {};
  state.activeRouteId = null;
}

function winnerCard(plan, standard) {
  const winner = plan.winner;
  const card = element(
    "section",
    { className: "winner" },
    element(
      "div",
      { className: "winner__head" },
      element(
        "div",
        {},
        element("span", { className: "winner__badge" }, "Recommended"),
        element("h2", { className: "winner__title" }, routeLabel(winner, plan)),
        ...(winner.via ? [element("p", { className: "winner__kind" }, routeKind(winner, plan))] : []),
      ),
      scoreRing(winner.safety_score),
    ),
  );
  if (winner.reason) card.append(element("p", { className: "winner__reason" }, capitalize(winner.reason)));
  if (standard && standard.id !== winner.id) {
    card.append(element("p", { className: "override" }, comparedWithStandard(winner, standard)));
  }
  card.append(stats(winner), feedbackBlock(winner, standard));
  return card;
}

// The safety score as a ring that fills to it: green when high, amber, then red.
function scoreRing(score) {
  const band = score >= 80 ? "high" : score >= 50 ? "mid" : "low";
  return element(
    "div",
    { className: `score score--${band}`, style: `--score:${score}`, title: "Safety score: 0 to 100, higher is safer" },
    element("strong", {}, String(score)),
    element("span", {}, "safety"),
  );
}

function scorePill(score) {
  const band = score >= 80 ? "high" : score >= 50 ? "mid" : "low";
  return element("span", { className: `score-pill score-pill--${band}`, title: "Safety score: 0 to 100, higher is safer" }, `${score}`);
}

function weatherAlert(alert) {
  const until = alert.ends ? ` until ${clockTime(alert.ends)}` : "";
  return element(
    "div",
    { className: "alert" },
    element("strong", {}, `${alert.event}${until}`),
    element(
      "p",
      {},
      alert.slippery
        ? "Slippery roads: turns across oncoming traffic count as riskier, so routes avoid more of them."
        : "Drive with care: the routes account for hazards reported on the roads.",
    ),
  );
}

// "3:45 PM" from an ISO time.
function clockTime(iso) {
  const moment = new Date(iso);
  return Number.isNaN(moment.getTime()) ? "" : moment.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function stats(route) {
  const f = route.features;
  const items = [
    ["Time", `${Math.round(f.duration_min)} min${route.live_traffic ? "" : " (est.)"}`],
    ["Distance", `${(f.distance_km * KM_TO_MI).toFixed(1)} mi`],
    ["Fuel", `${gallons(route).toFixed(2)} gal · ${dollars(gallons(route) * state.fuelPrice)}`],
    ["CO₂", `${(gallons(route) * CO2_KG_PER_GALLON).toFixed(1)} kg`],
    ["Risky turns", String(riskyTurns(route))],
  ];
  for (const [label, value] of hazards(f)) items.push([label, value]);
  return element("dl", { className: "stats" }, ...items.map(([label, value]) => element("div", {}, element("dt", {}, label), element("dd", {}, value))));
}

function gallons(route) {
  return route.features.fuel_l / LITERS_PER_GALLON;
}

// "$1.40", or "35¢" under a dollar.
function dollars(amount) {
  return amount < 1 ? `${Math.round(amount * 100)}¢` : `$${amount.toFixed(2)}`;
}

// The hazards a route has besides risky turns, as [label, shown value], for the ones it has.
function hazards(f) {
  return [
    ["Lane closures", f.lane_closures],
    ["Closed roads", f.closures],
    ["School zones", f.school_zones],
    ["Rail crossings", f.rail_crossings],
    ["Weather hazards", f.weather_hazards || 0],
  ]
    .filter(([, count]) => count > 0)
    .map(([label, count]) => [label, String(count)])
    .concat((f.sun_glare_min || 0) >= 0.5 ? [["Sun glare", `${Math.round(f.sun_glare_min)} min`]] : []);
}

function routeList(plan) {
  return element(
    "ol",
    { className: "routes" },
    ...plan.candidates.map((route) => {
      const color = route.id === plan.winner.id ? COLORS.winner : isStandard(route) ? COLORS.standard : COLORS.alternative;
      const f = route.features;
      const risky = riskyTurns(route);
      const item = element(
        "li",
        { className: "route", tabIndex: 0, dataset: { id: route.id } },
        element("span", { className: "swatch", style: `background:${color}` }),
        element(
          "div",
          {},
          element("div", { className: "route__name" }, routeLabel(route, plan), " ", scorePill(route.safety_score)),
          element(
            "div",
            { className: "route__reason" },
            [
              route.via ? routeKind(route, plan) : "",
              route.reason ? capitalize(route.reason) : route.eligible ? "" : "Too slow to recommend",
            ]
              .filter(Boolean)
              .join(" · "),
          ),
        ),
        element(
          "div",
          { className: "route__numbers" },
          element("div", {}, `${Math.round(f.duration_min)} min · ${(f.distance_km * KM_TO_MI).toFixed(1)} mi`),
          element(
            "div",
            {},
            `${dollars(gallons(route) * state.fuelPrice)} gas · `,
            element("span", { className: risky ? "risky" : "" }, `${risky} risky`),
          ),
          ...hazards(f).map(([label, value]) => element("div", { className: "risky" }, `${value} ${label.toLowerCase()}`)),
        ),
      );
      item.addEventListener("click", () => selectRoute(route.id));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") selectRoute(route.id);
      });
      return item;
    }),
  );
}

function comparedWithStandard(winner, standard) {
  const parts = [];
  if (winner.safety_score !== standard.safety_score) parts.push(`safety score ${standard.safety_score} → ${winner.safety_score}`);
  const fewer = riskyTurns(standard) - riskyTurns(winner);
  if (fewer > 0) parts.push(`${fewer} fewer risky turn${fewer === 1 ? "" : "s"}`);
  for (const [field, name] of [
    ["lane_closures", "lane closure"],
    ["school_zones", "school zone"],
    ["rail_crossings", "rail crossing"],
    ["weather_hazards", "weather hazard"],
  ]) {
    const fewerOf = (standard.features[field] || 0) - (winner.features[field] || 0);
    if (fewerOf > 0) parts.push(`${fewerOf} fewer ${name}${fewerOf === 1 ? "" : "s"}`);
  }
  const glare = (standard.features.sun_glare_min || 0) - (winner.features.sun_glare_min || 0);
  if (glare >= 0.5) parts.push(`${Math.round(glare)} min less sun glare`);
  if (standard.features.closures && !winner.features.closures) parts.push("avoids a closed road");
  const cost = (gallons(winner) - gallons(standard)) * state.fuelPrice;
  if (cost <= -0.01) parts.push(`${dollars(-cost)} less gas`);
  else if (cost >= 0.01) parts.push(`${dollars(cost)} more gas`);
  const minutes = winner.features.duration_min - standard.features.duration_min;
  if (minutes >= 0.5) parts.push(`${minutes.toFixed(1)} min longer`);
  else if (minutes <= -0.5) parts.push(`${(-minutes).toFixed(1)} min faster`);
  return `Instead of the standard route: ${parts.join(", ") || "a different path"}`;
}

// The standard route is the one a typical navigation app would suggest: the
// map provider's own choice, or with the OpenStreetMap map, our fastest route.
function isStandard(route) {
  return route.is_standard;
}

// What a route is called: by its road ("Via W Wisconsin Ave"), or else by what it is.
function routeLabel(route, plan) {
  return route.via ? `Via ${route.via}` : routeKind(route, plan);
}

// What a route is: one of our designs, the standard route, or another alternative.
function routeKind(route, plan) {
  if (route.source === "own") {
    // Number our designs when there are several (ids are own-1, own-2, ...).
    const several = plan.candidates.filter((candidate) => candidate.source === "own").length > 1;
    return several ? `Our safer design ${route.id.split("-").pop()}` : "Our safer design";
  }
  if (isStandard(route)) return "Standard route";
  return "Alternative route";
}

function riskyTurns(route) {
  return route.features.busy_crossing_turns + route.features.hotspot_crossing_turns;
}

// ---- Location: the start point, and following the drive

function locationProblem() {
  if (!window.isSecureContext) return "Location only works on a secure page: https, or this computer's own address.";
  if (!navigator.geolocation) return "This browser can't share its location.";
  return null;
}

function locationError(error) {
  if (error.code === error.PERMISSION_DENIED) {
    return "Location access was denied. Allow it for this site in the browser's address bar, then try again.";
  }
  if (error.code === error.TIMEOUT) return "Finding your location took too long. Try again.";
  return "Your location isn't available right now.";
}

function useMyLocation() {
  const problem = locationProblem();
  if (problem) return showStatus(problem, true);
  const button = $("locate");
  button.disabled = true;
  button.textContent = "Locating…";
  const done = () => {
    button.disabled = false;
    button.textContent = "Use my location";
  };
  navigator.geolocation.getCurrentPosition(
    (position) => {
      done();
      const here = { lat: position.coords.latitude, lng: position.coords.longitude };
      setPoint("origin", { ...here, label: "My location", countryCode: null });
      if (state.view) state.view.panTo(here);
    },
    (error) => {
      done();
      showStatus(locationError(error), true);
    },
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 },
  );
}

async function startDriving() {
  const problem = locationProblem();
  if (problem) return setNavStatus(problem);
  if (!state.plan || !state.destination) return;
  const nav = state.nav;
  Object.assign(nav, { active: true, following: true, fix: null, anchor: null, heading: null, offRoute: 0, lastRerouteAt: 0 });
  nav.stopsLeft = chosenStops();
  nav.spoken.clear();
  stopDemo();
  nav.watchId = navigator.geolocation.watchPosition(onFix, onFixError, {
    enableHighAccuracy: true,
    maximumAge: 2000,
    timeout: 20000,
  });
  keepScreenOn();
  if (state.pins.origin) state.pins.origin.remove(); // the car marks the start while driving
  $("drive").textContent = "Stop driving";
  document.body.classList.add("driving");
  setNavStatus("Waiting for your location…");
}

function stopDriving(message = null) {
  const nav = state.nav;
  if (nav.watchId !== null) navigator.geolocation.clearWatch(nav.watchId);
  nav.watchId = null;
  nav.active = false;
  if (nav.car) nav.car.remove();
  nav.car = null;
  if (state.origin) placePin("origin", state.origin);
  if (nav.wakeLock) nav.wakeLock.release().catch(() => {});
  nav.wakeLock = null;
  $("drive").textContent = "Start driving";
  $("recenter").hidden = true;
  document.body.classList.remove("driving");
  setNavStatus(message);
}

function onFix(position) {
  const nav = state.nav;
  if (!nav.active) return;
  const { latitude, longitude, accuracy, heading, speed } = position.coords;
  const here = { lat: latitude, lng: longitude };
  const first = nav.fix === null;

  // The device's heading while moving; otherwise the bearing since the last few meters.
  if (Number.isFinite(heading) && speed > 1) nav.heading = heading;
  else if (nav.anchor && distanceM(nav.anchor, here) >= NAV.headingFromM) nav.heading = bearing(nav.anchor, here);
  if (!nav.anchor || distanceM(nav.anchor, here) >= NAV.headingFromM) nav.anchor = here;
  nav.fix = here;

  moveCar(here, nav.heading);
  if (nav.following) {
    state.view.panTo(here);
    if (first) state.view.setZoom(NAV.followZoom);
  }
  if (distanceM(here, state.destination) <= NAV.arrivedM) {
    speak("You have arrived.");
    return stopDriving("You've arrived.");
  }
  // A stop reached: new routes from here on leave it out.
  if (nav.stopsLeft.length && distanceM(here, nav.stopsLeft[0]) <= NAV.arrivedM) {
    nav.stopsLeft.shift();
    const left = nav.stopsLeft.length;
    speak(left ? "You've reached your stop." : "You've reached your last stop.");
  }
  if (accuracy > NAV.worstAccuracyM) return setNavStatus("Weak GPS signal…");
  checkRoute(here);
}

function onFixError(error) {
  if (error.code === error.PERMISSION_DENIED) stopDriving(locationError(error));
  else setNavStatus("Waiting for a GPS signal…"); // the watch keeps trying
}

// On the followed route: show progress. On another drawn route: follow that one.
// Off all of them for a couple of fixes: ask for a new route from here.
function checkRoute(here) {
  const nav = state.nav;
  const active = state.paths[state.activeRouteId];
  const onActive = active && nearestOnPath(here, active);
  if (onActive && onActive.distance <= NAV.onRouteM) {
    nav.offRoute = 0;
    if (!nav.rerouting) showProgress(active, onActive.along);
    return;
  }
  for (const [id, path] of Object.entries(state.paths)) {
    if (id !== state.activeRouteId && nearestOnPath(here, path).distance <= NAV.onRouteM) {
      nav.offRoute = 0;
      selectRoute(id);
      setNavStatus(`Following ${path.label} instead`);
      return;
    }
  }
  nav.offRoute += 1;
  if (nav.offRoute >= NAV.offRouteFixes) reroute(here);
}

async function reroute(here) {
  const nav = state.nav;
  if (nav.rerouting || Date.now() - nav.lastRerouteAt < NAV.rerouteGapMs) return;
  nav.rerouting = true;
  nav.lastRerouteAt = Date.now();
  setNavStatus("Off route. Finding a new one…");
  speak("Finding a new route.");
  const countryCode = (state.origin && state.origin.countryCode) || null;
  const origin = { ...here, label: "Current location", countryCode };
  try {
    const trip = await requestTrip(origin, state.destination, { heading: nav.heading, stops: nav.stopsLeft });
    if (!nav.active) return;
    state.origin = origin; // its pin returns when driving stops
    $("origin-picked").textContent = origin.label;
    showTrip(trip, { fit: false });
    nav.offRoute = 0;
    const reason = trip.plan.winner.reason;
    setNavStatus(reason ? `New route: ${reason}` : "New route found");
  } catch (error) {
    if (nav.active) setNavStatus(`${error.message} Trying again shortly.`);
  } finally {
    nav.rerouting = false;
  }
}

// The next turn and what's left, on the banner; the turn spoken once ahead of it and once at it.
function showProgress(path, along) {
  const leftM = Math.max(0, path.lengthM - along);
  const minutes = path.lengthM ? path.minutes * (leftM / path.lengthM) : 0;
  const left = `${(leftM / M_PER_MI).toFixed(1)} mi left · about ${Math.max(1, Math.round(minutes))} min`;
  const next = path.maneuvers.find((maneuver) => maneuver.along > along + 5);
  if (!next) return setNavStatus(left);
  const toGo = next.along - along;
  setNavStatus(`In ${spokenDistance(toGo)}: ${next.instruction}\n${left}`);

  const key = `${next.location.lat.toFixed(5)},${next.location.lng.toFixed(5)}|${next.instruction}`;
  if (toGo <= NAV.announceNearM) {
    if (!state.nav.spoken.has(`${key}|near`)) speak(next.instruction);
    state.nav.spoken.add(`${key}|near`).add(`${key}|far`);
  } else if (toGo <= NAV.announceFarM && !state.nav.spoken.has(`${key}|far`)) {
    speak(`In ${spokenDistance(toGo)}, ${next.instruction}`);
    state.nav.spoken.add(`${key}|far`);
  }
}

// "500 feet" under a tenth of a mile, else "0.3 miles".
function spokenDistance(meters) {
  const miles = meters / M_PER_MI;
  if (miles < 0.1) return `${Math.max(50, Math.round((meters * 3.28084) / 50) * 50)} feet`;
  return `${miles.toFixed(1)} miles`;
}

// Read directions aloud, if the driver wants them and the browser can.
function speak(text) {
  if (!$("voice").checked || !("speechSynthesis" in window)) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  window.speechSynthesis.speak(utterance);
}

function moveCar(here, heading) {
  const nav = state.nav;
  if (!nav.car) {
    nav.carArrow = element("div", { className: "car__arrow" });
    nav.car = state.view.marker(here, element("div", { className: "car" }, nav.carArrow), { title: "You", zIndex: 50 });
  } else {
    nav.car.move(here);
  }
  nav.carArrow.hidden = heading === null; // just a dot until the direction is known
  if (heading !== null) nav.carArrow.style.transform = `rotate(${heading}deg)`;
}

function recenter() {
  const nav = state.nav;
  nav.following = true;
  $("recenter").hidden = true;
  if (nav.fix) {
    state.view.panTo(nav.fix);
    state.view.setZoom(NAV.followZoom);
  }
}

// Keep the screen on while driving, where the browser allows it.
async function keepScreenOn() {
  try {
    state.nav.wakeLock = await navigator.wakeLock.request("screen");
  } catch {
    // Not supported or refused: the screen may sleep, but following still works while it's on.
  }
}

// The banner over the map while driving; null hides it.
function setNavStatus(message) {
  const banner = $("nav-banner");
  banner.textContent = message || "";
  banner.hidden = !message;
}

// Distance from point to the path (m) and how far along the path the nearest spot is (m),
// on a flat projection around point: exact enough at the distances that matter here.
function nearestOnPath(point, path) {
  const { points, cumulative } = path;
  const mPerDegLng = M_PER_DEG_LAT * Math.cos((point.lat * Math.PI) / 180);
  let best = { distance: Infinity, along: 0 };
  for (let i = 0; i + 1 < points.length; i++) {
    const ax = (points[i].lng - point.lng) * mPerDegLng;
    const ay = (points[i].lat - point.lat) * M_PER_DEG_LAT;
    const dx = (points[i + 1].lng - point.lng) * mPerDegLng - ax;
    const dy = (points[i + 1].lat - point.lat) * M_PER_DEG_LAT - ay;
    const lengthSq = dx * dx + dy * dy;
    const t = lengthSq ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / lengthSq)) : 0;
    const distance = Math.hypot(ax + t * dx, ay + t * dy);
    if (distance < best.distance) best = { distance, along: cumulative[i] + t * (cumulative[i + 1] - cumulative[i]) };
  }
  return best;
}


// ---- Stops on the way (up to MAX_STOPS, in order)

// point: a place already picked on the map; index: its place in the order (last by default);
// id: the stop's own, when Undo puts back a removed stop. Returns the stop's id, or null when full.
async function addStop({ point = null, index = state.stops.length, id = state.nextStopId++ } = {}) {
  if (state.stops.length >= MAX_STOPS || !state.view) return null;
  const searchBox = element("div", { id: `stop-${id}-search`, className: "search" });
  const remove = element("button", { className: "link-button link-button--remove", type: "button" }, "Remove");
  const pin = element("span", { className: "pin pin--via" });
  pin.setAttribute("aria-hidden", "true");
  const row = element(
    "div",
    { className: "stop stop--via" },
    pin,
    element(
      "div",
      { className: "stop__body" },
      element("div", { className: "stop__head" }, element("span", { className: "stop__label" }), remove),
      searchBox,
      element("p", { id: `stop-${id}-picked`, className: "picked" }),
    ),
  );
  remove.addEventListener("click", () => removeStop(id));
  const stop = { id, point, pin: null, row };
  const next = state.stops[index];
  $("stops").insertBefore(row, next ? next.row : null);
  state.stops.splice(index, 0, stop);
  renumberStops();
  $("add-stop").hidden = state.stops.length >= MAX_STOPS;
  if (point) {
    showStopName(stop);
    tripChanged();
  }
  await state.view.search(searchBox, "Add a stop", (place) => setStop(id, place));
  const input = searchBox.querySelector("input");
  if (input && !point) input.focus();
  return id;
}

// Picked in the stop's search box, which shows its name.
function setStop(id, place) {
  const stop = state.stops.find((candidate) => candidate.id === id);
  if (!stop) return;
  forgetUndo("stops");
  stop.point = place;
  stop.row.querySelector(".picked").textContent = "";
  renumberStops();
  tripChanged();
}

function moveStop(id, point) {
  const stop = state.stops.find((candidate) => candidate.id === id);
  if (!stop) return;
  stop.point = point;
  const box = stop.row.querySelector("input");
  if (box) box.value = "";
  renumberStops();
  showStopName(stop);
  tripChanged();
}

// onMap: Undo can bring it back (see removeStopOnMap); any other removal forgets those.
function removeStop(id, { onMap = false } = {}) {
  const index = state.stops.findIndex((stop) => stop.id === id);
  if (index < 0) return;
  if (!onMap) forgetUndo("stops");
  const [stop] = state.stops.splice(index, 1);
  stop.row.remove();
  if (stop.pin) stop.pin.remove();
  renumberStops();
  $("add-stop").hidden = false;
  tripChanged();
}

// A stop placed on the map is spelled out under its box, then named by the nearest address.
function showStopName(stop) {
  const point = stop.point;
  const picked = stop.row.querySelector(".picked");
  picked.textContent = point ? placeName(point) : "";
  if (point) nameIt(point, () => stop.point === point && (picked.textContent = point.label));
}

// A right-click or long press adds a stop where it fits the trip best. Before the trip has
// both ends, it sets them like a click.
function addStopOnMap(point) {
  if (!mapTouchAllowed()) return;
  if (!state.origin || !state.destination) return changeOnMap(state.origin ? "destination" : "origin", unnamed(point));
  if (state.stops.length >= MAX_STOPS) return mapNotice(`Up to ${MAX_STOPS} stops: remove one to add another.`);
  const id = state.nextStopId++;
  remember("stops", "Take the stop off the map", () => removeStop(id, { onMap: true }));
  addStop({ point: unnamed(point), index: bestStopIndex(point), id });
}

function moveStopOnMap(id, point) {
  const stop = state.stops.find((candidate) => candidate.id === id);
  if (!stop) return;
  const before = stop.point;
  remember("stops", "Put the stop back", () => moveStop(id, before));
  moveStop(id, point);
}

function removeStopOnMap(id) {
  const index = state.stops.findIndex((stop) => stop.id === id);
  if (index < 0) return;
  const { point } = state.stops[index];
  remember("stops", "Put the stop back", () => addStop({ point, index, id }));
  removeStop(id, { onMap: true });
}

// Where a new stop fits: the place in the order that adds the least distance, as the crow flies.
function bestStopIndex(point) {
  const length = (points) => points.reduce((sum, p, i) => (i ? sum + distanceM(points[i - 1], p) : 0), 0);
  let best = state.stops.length;
  let shortest = Infinity;
  for (let i = 0; i <= state.stops.length; i++) {
    const stops = state.stops.map((stop) => stop.point);
    stops.splice(i, 0, point);
    const total = length([state.origin, ...stops, state.destination].filter(Boolean));
    if (total < shortest - 1) [best, shortest] = [i, total];
  }
  return best;
}

// Stops are numbered in order, in the panel and on their map pins, which drag and click like the others.
function renumberStops() {
  state.stops.forEach((stop, i) => {
    const number = String(i + 1);
    stop.row.querySelector(".pin").textContent = number;
    stop.row.querySelector(".stop__label").textContent = `Stop ${number}`;
    if (stop.pin) stop.pin.remove();
    stop.pin = stop.point
      ? state.view.marker(stop.point, element("div", { className: "map-pin map-pin--stop" }, element("span", {}, number)), {
          title: `Stop ${number}: drag to move, click to remove`,
          onClick: () => mapTouchAllowed() && removeStopOnMap(stop.id),
          onDragEnd: (to) => (mapTouchAllowed() ? moveStopOnMap(stop.id, unnamed(to)) : renumberStops()),
        })
      : null;
  });
}

function chosenStops() {
  return state.stops.filter((stop) => stop.point).map((stop) => stop.point);
}

// ---- Map layers: remembered in this browser; the OpenStreetMap map's buses

function rememberLayers() {
  const base = document.querySelector('input[name="base-map"]:checked').value;
  const on = (name) => $(`${name}-layer`).checked;
  try {
    localStorage.setItem(LAYERS_KEY, JSON.stringify({ base, traffic: on("traffic"), transit: on("transit"), biking: on("biking") }));
  } catch {
    // Storage blocked: the layers start as they are next time.
  }
}

// Set as last time, through the controls' own change handlers.
function restoreLayers() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LAYERS_KEY));
  } catch {
    return;
  }
  if (!saved || typeof saved !== "object") return;
  const radio = [...document.querySelectorAll('input[name="base-map"]')].find((input) => input.value === saved.base);
  const changes = radio && !radio.checked ? [radio] : [];
  for (const name of ["traffic", "transit", "biking"]) {
    const box = $(`${name}-layer`);
    if (typeof saved[name] === "boolean" && box.checked !== saved[name]) changes.push(box);
  }
  for (const input of changes) {
    input.checked = input.type === "radio" || !input.checked;
    input.dispatchEvent(new Event("change"));
  }
}

// Transit lines from the map itself, and on the OpenStreetMap map the county's bus routes from
// our server, each in its own color, beneath the trip's routes.
async function showTransit(on) {
  if (!state.view) return;
  state.view.transit(on);
  const buses = state.buses;
  if (!buses.available) return;
  $("legend-buses").hidden = !on;
  if (!on) {
    buses.lines.splice(0).forEach((line) => line.remove());
    if (buses.data) state.view.credit(buses.data.attribution, false);
    return;
  }
  try {
    if (!buses.data) {
      const response = await fetch("/bus-routes");
      if (!response.ok) throw new Error(`The server answered ${response.status}`);
      buses.data = await response.json();
    }
  } catch (error) {
    console.warn("Bus routes unavailable:", error);
    return;
  }
  if (!$("transit-layer").checked || buses.lines.length) return; // turned off, or drawn, while waiting
  for (const route of buses.data.routes) {
    for (const path of route.paths) {
      buses.lines.push(state.view.line(decodePolyline(path), { color: route.color, weight: 3, opacity: 0.7, zIndex: 1, under: true }));
    }
  }
  if (buses.data.attribution) state.view.credit(buses.data.attribution, true);
}

// ---- Visitors' answers, and the best time to leave

// "Would you take this route?": answers are counted on the server, with no places kept.
function feedbackBlock(winner, standard) {
  const block = element("div", { className: "feedback" }, element("span", { className: "feedback__question" }, "Would you take this route?"));
  const answer = async (choice) => {
    block.replaceChildren(element("span", { className: "feedback__thanks" }, "Thanks for your answer!"));
    try {
      await postJson("/feedback", {
        choice,
        safety_gain: standard ? winner.safety_score - standard.safety_score : 0,
        extra_min: standard ? Math.round((winner.features.duration_min - standard.features.duration_min) * 10) / 10 : 0,
        profile: $("profile").value,
      });
    } catch (error) {
      console.warn("The answer wasn't saved:", error);
    }
  };
  for (const [choice, label] of [["yes", "Yes"], ["no", "No"]]) {
    const button = element("button", { className: `chip chip--${choice}`, type: "button" }, label);
    button.addEventListener("click", () => answer(choice));
    block.append(button);
  }
  return block;
}

// "Best time to leave?": the recommended route's safety and time leaving now, in 15 or 30 minutes, or in an hour.
function leaveTimesBlock() {
  const block = element("div", { className: "leave-times" });
  const button = element("button", { className: "chip chip--clock", type: "button" }, "Best time to leave?");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Comparing…";
    try {
      block.replaceChildren(leaveTimesList(await postJson("/leave-times", tripPayload(state.origin, state.destination))));
    } catch (error) {
      block.replaceChildren(element("p", { className: "status status--error" }, error.message));
    }
  });
  block.append(button);
  return block;
}

function leaveTimesList({ times, best_offset_min: best }) {
  const rows = times.map((time) => {
    const when = time.offset_min === 0 ? "Now" : time.offset_min === 60 ? "In 1 hour" : `In ${time.offset_min} min`;
    const why = [
      time.sun_glare_min >= 0.5 ? `${Math.round(time.sun_glare_min)} min sun glare` : "",
      time.school_zones ? `${time.school_zones} school zone${time.school_zones === 1 ? "" : "s"}` : "",
    ].filter(Boolean);
    const row = element(
      "button",
      { className: `leave-time${time.offset_min === best ? " leave-time--best" : ""}`, type: "button", title: "Plan the trip for this time" },
      element("span", { className: "leave-time__when" }, `${when} · ${clockTime(time.leaving_at)}`),
      element("span", { className: "leave-time__why" }, why.join(" · ") || "No sun glare or school zones"),
      element("span", { className: "leave-time__numbers" }, scorePill(time.safety_score), ` ${Math.round(time.duration_min)} min`),
    );
    row.addEventListener("click", () => {
      $("leave").value = String(time.offset_min);
      findRoute();
    });
    return row;
  });
  return element("div", { className: "leave-times__list" }, element("p", { className: "results__heading" }, "Best time to leave · pick one to plan it"), ...rows);
}

// ---- Demo mode: sample trips for an expo screen while no one's using it

function setUpDemo() {
  const fromLink = new URLSearchParams(window.location.search).get("demo");
  let on = fromLink === "1";
  try {
    if (fromLink !== null) localStorage.setItem("routes-demo", on ? "on" : "off");
    else on = localStorage.getItem("routes-demo") === "on";
  } catch {
    // Storage blocked: the link's ?demo=1 still works for this visit.
  }
  state.demo.on = on;
  $("demo-mode").checked = on;
  $("demo-mode").addEventListener("change", (event) => {
    state.demo.on = event.target.checked;
    try {
      localStorage.setItem("routes-demo", state.demo.on ? "on" : "off");
    } catch {
      // Remembered for this visit only.
    }
    if (!state.demo.on) stopDemo();
    restartIdleTimer();
  });
  // Anyone touching the page ends the demo, and the idle countdown starts over.
  for (const type of ["pointerdown", "keydown", "wheel", "touchstart"]) {
    document.addEventListener(type, (event) => event.isTrusted && userIsHere(), { capture: true, passive: true });
  }
  restartIdleTimer();
}

function userIsHere() {
  if (state.demo.running) stopDemo();
  restartIdleTimer();
}

function restartIdleTimer() {
  clearTimeout(state.demo.idleTimer);
  if (state.demo.on && !state.nav.active) state.demo.idleTimer = setTimeout(startDemo, DEMO.idleMs);
}

function startDemo() {
  if (!state.demo.on || state.nav.active || document.querySelector("dialog[open]")) return restartIdleTimer();
  state.demo.running = true;
  demoStep();
}

async function demoStep() {
  if (!state.demo.running) return;
  const trip = DEMO.trips[state.demo.index % DEMO.trips.length];
  state.demo.index += 1;
  for (const stop of [...state.stops]) removeStop(stop.id);
  setPoint("origin", { ...trip.from, countryCode: "US" });
  setPoint("destination", { ...trip.to, countryCode: "US" });
  const banner = $("demo-banner");
  banner.replaceChildren(
    element("strong", {}, `Sample trip: ${trip.from.label} → ${trip.to.label}`),
    element("span", {}, "Touch anywhere to plan your own"),
  );
  banner.hidden = false;
  try {
    const result = await requestTrip(state.origin, state.destination, { stops: [], demo: true });
    if (state.demo.running) showTrip(result);
  } catch (error) {
    console.warn("A sample trip failed:", error);
  }
  if (state.demo.running) state.demo.stepTimer = setTimeout(demoStep, DEMO.showMs);
}

function stopDemo() {
  if (state.demo.running) state.demo.stoppedAt = Date.now();
  state.demo.running = false;
  clearTimeout(state.demo.stepTimer);
  $("demo-banner").hidden = true;
}

// ---- About this app, and the link for phones

// "How it works", with the headline numbers of the latest results summary (python -m app.evaluate).
async function showAbout() {
  const dialog = $("about");
  dialog.showModal();
  if (dialog.dataset.loaded) return;
  dialog.dataset.loaded = "yes";
  try {
    const response = await fetch("results.json", { cache: "no-store" });
    const results = response.ok ? await response.json() : null;
    if (!results || !results.trips) return;
    $("about-results-title").textContent = `Across ${results.trips} Milwaukee test trips`;
    $("about-numbers").replaceChildren(...resultLines(results).map((text) => element("li", {}, text)));
    const day = new Date(`${results.generated}T12:00:00`).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
    $("about-measured").textContent = `Measured ${day}, with that day's closures and traffic.`;
    $("about-results").hidden = false;
  } catch {
    // The explanation stands on its own without the numbers.
  }
}

function resultLines(r) {
  const count = (value) => String(Math.round(value));
  const avoided = r.risky_turns_standard - r.risky_turns_ours;
  const lines = [
    `${r.different} of ${r.trips} trips got a different route than the standard one.`,
    ...(r.safety_score_ours !== undefined
      ? [`Average safety score: ${count(r.safety_score_standard / r.trips)} for the standard routes, ${count(r.safety_score_ours / r.trips)} for ours.`]
      : []),
    `${count(avoided)} risky turns avoided: ${count(r.risky_turns_standard)} on the standard routes, ${count(r.risky_turns_ours)} on ours.`,
  ];
  for (const [key, name] of [["closures", "Closed roads"], ["lane_closures", "Lane closures"], ["school_zones", "School zones"], ["rail_crossings", "Railroad crossings"]]) {
    const [standard, ours] = [r[`${key}_standard`], r[`${key}_ours`]];
    if (standard > ours) lines.push(`${name}: ${count(standard)} on the standard routes, ${count(ours)} on ours.`);
  }
  const minutes = r.minutes_added_per_trip;
  lines.push(
    minutes > 0
      ? `About ${minutes.toFixed(1)} min longer per trip on average: the price of the safer choice.`
      : `No slower on average (${Math.abs(minutes).toFixed(1)} min ${minutes < 0 ? "faster" : "difference"} per trip).`,
  );
  return lines;
}

// A QR code of this page's address, with the access code, to scan with a phone. Phones need
// the public https link from the tunnel (EXPO.md): this computer's own address won't open on them.
async function showPhoneLink() {
  const body = $("phone-body");
  $("phone").showModal();
  if (["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname)) {
    body.replaceChildren(
      element("p", {}, "This page is open at this computer's own address, which phones can't reach."),
      element(
        "p",
        {},
        "Start the tunnel (EXPO.md, “Let visitors use their phones”) and open this page from its https://…trycloudflare.com address. The code to scan will show here.",
      ),
    );
    return;
  }
  const link = tripLink();
  const withTrip = Boolean(state.origin && state.destination);
  body.replaceChildren(element("p", { className: "status" }, "Making the code…"));
  try {
    if (!window.qrcode) await loadScript(QR_LIBRARY);
    const qr = window.qrcode(0, "M");
    qr.addData(link);
    qr.make();
    body.replaceChildren(
      element("img", { className: "qr", src: qr.createDataURL(6, 3), alt: `QR code for ${link}` }),
      element("p", { className: "qr__link" }, link),
      window.location.protocol === "https:"
        ? element("p", { className: "hint" }, "Point the phone's camera at the code, then tap the link it shows.")
        : element("p", { className: "hint" }, "Driving mode on a phone needs the secure https link from the tunnel."),
      ...(withTrip ? [element("p", { className: "hint" }, "The trip on the map comes with it, ready to drive.")] : []),
    );
  } catch {
    body.replaceChildren(element("p", {}, "The code couldn't be made. Visitors can type this address:"), element("p", { className: "qr__link" }, link));
  }
}

// This page's address with the access code and the trip on the map: start, destination and stops.
// Names come along, except "My location", which is somewhere else on the phone.
function tripLink() {
  const params = new URLSearchParams();
  const code = accessCode();
  if (code) params.set("code", code);
  const where = (point) => `${point.lat.toFixed(5)},${point.lng.toFixed(5)}`;
  for (const [key, point] of [["from", state.origin], ["to", state.destination]]) {
    if (!point) continue;
    params.set(key, where(point));
    if (point.label && !/^(My|Current) location$/.test(point.label)) params.set(`${key}Name`, point.label.slice(0, 80));
  }
  for (const stop of chosenStops()) params.append("stop", where(stop));
  const query = params.toString();
  return `${window.location.origin}${window.location.pathname}${query ? `?${query}` : ""}`;
}

// A trip in the page's address (tripLink, from "Open on your phone"): placed on the map and planned.
function openTripFromLink() {
  const params = new URLSearchParams(window.location.search);
  const place = (text, label = null) => {
    const [lat, lng] = (text || "").split(",").map(Number);
    return text && Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? { lat, lng, label, countryCode: null } : null;
  };
  const from = place(params.get("from"), params.get("fromName"));
  const to = place(params.get("to"), params.get("toName"));
  if (from) setPoint("origin", from);
  if (to) setPoint("destination", to);
  for (const text of params.getAll("stop").slice(0, MAX_STOPS)) {
    const stop = place(text);
    if (stop) addStop({ point: stop });
  }
  if (from && to) findRoute();
}

// ---- Small helpers

function showStatus(message, isError = false) {
  $("results").replaceChildren(element("p", { className: `status${isError ? " status--error" : ""}` }, message));
}

function showSetup(problem) {
  const setup = $("setup");
  setup.hidden = false;
  setup.replaceChildren(
    element(
      "div",
      { className: "setup__card" },
      element("h2", {}, "The map needs a key"),
      ...(problem ? [element("p", { className: "status status--error" }, problem)] : []),
      element(
        "ol",
        {},
        element("li", {}, "Copy frontend/config.example.js to frontend/config.js and paste the map key in."),
        element("li", {}, "Allow the key on http://localhost:3143/* and http://127.0.0.1:3143/*."),
        element("li", {}, "Reload this page."),
      ),
      element("p", {}, "Full setup steps are in the README, under “The browser key for the map”."),
      element("p", {}, "The routing server works without it: try it at /docs."),
    ),
  );
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// Build a DOM element. Text is always set as text, never parsed as HTML.
function element(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") node.style.cssText = value;
    else node[key] = value;
  }
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== ""));
  return node;
}
