// The expo's control page (control.html): whether the server is ready for visitors, how much of
// each day's paid allowance is used, and the booth's two switches (pause demo mode on the screens
// running it; restart today's stats after a rehearsal). Refreshed every REFRESH_MS.
//
// The switches need the admin code (ROUTES_ADMIN_CODE), or without one, this page opened on the
// expo computer itself: the server refuses them from anywhere else.

import { $, element } from "./core.js";
import { locale, startTranslating } from "./i18n.js";

const REFRESH_MS = 15000;
const CODE_KEY = "routes-admin-code";
let control = { demo_paused: false, stats_since: null };

startTranslating();
try {
  $("admin-code").value = localStorage.getItem(CODE_KEY) || "";
} catch {
  // Storage blocked: the code is typed each visit.
}
$("admin-code").addEventListener("change", (event) => {
  try {
    localStorage.setItem(CODE_KEY, event.target.value);
  } catch {
    // Kept for this visit only.
  }
});
$("demo-toggle").addEventListener("click", () => change({ demo_paused: !control.demo_paused }, control.demo_paused ? "Demo mode resumed." : "Demo mode paused."));
$("reset-stats").addEventListener("click", () => {
  if (window.confirm("Count today's stats from now? Earlier trips and answers stay saved but stop counting.")) {
    change({ reset_stats: true }, "The stats count from now.");
  }
});
show();
setInterval(show, REFRESH_MS);

async function show() {
  let health;
  let stats;
  try {
    [health, stats, control] = await Promise.all(["health", "expo-stats", "expo-control"].map(getJson));
  } catch (error) {
    $("control-updated").textContent = "Can't reach the server.";
    $("ready").replaceChildren(check(false, "The server", "not answering: is it running?"));
    console.warn("Control page:", error);
    return;
  }
  const events = health.road_events;
  $("ready").replaceChildren(
    check(true, "The server", "running"),
    check(Boolean(health.road_graph), "The road map", health.road_graph ? `${health.road_graph.area}, ${number(health.road_graph.junctions)} junctions` : "not loaded: build it (EXPO.md)"),
    check(events.matched_ahead, "Closures matched to the map", events.matched_ahead ? "done" : "still matching (about a minute after a start)"),
    check(health.crash_data, "Crash data", health.crash_data ? `${number(health.hazards.crash_locations)} crash locations` : "none loaded (ROUTES_CRASH_DATA)"),
    check(health.hazards.rail_crossings > 0, "Railroad crossings and schools", `${number(health.hazards.rail_crossings)} crossings, ${number(health.hazards.schools)} schools`),
    check(health.road_controls.signal > 0, "Stop signs and lights for the driving view", health.road_controls.signal > 0 ? `${number(health.road_controls.stop)} stop signs, ${number(health.road_controls.signal)} lights` : "none: python -m app.road_controls (EXPO.md)"),
    check(health.weather_alerts, "Weather alerts", health.weather_alerts ? "on" : "off (ROUTES_WEATHER_ALERTS)"),
    ...(health.bus_routes ? [check(health.bus_routes_ready, "Bus routes", health.bus_routes_ready ? "ready" : "not downloaded yet")] : []),
  );
  $("usage").replaceChildren(
    ...(health.google_api_key_set ? [usage("Google route requests", health.google_requests_today, health.daily_limit)] : []),
    ...(events.tomtom_key_set
      ? [usage("TomTom traffic requests", events.tomtom_requests_today, events.tomtom_daily_requests), usage("TomTom traffic map squares", events.tomtom_tiles_today, events.tomtom_daily_tiles)]
      : []),
    ...(health.google_api_key_set || events.tomtom_key_set ? [] : [element("p", {}, "No paid services are set up: nothing to run out of.")]),
  );
  const trips = stats.trips;
  const since = stats.since ? ` since ${new Date(stats.since).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}` : "";
  $("today").textContent = `Today${since}: ${number(trips.trips)} trips, ${number(trips.safer_than_standard)} with a safer route; ${number(stats.answers.yes)} yes, ${number(stats.answers.no)} no. ${control.demo_paused ? "Demo mode is paused on the booth screens." : "Demo mode runs on the booth screens."}`;
  $("demo-toggle").textContent = control.demo_paused ? "Resume demo mode" : "Pause demo mode";
  $("control-updated").textContent = `Updated ${new Date().toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit", second: "2-digit" })}`;
}

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: the server answered ${response.status}`);
  return response.json();
}

async function change(what, done) {
  const headers = { "Content-Type": "application/json" };
  if ($("admin-code").value) headers["X-Admin-Code"] = $("admin-code").value;
  try {
    const response = await fetch("expo-control", { method: "POST", headers, body: JSON.stringify(what) });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `The server answered ${response.status}.`);
    $("control-message").textContent = done;
  } catch (error) {
    $("control-message").textContent = error.message;
  }
  show();
}

function check(ok, name, detail) {
  return element("li", { className: ok ? "control__check control__check--ok" : "control__check control__check--no" }, element("strong", {}, name), ` · ${detail}`);
}

// A bar for one daily allowance: green, amber from 70%, red from 90%.
function usage(name, used, limit) {
  const share = limit ? used / limit : 0;
  const level = share >= 0.9 ? "high" : share >= 0.7 ? "mid" : "low";
  return element(
    "div",
    { className: `control__meter control__meter--${level}` },
    element("span", {}, name),
    element("span", { className: "control__meter-numbers" }, limit ? `${number(used)} of ${number(limit)}` : `${number(used)} (no cap)`),
    element("span", { className: "control__bar", style: `--share:${Math.min(1, share)}` }),
  );
}

function number(value) {
  return Number(value || 0).toLocaleString(locale);
}
