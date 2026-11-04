// The third-person view while driving a car: a tilted map that follows the road from behind the car,
// with the stop signs, traffic lights and turns coming up. drive.js keeps following the trip as ever;
// this only shows it. Where it can't run (no WebGL, say), the flat map below stays in view.
//
// The map's look comes from OpenFreeMap (no key, with 3D buildings), or OpenStreetMap's own images
// where that doesn't answer. The lights are drawn without a color: nothing here knows if one is red.

import { bearing, loadScript } from "./maps.js";
import { $, COLORS, NAV, element, state } from "./core.js";
import { postJson } from "./app.js";
import { nearestOnPath, spokenDistance } from "./drive.js";

const LIBRARY = "vendor/maplibre/maplibre-gl.js"; // MapLibre GL JS 4.7.1
const STYLESHEET = "vendor/maplibre/maplibre-gl.css";
const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";
const STYLE_WAIT_MS = 4000;
const RASTER_STYLE = {
  version: 8,
  sources: {
    osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" },
  },
  layers: [
    { id: "ground", type: "background", paint: { "background-color": "#dfe4e8" } },
    { id: "osm", type: "raster", source: "osm" },
  ],
};
const CAMERA = { zoom: 18.3, pitch: 68, maxPitch: 70 };
const CAR_AT = 0.5; // the padding above the map's center, as a share of its height: the car sits three quarters down
const LOOK_AHEAD_M = 25; // the road's direction is judged this far on, so the view eases into bends
const TURN_EASE_MS = 450;
const SIGNS_EVERY_MS = 200;
const SIGNS_WITHIN_M = 450;
const PASSED_M = 15;

const view = { map: null, path: null, items: [], from: null, to: null, heading: null, fixAt: 0, spanMs: 1000, lastFrame: 0, lastSigns: 0, frame: 0, shown: null, along: 0, bearing: 0 };

export function driveViewOn() {
  return document.body.classList.contains("driving3d");
}

export async function openDriveView() {
  const box = $("drive-view");
  try {
    if (!document.querySelector(`link[href="${STYLESHEET}"]`)) {
      document.head.append(element("link", { rel: "stylesheet", href: STYLESHEET }));
    }
    if (!window.maplibregl) await loadScript(LIBRARY);
    const style = await mapStyle();
    if (!state.nav.active || view.map) return;
    box.hidden = false;
    const path = state.paths[state.activeRouteId];
    const start = path ? path.points[0] : state.origin;
    view.map = new window.maplibregl.Map({
      container: $("drive-view-map"),
      style,
      center: start,
      bearing: path ? roadBearing(path, 0) : 0,
      ...CAMERA,
      interactive: false,
      fadeDuration: 0,
      attributionControl: { compact: true },
    });
    view.map.on("error", (event) => console.warn("Driving view:", event.error && event.error.message));
    view.map.on("resize", holdTheCar);
    await new Promise((resolve) => view.map.once("load", resolve));
    if (!view.map) return;
    holdTheCar();
    addRouteLine();
    view.bearing = view.map.getBearing();
    view.along = 0;
    view.shown = start;
    document.body.classList.add("driving3d");
    box.scrollIntoView({ block: "start" }); // on a phone, the button that started this is further down
    view.map.resize();
    view.frame = requestAnimationFrame(draw);
  } catch (error) {
    console.warn("No driving view:", error);
    closeDriveView();
  }
}

export function closeDriveView() {
  cancelAnimationFrame(view.frame);
  for (const item of view.items) if (item.marker) item.marker.remove();
  if (view.map) view.map.remove();
  Object.assign(view, { map: null, path: null, items: [], from: null, to: null, heading: null, shown: null });
  $("drive-view").hidden = true;
  $("drive-turn").hidden = true;
  $("drive-ahead").hidden = true;
  document.body.classList.remove("driving3d");
}

