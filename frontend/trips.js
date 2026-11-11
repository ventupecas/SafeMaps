// Whole trips: in a link (for phones), and the recent ones planned in this browser.

import { $, MAX_RECENT, MAX_STOPS, PLACES_KEY, RECENT_KEY, element, state } from "./core.js";
import { accessCode, clearTrip, findRoute } from "./app.js";
import { addStop, chosenStops, removeStop, setPoint } from "./editing.js";
import { setNavStatus } from "./drive.js";
import { setMode } from "./bike.js";

function recentTrips() {
  try {
    const trips = JSON.parse(localStorage.getItem(RECENT_KEY));
    return Array.isArray(trips) ? trips.filter((trip) => trip && trip.from && trip.to) : [];
  } catch {
    return [];
  }
}

export function rememberTrip() {
  if ($("demo-mode").checked || state.demo.running || !state.origin || !state.destination) return;
  const keep = ({ lat, lng, label }) => ({ lat, lng, label: /^(My|Current) location$/.test(label || "") ? null : label });
  const trip = { from: keep(state.origin), to: keep(state.destination), stops: chosenStops().map(keep), mode: state.mode };
  const key = (t) => JSON.stringify([t.from, t.to, t.stops].flat().map((p) => [p.lat.toFixed(4), p.lng.toFixed(4)]));
  const trips = [trip, ...recentTrips().filter((other) => key(other) !== key(trip))].slice(0, MAX_RECENT);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(trips));
  } catch {
    return;
  }
  showRecentTrips();
}

export function forgetRecentTrips() {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch {
    // Nothing kept to forget.
  }
  showRecentTrips();
}

export function showRecentTrips() {
  const trips = $("demo-mode").checked ? [] : recentTrips();
  $("recent").hidden = trips.length === 0;
  const name = (point) => (point.label ? point.label.split(",")[0] : `${point.lat.toFixed(3)}, ${point.lng.toFixed(3)}`);
  $("recent-list").replaceChildren(
    ...trips.map((trip) => {
      const stops = trip.stops && trip.stops.length ? ` · ${trip.stops.length} stop${trip.stops.length === 1 ? "" : "s"}` : "";
      const button = element("button", { className: "recent__trip", type: "button" }, `${name(trip.from)} → ${name(trip.to)}${stops}`);
      button.addEventListener("click", () => loadTrip(trip));
      return element("li", {}, button);
    }),
  );
}

// ---- Home and Work: two places saved in this browser, each picked with one click.
// Unsaved, a button reads "Set Home": it saves the trip's destination (or its start, with no destination yet).
// Saved, it puts the place on the trip: as the start, or as the destination once there's a start. The small
// button beside it forgets the place.

function savedPlaces() {
  try {
    const places = JSON.parse(localStorage.getItem(PLACES_KEY));
    return places && typeof places === "object" ? places : {};
  } catch {
    return {};
  }
}

function keepPlaces(places) {
  try {
    localStorage.setItem(PLACES_KEY, JSON.stringify(places));
  } catch {
    note("This browser won't keep places: its storage is blocked.");
  }
  showSavedPlaces();
}

// A short word over the map, as the map's own notes are.
function note(text) {
  if (state.nav.active) return;
  setNavStatus(text);
  clearTimeout(state.noticeTimer);
  state.noticeTimer = setTimeout(() => state.nav.active || setNavStatus(null), 3500);
}

export function showSavedPlaces() {
  const places = savedPlaces();
  $("saved-places").replaceChildren(
    ...[["home", "Home"], ["work", "Work"]].flatMap(([key, name]) => {
      const place = places[key];
      const usable = place && Number.isFinite(place.lat) && Number.isFinite(place.lng);
      const button = element("button", { className: "saved-place", type: "button" }, usable ? name : "Set " + name);
      button.title = usable ? place.label || "" : "Saves the destination on the map as " + name;
      button.addEventListener("click", () => (usable ? usePlace(place) : savePlace(key, name)));
      if (!usable) return [button];
      const forget = element("button", { className: "link-button link-button--remove saved-place__forget", type: "button" }, "Forget " + name);
      forget.addEventListener("click", () => {
        const rest = savedPlaces();
        delete rest[key];
        keepPlaces(rest);
      });
      return [button, forget];
    }),
  );
}

function savePlace(key, name) {
  const point = state.destination || state.origin;
  if (!point) return note("Pick a place first: then Set " + name + " saves it.");
  const { lat, lng, label, countryCode } = point;
  keepPlaces({ ...savedPlaces(), [key]: { lat, lng, label: /^(My|Current) location$/.test(label || "") ? null : label, countryCode } });
  note(name + " saved.");
}

function usePlace(place) {
  if (state.nav.active) return;
  setPoint(state.origin ? "destination" : "origin", { ...place });
}

// This page's address with the access code and the trip on the map: start, destination and stops.
// Names come along, except "My location", which is somewhere else on the phone.
export function tripLink() {
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
  if (state.mode !== "car") params.set("mode", state.mode);
  const query = params.toString();
  return `${window.location.origin}${window.location.pathname}${query ? `?${query}` : ""}`;
}

// A trip in the page's address (tripLink, from "Open on your phone"): placed on the map and planned.
export function openTripFromLink() {
  const params = new URLSearchParams(window.location.search);
  const place = (text, label = null) => {
    const [lat, lng] = (text || "").split(",").map(Number);
    return text && Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? { lat, lng, label, countryCode: null } : null;
  };
  const from = place(params.get("from"), params.get("fromName"));
  const to = place(params.get("to"), params.get("toName"));
  if (from && to) loadTrip({ from, to, stops: params.getAll("stop").map((text) => place(text)).filter(Boolean) });
}

// A whole trip in place of the one on the map, planned at once.
function loadTrip({ from, to, stops = [], mode = null }) {
  if (mode) setMode(mode);
  if (state.nav.active) return;
  const copy = ({ lat, lng, label = null }) => ({ lat, lng, label, countryCode: null });
  clearTrip();
  for (const stop of [...state.stops]) removeStop(stop.id);
  setPoint("origin", copy(from));
  setPoint("destination", copy(to));
  for (const stop of stops.slice(0, MAX_STOPS)) addStop({ point: copy(stop) });
  findRoute();
}
