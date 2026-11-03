// Location: the start point, and following the drive (progress, spoken directions, new routes).

import { bearing, distanceM } from "./maps.js";
import { $, M_PER_DEG_LAT, M_PER_MI, NAV, element, state } from "./core.js";
import { requestTrip, showStatus } from "./app.js";
import { chosenStops, placePin, setPoint } from "./editing.js";
import { selectRoute, showTrip } from "./results.js";
import { stopDemo } from "./expo.js";
import { locale, translate } from "./i18n.js";
import { requestBikeTrip, showBikeTrip } from "./bike.js";

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

export function useMyLocation() {
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

export async function startDriving() {
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
  $("drive").textContent = state.mode === "bike" ? "Stop riding" : "Stop driving";
  document.body.classList.add("driving");
  setNavStatus("Waiting for your location…");
}

export function stopDriving(message = null) {
  const nav = state.nav;
  if (nav.watchId !== null) navigator.geolocation.clearWatch(nav.watchId);
  nav.watchId = null;
  nav.active = false;
  if (nav.car) nav.car.remove();
  nav.car = null;
  if (state.origin) placePin("origin", state.origin);
  if (nav.wakeLock) nav.wakeLock.release().catch(() => {});
  nav.wakeLock = null;
  $("drive").textContent = state.mode === "bike" ? "Start riding" : "Start driving";
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
    const bike = state.mode === "bike";
    const trip = bike
      ? await requestBikeTrip(origin, state.destination)
      : await requestTrip(origin, state.destination, { heading: nav.heading, stops: nav.stopsLeft, counted: false });
    if (!nav.active) return;
    state.origin = origin; // its pin returns when driving stops
    $("origin-picked").textContent = origin.label;
    if (bike) showBikeTrip(trip, { fit: false });
    else showTrip(trip, { fit: false });
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
  const utterance = new SpeechSynthesisUtterance(translate(text));
  utterance.lang = locale;
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

export function recenter() {
  const nav = state.nav;
  nav.following = true;
  $("recenter").hidden = true;
  if (nav.fix) {
    state.view.panTo(nav.fix);
    state.view.setZoom(NAV.followZoom);
  }
}

// Keep the screen on while driving, where the browser allows it.
export async function keepScreenOn() {
  try {
    state.nav.wakeLock = await navigator.wakeLock.request("screen");
  } catch {
    // Not supported or refused: the screen may sleep, but following still works while it's on.
  }
}

// The banner over the map while driving; null hides it.
export function setNavStatus(message) {
  const banner = $("nav-banner");
  banner.textContent = message || "";
  banner.hidden = !message;
}

// Distance from point to the path (m) and how far along the path the nearest spot is (m),
// on a flat projection around point: exact enough at the distances that matter here.
export function nearestOnPath(point, path) {
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