// A new position from drive.js: the view moves there over about the time since the last one, along the road.
export function driveViewFix(here, heading) {
  if (!view.map || !view.shown) return;
  const path = currentPath();
  const on = path && nearestOnPath(here, path);
  const now = performance.now();
  view.from = { point: view.shown, along: view.along };
  view.to = { point: here, along: on && on.distance <= NAV.onRouteM ? on.along : null };
  view.heading = heading;
  view.spanMs = Math.min(2000, Math.max(200, now - view.fixAt));
  view.fixAt = now;
}

async function mapStyle() {
  try {
    const response = await fetch(STYLE_URL, { signal: AbortSignal.timeout(STYLE_WAIT_MS) });
    if (response.ok) {
      const style = await response.json();
      style.layers = style.layers.filter((layer) => !layer.id.startsWith("poi")); // shops and parking only clutter the road
      return style;
    }
  } catch {
    // The look below needs no style of its own.
  }
  return RASTER_STYLE;
}

function holdTheCar() {
  view.map.setPadding({ top: view.map.getContainer().clientHeight * CAR_AT, bottom: 0, left: 0, right: 0 });
}

function addRouteLine() {
  const line = { type: "Feature", geometry: { type: "LineString", coordinates: [] } };
  view.map.addSource("route", { type: "geojson", data: line });
  const round = { "line-cap": "round", "line-join": "round" };
  view.map.addLayer({ id: "route-edge", type: "line", source: "route", layout: round, paint: { "line-color": "#ffffff", "line-width": 16 } });
  view.map.addLayer({ id: "route", type: "line", source: "route", layout: round, paint: { "line-color": COLORS.winner, "line-width": 11 } });
}

function currentPath() {
  const path = state.paths[state.activeRouteId] || null;
  if (path !== view.path && view.map && view.map.getSource("route")) showPath(path);
  return path;
}

// The followed route's line, its turns at once, and its signs when the server has answered.
function showPath(path) {
  view.path = path;
  for (const item of view.items) if (item.marker) item.marker.remove();
  view.items = [];
  if (!path) return;
  view.map.getSource("route").setData({ type: "Feature", geometry: { type: "LineString", coordinates: path.points.map((p) => [p.lng, p.lat]) } });
  view.items = path.maneuvers
    .filter((maneuver) => arrowFor(maneuver.maneuver))
    .map((maneuver) => ({ kind: "turn", along: maneuver.along, at: maneuver.location, arrow: arrowFor(maneuver.maneuver) }));
  if (!path.encoded) return;
  postJson("/road-controls", { encoded_polyline: path.encoded })
    .then(({ controls }) => {
      if (view.path !== path) return;
      view.items.push(...controls.map((control) => ({ kind: control.kind, along: control.along_m, at: control })));
    })
    .catch((error) => console.warn("No signs for this route:", error));
}

function draw(now) {
  view.frame = requestAnimationFrame(draw);
  const path = currentPath();
  const elapsed = Math.min(100, now - view.lastFrame);
  view.lastFrame = now;
  const moved = place(now, path);
  if (moved) view.map.jumpTo({ center: view.shown, bearing: view.bearing });
  if (now - view.lastSigns >= SIGNS_EVERY_MS) {
    view.lastSigns = now;
    showSigns(path);
  }
  easeBearing(wantedBearing(path), elapsed);
}

// Where the car is drawn now, between the last position and the newest. Whether it changed.
function place(now, path) {
  if (!view.to) return view.bearingMoving;
  const share = Math.min(1, (now - view.fixAt) / view.spanMs);
  const { from, to } = view;
  if (to.along !== null && from.along !== null && path) {
    view.along = from.along + (to.along - from.along) * share;
    view.shown = pointAt(path, view.along);
  } else {
    view.along = to.along;
    view.shown = { lat: from.point.lat + (to.point.lat - from.point.lat) * share, lng: from.point.lng + (to.point.lng - from.point.lng) * share };
  }
  return share < 1 || view.bearingMoving;
}

function wantedBearing(path) {
  if (path && view.along !== null) return roadBearing(path, view.along);
  return view.heading === null ? view.bearing : view.heading;
}

