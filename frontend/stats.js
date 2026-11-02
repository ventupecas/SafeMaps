// The expo's stats screen (stats.html): today's trips and visitors' answers, from GET /expo-stats,
// refreshed every REFRESH_MS. Big numbers for a screen across the booth; no places are ever shown.

import { QR_LIBRARY, $ } from "./core.js";
import { loadScript } from "./maps.js";
import { locale, startTranslating } from "./i18n.js";

const REFRESH_MS = 20000;
const LOCAL = ["localhost", "127.0.0.1", "[::1]"];

startTranslating();
show();
setInterval(show, REFRESH_MS);
showQr();

async function show() {
  let stats;
  try {
    const response = await fetch("/expo-stats", { cache: "no-store" });
    if (!response.ok) throw new Error(`The server answered ${response.status}`);
    stats = await response.json();
  } catch (error) {
    $("board-updated").textContent = "Can't reach the server: showing the last numbers.";
    console.warn("Stats unavailable:", error);
    return;
  }
  const { trips, answers } = stats;
  const day = new Date(`${stats.day}T12:00:00`).toLocaleDateString(locale, { weekday: "long", month: "long", day: "numeric" });
  $("board-day").textContent = `Today at the booth · ${day}`;
  $("stat-trips").textContent = number(trips.trips);
  $("stat-safer").textContent = trips.trips ? `${Math.round((100 * trips.safer_than_standard) / trips.trips)}%` : "–";
  $("stat-safer-label").textContent = trips.trips
    ? `got a safer route than the standard one (${number(trips.safer_than_standard)})`
    : "got a safer route than the standard one";
  $("stat-turns").textContent = number(trips.risky_turns_avoided);
  $("stat-rail").textContent = number(trips.rail_crossings_avoided);
  $("stat-schools").textContent = number(trips.school_zones_avoided);
  $("stat-closures").textContent = number(trips.closures_avoided);
  $("stat-gain").textContent = trips.average_safety_gain === null ? "–" : `+${trips.average_safety_gain}`;
  $("stat-yes").textContent = answers.yes_share === null ? "–" : `${Math.round(answers.yes_share * 100)}%`;
  $("stat-answers").textContent = answers.total ? `${number(answers.yes)} yes · ${number(answers.no)} no` : "No answers yet today";
  $("board-updated").textContent = `Updated ${new Date().toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}`;
}

function number(value) {
  return Number(value || 0).toLocaleString(locale);
}

// A code for phones to open the planner. Only from the public link: this computer's own address
// doesn't open on a phone (see EXPO.md, "Let visitors use their phones").
async function showQr() {
  if (LOCAL.includes(window.location.hostname)) return;
  try {
    if (!window.qrcode) await loadScript(QR_LIBRARY);
    const qr = window.qrcode(0, "M");
    qr.addData(`${window.location.origin}/${accessCode() ? `?code=${encodeURIComponent(accessCode())}` : ""}`);
    qr.make();
    const image = document.createElement("img");
    Object.assign(image, { src: qr.createDataURL(8, 2), alt: "QR code to open the planner on a phone" });
    $("board-qr").replaceChildren(image);
  } catch (error) {
    console.warn("No QR code:", error);
  }
}

// A shared demo's access code (see app.js), so the phone can plan trips too.
function accessCode() {
  const fromLink = new URLSearchParams(window.location.search).get("code");
  try {
    return fromLink || localStorage.getItem("routes-access-code");
  } catch {
    return fromLink;
  }
}
