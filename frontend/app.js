// The web map for the routing engine: pick a trip, draw the ranked routes, explain the choice.
//
// All routing, scoring and traffic lookups happen in the backend (POST /route).
// This page only collects the trip, draws what comes back and explains the
// ranking. The map script is used only for the map and place search.
//
// While driving, the page follows the device's location: it tracks progress
// along the chosen route, switches to another drawn route if the driver takes
// it, and asks the server for a new route (starting the way the car is going)
// after leaving them all.
//
// Nothing on the page names the map provider; its own logo and terms on the
// map, and the credit in the place search list, are required and stay.

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
const COLORS = { winner: "#0b7a53", standard: "#4a5a6a", alternative: "#a9b2bc", closed: "#202124", lanes: "#e37400" };
// Where closures and slowdowns came from, by the server's source names.
const EVENT_SOURCES = {
  wzdx: "Wisconsin work zones (WZDx)",
  wi511: "Wisconsin 511",
  google: "traffic speeds along the routes",
  apify: "traffic tiles",
};
const PROFILE_LABELS = {
  "safe-economy": "Safe & economical (recommended)",
  fastest: "Fastest",
  "highway-averse": "Avoid highways",
  scenic: "Scenic",
  calm: "Calm: extra wary of risky turns",
  eco: "Eco: least fuel",
};

const state = {
  map: null,
  origin: null, // { lat, lng, label, countryCode }
  destination: null,
  pins: { origin: null, destination: null },
  routeLines: [], // { id, lines: [{ line: google.maps.Polyline, zIndex }] }
  turnMarkers: [],
  eventShapes: [], // closures and lane closures drawn on the map
  trafficLayer: null,
  plan: null, // the last plan from the server
  paths: {}, // route id -> { points: [{ lat, lng }], cumulative: [m], lengthM, minutes, label }
  activeRouteId: null, // the route being followed: the winner, or one the driver picked
  nav: {
    active: false,
    watchId: null,
    car: null, // the location marker
    following: true, // the map moves with the car until the driver drags it
    fix: null, // last position
    anchor: null, // where the current heading was measured from
    heading: null, // degrees clockwise from north, once known
    offRoute: 0,
    rerouting: false,
    lastRerouteAt: 0,
    wakeLock: null,
    spoken: new Set(), // directions already announced, so each is said once (far and near)
  },
};

const $ = (id) => document.getElementById(id);

boot();

function boot() {
  loadProfiles();
  wireControls();

  const config = window.ROUTES_CONFIG;
  if (window.ROUTES_CONFIG_MISSING || !config || !config.mapsApiKey) {
    showSetup();
    return;
  }
  window.initMap = initMap;
  // The map script calls this when it rejects the key (wrong website, service not enabled, ...).
  window.gm_authFailure = () =>
    showSetup("The map key was rejected. Check which websites it allows and that the map service is enabled for it.");

  const params = new URLSearchParams({
    key: config.mapsApiKey,
    v: "weekly",
    loading: "async",
    libraries: "places,geometry,marker",
    callback: "initMap",
  });
  const script = document.createElement("script");
  script.src = `https://maps.googleapis.com/maps/api/js?${params}`;
  script.async = true;
  script.onerror = () => showSetup("The map couldn't load. Check the key and your network.");
  document.head.append(script);
}

async function initMap() {
  state.map = new google.maps.Map($("map"), {
    center: MILWAUKEE,
    zoom: 11,
    mapId: window.ROUTES_CONFIG.mapId || "DEMO_MAP_ID",
    mapTypeControl: false,
    streetViewControl: false,
    fullscreenControl: false,
  });
  state.trafficLayer = new google.maps.TrafficLayer();
  state.map.addListener("click", (event) => {
    if (state.nav.active) return; // no accidental trip changes while driving
    // First click sets the start; after that, clicks move the destination.
    const kind = state.origin ? "destination" : "origin";
    setPoint(kind, { lat: event.latLng.lat(), lng: event.latLng.lng(), label: null, countryCode: null });
  });
  state.map.addListener("dragstart", () => {
    if (!state.nav.active) return;
    state.nav.following = false; // the driver is looking around; "Re-center" resumes
    $("recenter").hidden = false;
  });
  await Promise.all([setUpSearch("origin"), setUpSearch("destination")]);
}

