// Visitors' answers to "Would you take this route?" (answers.html): today's, from GET /expo-stats, and
// every day's, from GET /feedback-summary, refreshed every REFRESH_MS. No places are ever shown.

import { $, element } from "./core.js";
import { locale, startTranslating } from "./i18n.js";

const REFRESH_MS = 20000;

startTranslating();
show();
setInterval(show, REFRESH_MS);

async function show() {
  let stats;
  let ever;
  try {
    [stats, ever] = await Promise.all(["/expo-stats", "/feedback-summary"].map(getJson));
  } catch (error) {
    $("answers-updated").textContent = "Can't reach the server: showing the last numbers.";
    console.warn("Answers unavailable:", error);
    return;
  }
  // Restarting today's stats from the control page leaves a rehearsal's answers out of today, not out of the file.
  $("today-title").textContent = stats.since ? `Today since ${new Date(stats.since).toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}` : "Today";
  $("answers-today").replaceChildren(...numbers(stats.answers, "No answers yet today"));
  $("answers-ever").replaceChildren(...numbers(ever, "No answers yet"));
  $("answers-updated").textContent = `Updated ${new Date().toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}`;
}

async function getJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: the server answered ${response.status}`);
  return response.json();
}

function numbers(summary, noneYet) {
  const tile = element(
    "div",
    { className: "tile tile--answers" },
    element("strong", {}, summary.yes_share === null ? "–" : `${Math.round(summary.yes_share * 100)}%`),
    element("span", {}, "said they'd take the recommended route"),
    element("span", { className: "tile__detail" }, summary.total ? `${count(summary.yes)} yes · ${count(summary.no)} no` : noneYet),
  );
  if (!summary.total) return [tile];
  const row = (answer, visitors, gain, minutes) =>
    element("tr", {}, element("th", {}, answer), element("td", {}, count(visitors)), element("td", {}, signed(gain)), element("td", {}, signed(minutes)));
  const heading = (text) => element("th", { scope: "col" }, text);
  return [
    tile,
    element(
      "table",
      { className: "answers__table" },
      element("thead", {}, element("tr", {}, heading("Answer"), heading("Visitors"), heading("Average safety points gained"), heading("Average extra minutes"))),
      element(
        "tbody",
        {},
        row("Yes", summary.yes, summary.safety_gain_when_yes, summary.extra_min_when_yes),
        row("No", summary.no, summary.safety_gain_when_no, summary.extra_min_when_no),
      ),
    ),
  ];
}

function count(value) {
  return Number(value || 0).toLocaleString(locale);
}

// "+3.5", "-1.2" or "0"; no answers to average shows a dash.
function signed(value) {
  if (value === null || value === undefined) return "–";
  return `${value > 0 ? "+" : ""}${value.toLocaleString(locale)}`;
}