function easeBearing(wanted, elapsed) {
  const turn = ((wanted - view.bearing + 540) % 360) - 180;
  view.bearingMoving = Math.abs(turn) > 0.2;
  if (view.bearingMoving) view.bearing += turn * Math.min(1, elapsed / TURN_EASE_MS);
}

function roadBearing(path, along) {
  return bearing(pointAt(path, along), pointAt(path, Math.min(path.lengthM, along + LOOK_AHEAD_M)));
}

// The point this far along the path.
export function pointAt(path, along) {
  const { points, cumulative } = path;
  let low = 0;
  let high = points.length - 1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (cumulative[middle] <= along) low = middle;
    else high = middle;
  }
  const length = cumulative[high] - cumulative[low];
  const share = length ? Math.max(0, Math.min(1, (along - cumulative[low]) / length)) : 0;
  return { lat: points[low].lat + (points[high].lat - points[low].lat) * share, lng: points[low].lng + (points[high].lng - points[low].lng) * share };
}

// The signs just ahead, growing as they come closer, and what's next on the card over the map.
function showSigns(path) {
  const along = view.along;
  let nextControl = null;
  for (const item of view.items) {
    const toGo = along === null ? Infinity : item.along - along;
    const near = toGo > -PASSED_M && toGo < SIGNS_WITHIN_M;
    if (near && !item.marker) {
      item.face = signFace(item);
      item.marker = new window.maplibregl.Marker({ element: element("div", { className: "road-sign-spot" }, item.face), anchor: "bottom", pitchAlignment: "viewport", rotationAlignment: "viewport", offset: [34, 0] })
        .setLngLat(item.at)
        .addTo(view.map);
    } else if (!near && item.marker) {
      item.marker.remove();
      item.marker = null;
    }
    if (item.marker) item.face.style.transform = `scale(${Math.max(0.45, Math.min(1.3, 1.35 - toGo / 320))})`;
    if (item.kind !== "turn" && toGo > 0 && (!nextControl || toGo < nextControl.along - along)) nextControl = item;
  }
  showCard(path, along, nextControl);
}

function signFace(item) {
  if (item.kind === "stop") return element("div", { className: "road-sign road-sign--stop", title: "Stop sign" }, element("span", {}, "STOP"));
  if (item.kind === "signal") {
    return element("div", { className: "road-sign road-sign--signal", title: "Traffic light" }, element("i", {}), element("i", {}), element("i", {}));
  }
  return element("div", { className: `road-sign road-sign--turn road-sign--${item.arrow}`, title: "Turn" }, element("i", {}));
}

function showCard(path, along, control) {
  const next = path && along !== null ? path.maneuvers.find((maneuver) => maneuver.along > along + 5) : null;
  const arrow = next && arrowFor(next.maneuver);
  $("drive-turn").hidden = !arrow;
  if (arrow) {
    $("drive-turn").className = `drive-hud__turn road-sign--${arrow}`;
    setText("drive-turn-distance", spokenDistance(next.along - along));
  }
  const soon = control && control.along - along < SIGNS_WITHIN_M;
  $("drive-ahead").hidden = !soon;
  if (soon) {
    setText("drive-ahead-what", control.kind === "stop" ? "Stop sign" : "Traffic light");
    setText("drive-ahead-distance", spokenDistance(control.along - along));
    $("drive-ahead").dataset.kind = control.kind;
  }
}

// Only when it changes: the page's Spanish translates each new text.
function setText(id, text) {
  const node = $(id);
  if (node.dataset.english !== text) {
    node.dataset.english = text;
    node.textContent = text;
  }
}

// Which arrow a turn gets; one that's just straight on gets none.
function arrowFor(maneuver) {
  const name = maneuver || "";
  const side = name.includes("LEFT") ? "left" : name.includes("RIGHT") ? "right" : "";
  if (!side) return name === "MERGE" ? "ahead" : name.startsWith("ROUNDABOUT") ? "ahead" : null;
  if (name.startsWith("UTURN")) return `uturn-${side}`;
  if (name.includes("SLIGHT") || name.startsWith("RAMP") || name.startsWith("FORK") || name.startsWith("MERGE")) return `bear-${side}`;
  return `turn-${side}`;
}