// ---- Trip input

async function setUpSearch(kind) {
  const container = $(`${kind}-search`);
  try {
    const { PlaceAutocompleteElement } = await google.maps.importLibrary("places");
    let search;
    try {
      search = new PlaceAutocompleteElement({
        includedRegionCodes: ["us"],
        locationBias: { center: MILWAUKEE, radius: 50000 }, // prefer places around Milwaukee
      });
    } catch (error) {
      console.warn("Place search options not accepted; using defaults:", error);
      search = new PlaceAutocompleteElement({});
    }
    search.setAttribute("placeholder", kind === "origin" ? "Where from?" : "Where to?");
    // Current versions fire "gmp-select"; older ones fired "gmp-placeselect".
    search.addEventListener("gmp-select", (event) => usePlace(kind, event.placePrediction.toPlace()));
    search.addEventListener("gmp-placeselect", (event) => usePlace(kind, event.place));
    container.replaceChildren(search);
  } catch (error) {
    console.warn("Place search unavailable:", error);
    container.textContent = "Place search is unavailable. Click the map instead.";
  }
}

async function usePlace(kind, place) {
  await place.fetchFields({ fields: ["location", "displayName", "formattedAddress", "addressComponents"] });
  const country = (place.addressComponents || []).find((part) => part.types.includes("country"));
  setPoint(kind, {
    lat: place.location.lat(),
    lng: place.location.lng(),
    label: place.displayName || place.formattedAddress,
    countryCode: country ? country.shortText : null,
  });
}

async function setPoint(kind, point) {
  state[kind] = point;
  $(`${kind}-picked`).textContent = point.label || `${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}`;
  await placePin(kind, point);
  updateFindButton();
}

async function placePin(kind, point) {
  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  if (state.pins[kind]) state.pins[kind].map = null;
  const pin = element("div", { className: `map-pin map-pin--${kind}` }, element("span", {}, kind === "origin" ? "A" : "B"));
  state.pins[kind] = new AdvancedMarkerElement({
    map: state.map,
    position: { lat: point.lat, lng: point.lng },
    content: pin,
    title: kind === "origin" ? "Start" : "Destination",
  });
}

function wireControls() {
  $("find").addEventListener("click", findRoute);
  $("clear").addEventListener("click", clearAll);
  $("locate").addEventListener("click", useMyLocation);
  $("drive").addEventListener("click", () => (state.nav.active ? stopDriving() : startDriving()));
  $("recenter").addEventListener("click", recenter);
  $("traffic-layer").addEventListener("change", (event) => {
    if (state.trafficLayer) state.trafficLayer.setMap(event.target.checked ? state.map : null);
  });
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
  for (const kind of ["origin", "destination"]) {
    state[kind] = null;
    if (state.pins[kind]) state.pins[kind].map = null;
    state.pins[kind] = null;
    $(`${kind}-picked`).textContent = "";
  }
  clearRoutes();
  state.plan = null;
  $("results").replaceChildren();
  $("drive").hidden = true;
  setNavStatus(null);
  updateFindButton();
}

// ---- Asking the backend

async function findRoute() {
  if (!state.origin || !state.destination) return;
  const button = $("find");
  button.disabled = true;
  button.textContent = "Finding…";
  showStatus("Comparing routes for safety, fuel and time…");
  try {
    showTrip(await requestTrip(state.origin, state.destination));
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    button.textContent = "Find route";
    updateFindButton();
  }
}

// heading: the direction a moving car is driving, so new routes start that way.
async function requestTrip(origin, destination, heading = null) {
  const payload = {
    origin: { lat: origin.lat, lng: origin.lng },
    destination: { lat: destination.lat, lng: destination.lng },
    profile: $("profile").value,
    country_code: origin.countryCode || destination.countryCode || null,
    live_traffic: $("live-traffic").checked,
    use_router: $("use-router").checked,
    road_events: $("road-events").value,
  };
  if (heading !== null) payload.heading = Math.round(heading * 10) / 10;
  const headers = { "Content-Type": "application/json" };
  const code = accessCode();
  if (code) headers["X-Access-Code"] = code;
  let response;
  try {
    response = await fetch("/route", { method: "POST", headers, body: JSON.stringify(payload) });
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
  drawRoadEvents(trip.road_events);

  const credit = $("osm-credit");
  credit.textContent = trip.attribution.join(" · ");
  credit.hidden = trip.attribution.length === 0;

  const standard = plan.candidates.find(isStandard);
  const notes = driverNotes(trip.notes);
  const usage = trip.google; // the server's count of route data requests
  $("results").replaceChildren(
    winnerCard(plan, standard),
    element("h2", { className: "results__heading" }, "All routes, best first · pick one to follow it"),
    routeList(plan),
    ...(notes.length ? [element("ul", { className: "notes" }, ...notes.map((note) => element("li", {}, note)))] : []),
    element("p", { className: "usage" }, roadEventsLine(trip)),
    ...(trip.school_hours ? [element("p", { className: "usage" }, "School zones count now: it's a school drop-off or pickup time.")] : []),
    element(
      "p",
      { className: "usage" },
      `Route data requests for this trip: ${usage.live_requests}` +
        (usage.daily_limit ? ` (${usage.used_today} of ${usage.daily_limit} today)` : ""),
    ),
  );
  selectRoute(plan.winner.id);
  $("drive").hidden = false;
}

function drawRoutes(plan, fit) {
  const { decodePath } = google.maps.geometry.encoding;
  const bounds = new google.maps.LatLngBounds();
  state.paths = {};
  // Losers first, so the winner is drawn on top.
  const ordered = [...plan.candidates].reverse();
  for (const route of ordered) {
    const path = decodePath(route.encoded_polyline);
    path.forEach((point) => bounds.extend(point));
    state.paths[route.id] = followablePath(path, route, plan);
    const isWinner = route.id === plan.winner.id;
    const lines = [];
    if (isWinner) {
      lines.push(polyline(path, { strokeColor: "#ffffff", strokeWeight: 10, zIndex: 20 }));
      lines.push(polyline(path, { strokeColor: COLORS.winner, strokeWeight: 6, zIndex: 21 }));
    } else if (isStandard(route)) {
      lines.push(dashedPolyline(path, COLORS.standard, 10));
    } else {
      lines.push(polyline(path, { strokeColor: COLORS.alternative, strokeWeight: 5, strokeOpacity: 0.8, zIndex: 5 }));
    }
    state.routeLines.push({ id: route.id, lines: lines.map((line) => ({ line, zIndex: line.get("zIndex") })) });
  }
  if (fit && !bounds.isEmpty()) state.map.fitBounds(bounds, 48);
}

// Which source the closures and slowdowns came from, and which were tried first but unavailable.
function roadEventsLine(trip) {
  const skipped = trip.road_events_skipped.map((name) => EVENT_SOURCES[name] || name);
  const unavailable = skipped.length ? ` (${skipped.join(", ")} unavailable)` : "";
  if (!trip.road_events_source) return `No closure or slowdown data for this trip${unavailable}.`;
  // Sources used together arrive joined with "+".
  const used = trip.road_events_source.split("+").map((name) => EVENT_SOURCES[name] || name).join(" and ");
  // A closure affecting both directions arrives as one event per direction.
  const shown = new Set(trip.road_events.map((event) => `${event.kind}|${event.description}`)).size;
  const counted = shown ? `; ${shown} closure${shown === 1 ? "" : "s"} nearby` : "";
  return `Closures and slowdowns from ${used}${unavailable}${counted}.`;
}

// Closed roads in black, lane closures in orange, with a dot to hover for the details.
async function drawRoadEvents(events) {
  const { decodePath } = google.maps.geometry.encoding;
  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  for (const event of events) {
    const path = decodePath(event.path);
    if (path.length > 1) {
      const color = event.kind === "closed" ? COLORS.closed : COLORS.lanes;
      state.eventShapes.push(
        event.kind === "closed"
          ? polyline(path, { strokeColor: color, strokeWeight: 7, zIndex: 25 })
          : dashedPolyline(path, color, 25),
      );
    }
    const what = event.kind === "closed" ? "Road closed" : "Lane closure";
    const marker = new AdvancedMarkerElement({
      map: state.map,
      position: path[0],
      content: element("div", { className: `event-dot event-dot--${event.kind}` }),
      title: event.description ? `${what}: ${event.description}` : what,
      zIndex: 26,
    });
    state.eventShapes.push({ setMap: (map) => (marker.map = map) });
  }
}

// What following a route needs: its points, the distance to each along it, and its expected time.
function followablePath(path, route, plan) {
  const { computeDistanceBetween } = google.maps.geometry.spherical;
  const points = path.map((point) => ({ lat: point.lat(), lng: point.lng() }));
  const cumulative = [0];
  for (let i = 1; i < points.length; i++) cumulative.push(cumulative[i - 1] + computeDistanceBetween(points[i - 1], points[i]));
  const followable = {
    points,
    cumulative,
    lengthM: cumulative[cumulative.length - 1],
    minutes: route.features.duration_min,
    label: routeLabel(route, plan),
  };
  // Where each turn-by-turn instruction falls along the route, to know which comes next.
  followable.maneuvers = route.maneuvers.map((maneuver) => ({
    ...maneuver,
    along: nearestOnPath(maneuver.location, followable).along,
  }));
  return followable;
}

function polyline(path, options) {
  return new google.maps.Polyline({ map: state.map, path, clickable: false, strokeOpacity: 1, ...options });
}

function dashedPolyline(path, color, zIndex) {
  return polyline(path, {
    strokeOpacity: 0,
    zIndex,
    icons: [{ icon: { path: "M 0,-1 0,1", strokeOpacity: 1, strokeColor: color, scale: 3 }, offset: "0", repeat: "14px" }],
  });
}

async function drawRiskyTurns(route) {
  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  for (const turn of route.features.crossing_turns) {
    if (!(turn.busy || turn.hotspot) || !turn.location) continue;
    const why = turn.hotspot ? "a crash-prone junction" : "a busy junction";
    state.turnMarkers.push(
      new AdvancedMarkerElement({
        map: state.map,
        position: turn.location,
        content: element("div", { className: "turn-dot" }),
        title: `Turn across traffic at ${why}`,
        zIndex: 30,
      }),
    );
  }
}

// Make one route the one to follow, and bring it to the front without changing how it's drawn.
function selectRoute(id) {
  state.activeRouteId = id;
  for (const entry of state.routeLines) {
    for (const { line, zIndex } of entry.lines) line.setOptions({ zIndex: entry.id === id ? zIndex + 100 : zIndex });
  }
  for (const item of document.querySelectorAll(".route")) {
    item.classList.toggle("route--active", item.dataset.id === id);
  }
}

function clearRoutes() {
  for (const entry of state.routeLines) entry.lines.forEach(({ line }) => line.setMap(null));
  for (const marker of state.turnMarkers) marker.map = null;
  for (const shape of state.eventShapes) shape.setMap(null);
  state.routeLines = [];
  state.turnMarkers = [];
  state.eventShapes = [];
  state.paths = {};
  state.activeRouteId = null;
}

function winnerCard(plan, standard) {
  const winner = plan.winner;
  const card = element(
    "section",
    { className: "winner" },
    element("h2", { className: "winner__title" }, `Recommended: ${routeLabel(winner, plan)}`),
  );
  if (winner.reason) card.append(element("p", { className: "winner__reason" }, capitalize(winner.reason)));
  if (standard && standard.id !== winner.id) {
    card.append(element("p", { className: "override" }, comparedWithStandard(winner, standard)));
  }
  card.append(stats(winner));
  return card;
}

function stats(route) {
  const f = route.features;
  const items = [
    ["Time", `${Math.round(f.duration_min)} min${route.live_traffic ? "" : " (est.)"}`],
    ["Distance", `${(f.distance_km * KM_TO_MI).toFixed(1)} mi`],
    ["Fuel", `${(f.fuel_l / LITERS_PER_GALLON).toFixed(2)} gal`],
    ["Risky turns", String(riskyTurns(route))],
  ];
  for (const [label, value] of hazards(f)) items.push([label, String(value)]);
  return element("dl", { className: "stats" }, ...items.map(([label, value]) => element("div", {}, element("dt", {}, label), element("dd", {}, value))));
}

// The hazards a route has besides risky turns, as [label, count], for the ones it has.
function hazards(f) {
  return [
    ["Lane closures", f.lane_closures],
    ["Closed roads", f.closures],
    ["School zones", f.school_zones],
    ["Rail crossings", f.rail_crossings],
  ].filter(([, count]) => count > 0);
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
          element("div", { className: "route__name" }, routeLabel(route, plan)),
          element("div", { className: "route__reason" }, route.reason ? capitalize(route.reason) : route.eligible ? "" : "Too slow to recommend"),
        ),
        element(
          "div",
          { className: "route__numbers" },
          element("div", {}, `${Math.round(f.duration_min)} min · ${(f.distance_km * KM_TO_MI).toFixed(1)} mi`),
          element(
            "div",
            {},
            `${(f.fuel_l / LITERS_PER_GALLON).toFixed(2)} gal · `,
            element("span", { className: risky ? "risky" : "" }, `${risky} risky`),
          ),
          ...hazards(f).map(([label, count]) => element("div", { className: "risky" }, `${count} ${label.toLowerCase()}`)),
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
  const fewer = riskyTurns(standard) - riskyTurns(winner);
  if (fewer > 0) parts.push(`${fewer} fewer risky turn${fewer === 1 ? "" : "s"}`);
  for (const [field, name] of [["lane_closures", "lane closure"], ["school_zones", "school zone"], ["rail_crossings", "rail crossing"]]) {
    const fewerOf = standard.features[field] - winner.features[field];
    if (fewerOf > 0) parts.push(`${fewerOf} fewer ${name}${fewerOf === 1 ? "" : "s"}`);
  }
  if (standard.features.closures && !winner.features.closures) parts.push("avoids a closed road");
  const gallons = (standard.features.fuel_l - winner.features.fuel_l) / LITERS_PER_GALLON;
  if (gallons >= 0.01) parts.push(`${gallons.toFixed(2)} gal less fuel`);
  const minutes = winner.features.duration_min - standard.features.duration_min;
  if (minutes >= 0.5) parts.push(`${minutes.toFixed(1)} min longer`);
  else if (minutes <= -0.5) parts.push(`${(-minutes).toFixed(1)} min faster`);
  return `Instead of the standard route: ${parts.join(", ") || "a different path"}`;
}

// The standard route is the one a typical navigation app would suggest (the API calls it the default).
function isStandard(route) {
  return route.is_google_default;
}

function routeLabel(route, plan) {
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
      if (state.map) state.map.panTo(here);
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
  nav.spoken.clear();
  if (!nav.car) {
    const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
    const content = element("div", { className: "car" }, element("div", { className: "car__arrow" }));
    nav.car = new AdvancedMarkerElement({ content, title: "You", zIndex: 50 });
  }
  nav.watchId = navigator.geolocation.watchPosition(onFix, onFixError, {
    enableHighAccuracy: true,
    maximumAge: 2000,
    timeout: 20000,
  });
  keepScreenOn();
  if (state.pins.origin) state.pins.origin.map = null; // the car marks the start while driving
  $("drive").textContent = "Stop driving";
  document.body.classList.add("driving");
  setNavStatus("Waiting for your location…");
}

function stopDriving(message = null) {
  const nav = state.nav;
  if (nav.watchId !== null) navigator.geolocation.clearWatch(nav.watchId);
  nav.watchId = null;
  nav.active = false;
  if (nav.car) nav.car.map = null;
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
    state.map.panTo(here);
    if (first) state.map.setZoom(NAV.followZoom);
  }
  if (distanceM(here, state.destination) <= NAV.arrivedM) {
    speak("You have arrived.");
    return stopDriving("You've arrived.");
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
    const trip = await requestTrip(origin, state.destination, nav.heading);
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
  const car = state.nav.car;
  car.position = here;
  car.map = state.map;
  const arrow = car.content.firstChild;
  arrow.hidden = heading === null; // just a dot until the direction is known
  if (heading !== null) arrow.style.transform = `rotate(${heading}deg)`;
}

function recenter() {
  const nav = state.nav;
  nav.following = true;
  $("recenter").hidden = true;
  if (nav.fix) {
    state.map.panTo(nav.fix);
    state.map.setZoom(NAV.followZoom);
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

function distanceM(a, b) {
  return google.maps.geometry.spherical.computeDistanceBetween(a, b);
}

function bearing(from, to) {
  return (google.maps.geometry.spherical.computeHeading(from, to) + 360) % 360;
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
