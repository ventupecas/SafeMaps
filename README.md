# Personalized Routing Engine

Expo prototype. For a trip in the Milwaukee metro area, the backend designs
its own routes on OpenStreetMap roads, has Google price them with live traffic
alongside Google's own suggestions, and picks one by **safety first, then fuel,
then time**. The web map just collects the trip and draws the answer.

**Status:** working end to end: Routes client and cache, our own router with live-traffic pricing, priority scoring, the CLI, the HTTP API (FastAPI) and the web map. The web map needs a browser key (see below) before it can show the map.

```
backend/
  app/
    routes_client.py  Google Routes API (computeRoutes) over httpx, through the cache
    cache.py          File-backed response cache: record / replay / off
    usage.py          Local daily cap on live Google requests (ROUTES_DAILY_LIMIT)
    candidates.py     Widens the candidate pool: alternatives, avoid-highways, via-point offsets
    features.py       Raw route JSON -> highway time, winding time, turns, crossing turns, traffic delay
    driving_side.py   Which countries keep left, so "turn across traffic" means the right thing
    hotspots.py       Crash-data index: recorded crashes near a junction
    scoring.py        Pure scoring and ranking; the part you tune
    fuel.py           Rough fuel model: consumption by speed, turns, time in traffic
    profiles.py       Preset personas (fastest, safe-economy [default], highway-averse, scenic, calm, eco)
    overpass.py       Downloads OpenStreetMap roads for an area (Overpass API)
    roadgraph.py      Turns OSM data into a drivable road graph with turn restrictions
    build_graph.py    Command that downloads and builds the graph
    router.py         Our own router: personalized A* search with turn costs, plus alternatives
    road_events.py    Closures, lane closures and jams: WZDx work zones, Wisconsin 511, TomTom, Google traffic speeds, Apify (opt-in)
    traffic_tiles.py  TomTom's traffic map for the OpenStreetMap page, fetched by the server so its key stays private
    pricing.py        Sends our routes to Google for live-traffic times; measures how closely Google followed
    engine.py         The pipeline the API and CLI call
    main.py           HTTP API (FastAPI); also serves the web map
    cli.py            Run and tune from the terminal
    crash_data.py     Downloads fatal crashes (NHTSA FARS), or imports an export of all crashes, for the crash-prone junction rule
    sun.py            Where the sun is, and when a route drives into its glare
    weather.py        National Weather Service alerts, and what they change
    feedback.py       Visitors' "Would you take this route?" answers
    evaluate.py       Runs 20 Milwaukee trips and writes RESULTS.md: what the engine changes
  data/               Downloaded OSM tiles and built graphs (gitignored)
  tests/              331 tests, no network needed; GitHub runs them on every push (.github/workflows/tests.yml)
frontend/
  index.html, app.js, styles.css   The web map: place search, routes drawn and explained, driving mode
  core.js, editing.js, results.js, drive.js, layers.js, trips.js, expo.js
                                   The page's parts (app.js's header says which does what)
  maps.js                          The map underneath: Google's or OpenStreetMap's (ROUTES_MAP)
  config.example.js                Copy to config.js (gitignored) with the browser key
```

## Setup (PowerShell)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env   # then set GOOGLE_MAPS_API_KEY
pytest
```

`pytest` includes browser tests of the web map (`tests/test_browser.py`): clicks, drags, right-clicks, Undo and Swap, against the app on a small test city. They need a browser: `python -m playwright install chromium`, or set `$env:ROUTES_TEST_BROWSER = "msedge"` to use Edge. Without one they're skipped. CI installs Chromium and runs them.

## Google Cloud setup for $0

1. Create a Cloud project and attach a billing account. Google requires one even when usage stays inside the free allowance.
2. Enable the **Routes API**. The legacy Directions API can't be enabled on new projects, and this backend doesn't use it.
3. Create an API key for the backend and restrict it to the Routes API. The web map gets its own browser key (see "Web map and API" below).
4. Cap usage. Quotas are set per API on the project, not on the key, and the Routes API may only offer per-minute limits. The backend therefore enforces its own cap: `ROUTES_DAILY_LIMIT` (default 150) live Google requests per day, counted in the cache folder. Once that's used up, live calls are refused until local midnight, and cached answers keep working. Also add a budget alert under **Billing → Budgets & alerts**. It only sends email; it doesn't stop charges.
5. Leave `ROUTES_TRAFFIC_AWARE=false`. Traffic-aware routing bills at the Pro SKU, which has a smaller free allowance. Every other request this backend sends stays on Essentials.
   See Google's [pricing page](https://developers.google.com/maps/billing-and-pricing/pricing) for the current free caps.

Without the router, each new trip costs 2 Essentials requests (plain + avoid-highways), plus up to 2 via-offset requests when Google offers fewer than 3 distinct routes. A repeat of the same trip costs nothing.

With the router (see below), each trip costs 1 live-traffic request for Google's own suggestions plus 1 per route our router designs: up to 4, all at the **Pro** tier. Live-traffic answers are only reused for 5 minutes in record mode. Size `ROUTES_DAILY_LIMIT` for the expo: 4 × the number of trips you expect.

**Bike routes** (Car / Bike switch at the top of the planner): the lowest-stress route a rider is comfortable on, within a detour they accept, compared with the shortest. See "Bike routes" below and [BIKE_MODE.md](BIKE_MODE.md).

## Web map and API

From `backend/`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 3143
```

Then open **http://127.0.0.1:3143** for the map, or **http://127.0.0.1:3143/docs** to try the API by hand. Keep `--port 3143`: the browser key only works on the addresses it's restricted to.

| Endpoint | What it does |
|---|---|
| `GET /health` | What's loaded (road graph, crash data, cache mode) and today's Google usage |
| `GET /profiles` | The preset profiles, for the profile picker |
| `POST /route` | Ranks routes for one trip. Body: `origin`, `destination` (`{lat, lng}`), optional `stops` (up to 3, in order), `profile`, `weights`, `country_code`, `live_traffic`, `use_router`, `max_extra_min` (the most extra minutes a safer route may take), `departure_offset_min` (leave this many minutes from now). Returns the ranked `plan` (the map draws `plan.winner.encoded_polyline`; each route has a `safety_score` and its `via`, the main road), `notes`, `attribution`, `weather_alerts`, `low_sun` and Google usage. |
| `POST /leave-times` | The same trip leaving now, in 15 or 30 minutes and in an hour: the recommended route's safety score, minutes, sun glare and school zones for each, and the safest time. Our router designs the routes once (no Google) and judges them at each time. |
| `POST /feedback`, `GET /feedback-summary` | A visitor's answer to "Would you take this route?" (`choice`: yes or no, how much safer and slower the route was; no places), and the totals. Kept in `.cache/routes/feedback.jsonl`. |

Errors come back as: 422 bad input, 404 no route, 429 the daily Google limit is used up, 502 Google failed, 503 a replay-mode trip that wasn't recorded.

**The browser key for the map.** The map and the place search run in the browser, so they need a separate key from the backend's:
1. In the Cloud Console, enable **Maps JavaScript API** and **Places API (New)**.
2. Create a new API key. Under **Application restrictions**, pick **Websites** and add `http://localhost:3143/*` and `http://127.0.0.1:3143/*`. Under **API restrictions**, pick those two APIs.
3. Copy `frontend/config.example.js` to `frontend/config.js` and paste the key in. `config.js` is gitignored.

Until then, the page shows these steps in place of the map. On the page:
- Search for the start and destination, or click the map: the first click sets the start, the next the destination. **Use my location** sets the start from the device. A clicked spot is named by the nearest address (Photon on the OpenStreetMap map; on Google's map, the Geocoding API if the key allows it). **Swap** turns the trip around.
- Right-click the map (or press and hold on a phone) to add a stop where it fits the trip best. Drag any pin to move it; click a pin to remove it. **Undo** on the map (or Ctrl+Z) takes back the last change made on it.
- **Español:** the page follows the browser's language, and the **Español / English** button in the header switches it (remembered, keeping the trip). In Spanish, `frontend/i18n.js` translates the page's text as it appears, including the server's route reasons and turn-by-turn directions (spoken with a Spanish voice); Google's map loads in Spanish. Road names, closure descriptions from 511 and weather alert names stay as they come.
- **By keyboard:** Tab to the map and a cross marks its middle; the arrow keys move the map and + and - zoom it. Enter puts the start, then the destination, at the cross, S adds a stop there, and Ctrl+Z undoes. Pins are reached with Tab too: Enter on one removes it. A screen reader hears what was set.
- With routes drawn, a change plans the trip again (**Plan again after each change on the map**, under Advanced). It's on for the OpenStreetMap map and off for Google's, where each plan is a paid request: there a note says the routes are out of date until **Find route** is pressed.
- **Map layers** (top left of the map): Default, Satellite or Terrain, and Traffic, Transit and Biking over it. The choice is remembered in the browser. On satellite imagery and the dark map the gray and black lines get a white edge.
- **Dark map at night** (on by default): from dusk to dawn, worked out from the sun's position in the browser, Default turns dark (CARTO's dark map on the OpenStreetMap map). Google's map can only be made dark when the page opens, so there it's decided on opening.
- **Recent trips:** the last 5 trips planned with **Find route** in this browser, under the hint, one click to plan again; **Forget these trips** clears them. None are kept with **Demo mode** on, so a shared expo screen doesn't show one visitor's trips to the next.
- **Offline copy** (`frontend/sw.js`): the browser keeps the page's files, the map library (served from `frontend/vendor/`, not a CDN) and, on the OpenStreetMap map, each map image viewed (up to 4,000, refreshed weekly), so the page still opens and draws the map without the internet. Google's map and live traffic aren't kept. It needs https or `localhost`/`127.0.0.1`.
- **Open on your phone** carries the trip: the phone opens with the same start, destination and stops, and plans it. The page can be added to a phone's home screen (`manifest.json`, icons in `frontend/icons/`).
- The recommended route is drawn bold green, the standard route (Google's own choice, or our fastest on the OpenStreetMap map) dashed, and the other options in gray. Red dots mark risky turns on the recommended route; red rings mark the standard route's risky turns that the recommended route avoids. Railroad crossings (✕) and school zones at school times (S) are marked too, faded where only the standard route has them (each route's `rail_crossing_points` and `school_points` in the API). The **Map key** in the corner says what each line and marker means; it starts folded on a phone.
- Closed roads are black and lane closures dashed orange. Tap a closure's marker (or a risky turn's) for the details. On the OpenStreetMap map, closure markers close together merge into a numbered circle that splits apart as you zoom in. **Show closures** hides them all.
- Routes are named by their main road, "Via W Wisconsin Ave", from our road map: each route's longest road that no better route already goes by. Below the name: what it is ("Our safer design 2", "Standard route") and the reason for its place.
- **Extra time you'd accept for a safer route:** no fixed limit (a route up to 1.6× the fastest can win), or at most 5, 10 or 15 minutes more. The reasons say which limit applied.
- **Safety score:** every route gets one, 0 to 100, higher is safer: a ring on the recommendation and a pill on each route. It weighs the same risks for every profile (the default's weights: a busy risky turn 3 points, half that where a traffic signal controls the junction, a crash-prone one 9, a lane closure 3, a school zone 3, a rail crossing 1, a weather hazard 3, a minute of sun glare 1, a closed road 30), and 10 points halve it: score = 100 / (1 + points / 10). **What the safety score counts**, under the recommendation, lists each kind's points beside the standard route's (`safety_risks` on each route in the API).
- **Cost and emissions:** fuel shows in gallons and dollars (`ROUTES_FUEL_PRICE`, default $3.25 a gallon) and CO₂ (8.9 kg a gallon, the EPA's figure).
- **Stops:** **Add a stop** (up to 3). The trip is planned leg by leg, and the plan joins each leg's recommended route, and each leg's standard route; the directions say "Arrive at stop N". While driving, a stop reached is left out of new routes.
- **Leave:** now, or in 15, 30 or 60 minutes: school zones and sun glare are judged at that time (traffic and closures stay as they are now). **Best time to leave?** under the routes compares the four and marks the safest; pick one to plan it.
- **Would you take this route?** Yes or No under the recommendation. The answers are counted (`/feedback-summary`) with how much safer and slower the route was, and no places.
- **Demo mode** (under Advanced, or `?demo=1` in the address): after 45 seconds with no one touching the page, it plans four sample trips in turn, 20 seconds each, with a banner. Any touch stops it. Sample trips are planned without live traffic, so they cost no paid requests.
- **New driver** profile: keeps off freeways (a freeway minute feels like three), weighs risky turns, closures, school zones and crossings about twice the default, and accepts up to 1.8× the fastest time.
- The panel lists every route with its minutes, miles, gallons, risky turns and the reason for its place, plus how the winner compares with the standard route. Alternatives take different roads, even when they're much slower; a route that mostly runs along a better one is left out (`plan.hidden` in the API says which).
- Pick a route in the list to follow it (the recommended one is picked to start with).
- **Advanced** (folded) holds the controls visitors don't need: where closures come from, Live traffic, and Our router.
- **"Which route is safer?"** (`/game.html`, linked from the planner's header): a sample trip across Milwaukee, two of its routes as A and B without their scores, and the visitor picks the safer one; then both scores show with what each route has. The routes are planned without live traffic and aren't counted as trips; each guess is counted (right or not, no places: `POST /guess`), and the stats screen shows how often visitors guess right.
- **Control page** (`/control.html`, from `/health`, `/expo-stats` and `/expo-control`): readiness checks, today's paid usage against its caps, and switches to pause demo mode on the screens running it and to restart today's stats. The switches need `ROUTES_ADMIN_CODE` (sent as `X-Admin-Code`), or without it, a request from the server's own computer that didn't come through a tunnel.
- **Stats screen** (`/stats.html`, from `GET /expo-stats`): today's trips, how many got a safer route, what they avoided (risky turns, railroad crossings, school zones, closed roads) and visitors' answers, for a second screen at the expo. Trips are counted without places; sample trips and automatic re-plans aren't counted (`counted` in the trip request).
- **How it works** explains the ranking for visitors, with the headline numbers of the latest results summary (`frontend/results.json`, see "Results summary"). **Open on your phone** shows a QR code of the page's address, with the access code, once the page is opened from the tunnel's https link (see EXPO.md).
- Place search on the OpenStreetMap map works from the keyboard: arrow keys move through the suggestions, Enter picks one, Escape closes the list.
- The page itself never names Google: routes are labeled by road or as "Our safer design", "Standard route" and "Alternative route", and the server's notes and errors are reworded for drivers (the raw notes go to the browser console). Google's logo and terms on the map, and the credit in the place search list, are required by Google's terms and stay.
- Distances are shown in miles and fuel in gallons. The backend works in km and liters.

**Driving.** After a route is found, **Start driving** follows the device's location:
- The map follows a blue dot with an arrow for the direction of travel, and a banner shows miles and minutes left. Dragging the map stops the following; **Re-center** resumes it. The screen is kept on where the browser allows it.
- Drive onto one of the other routes on the map, and the page follows that one instead, with no new request.
- Leave every route for two GPS fixes in a row (more than 40 m off, with a fix accurate to 60 m or better), and the page asks for a new route from where the car is. It sends the car's heading, so the new routes start the way the car is going instead of with a U-turn. New routes are asked for at most every 20 s.
- Within 40 m of the destination, it stops and says so.
- **Cost:** each new route is a full trip request: with Live traffic on, up to 4 Google requests at the Pro tier (a heading also makes Google suggestions Pro). Turn Live traffic off to reroute on our own estimates.
- **Spoken directions:** the banner shows the next turn ("In 0.3 miles: Turn right onto West Wisconsin Avenue") above the miles and minutes left, and the browser reads it aloud about 400 m before the turn and again at it. It also says when it's finding a new route and when you've arrived. Untick **Spoken directions while driving** to keep it quiet. Each route's `maneuvers` in the API carry the instructions, named from the road map.
- **Phones:** browsers only share location with secure pages: `https://`, or `localhost` on the same computer. Opening `http://<this computer's IP>:3143` on a phone won't get a location. See "Sharing the demo over a link" below.

**Two maps: Google's or OpenStreetMap's.** `ROUTES_MAP` in `backend/.env` picks which one the page shows.
- **`google`** (the default): Google's map, place search and live traffic. Google's terms require its logo on its map, so the logo stays.
- **`osm`:** an OpenStreetMap map through [Leaflet](https://leafletjs.com), with no Google logo and no Google at all. Google's terms don't allow its results (routes, places, traffic) on another map, so in this mode:
  - **Place search** uses [Photon](https://photon.komoot.io), which is built on OpenStreetMap data.
  - **Our router designs every route.** The "standard route" to compare with is its fastest one.
  - **Live traffic comes from TomTom and Wisconsin 511 instead** (with `TOMTOM_API_KEY` and `WI511_API_KEY`): see "TomTom: traffic on every street" and "511 travel times" below. Google's Live traffic toggle is hidden.
  - The **Traffic** map layer shows TomTom's traffic map: every street colored green to red by speed, credited "Traffic © TomTom". Without a TomTom key it draws the 511 slowdowns instead.
  - The other layers come from free map services, each credited on the map: satellite imagery from Esri, terrain from OpenTopoMap, bike lanes from CyclOSM and rail lines from OpenRailwayMap. **Transit** also draws Milwaukee County Transit System's bus routes, each in its color (click one for its name; **Only buses near the trip** keeps those within 400 m of the route followed), from MCTS's published schedule (GTFS, free, no key): the server fetches it at most once a week and keeps the lines in the cache folder (`GET /bus-routes`). `ROUTES_BUS_ROUTES=off` leaves them out; any other value is another GTFS feed's address.
  - **Still on:** work zones, 511, crash data, school zones and rail crossings.
  - **Keys:** neither Google key is needed, any port works, and the page credits "© OpenStreetMap contributors".

  Leaflet loads from unpkg.com, map tiles from tile.openstreetmap.org, and search from photon.komoot.io. All three are free for light use like a demo, with fair-use policies, so don't use them for heavy traffic. TomTom's traffic tiles come through this server, so the TomTom key never reaches a browser.

**Sharing the demo over a link.** A free tunnel gives the laptop a public https address, so visitors can open the app on their phones, driving mode included. Anyone with the address could use your Google requests, so set an access code: with `ROUTES_ACCESS_CODE=...` in `backend/.env`, route requests without it are refused (401). Share the address with `?code=...` on the end; the page remembers the code. EXPO.md has the step-by-step instructions.

## Bike routes

With the bike map built, the planner gets a **Car / Bike** switch:

```powershell
.\.venv\Scripts\python.exe -m app.build_graph milwaukee-metro --bike      # the bike map: roads plus trails and paths
.\.venv\Scripts\python.exe -m app.crash_data milwaukee-metro --cyclists   # fatal crashes where a cyclist died (FARS)
```

The first writes `data/graphs/milwaukee-metro-bike.json.gz`, which the server loads by itself (`ROUTES_BIKE_GRAPH` to point elsewhere, `off` for no bike mode); the second prints the `ROUTES_BIKE_CRASH_DATA` line for `backend\.env`. The bike map takes about 240 MB of memory in each server process.

- **Stress, not speed:** every street and trail gets a Level of Traffic Stress, 1 (anyone: trails, protected lanes, quiet streets) to 4 (only the most confident: fast, multi-lane traffic), from its speed limit, lanes and bike lane or trail. Where OpenStreetMap doesn't say, the worse case is assumed. Freeways and sidewalks are never used; footways only where bikes are allowed.
- **The route:** our router searches the bike map with each stretch costing its riding time times a weight for its stress, and crossing a busy (stress 3 or 4) road without a signal costing extra, as do places where a cyclist was killed and railroad tracks. Closures are avoided; a lane closure counts as one stress level worse.
- **Riders:** Most adults (the default: some traffic is fine), Family (the quietest streets, long detours accepted), Confident (busier streets are fine) and E-bike (like most adults, faster). Each has a detour limit, such as 1.5 times the shortest time and at most 15 minutes more for most adults.
- **The page:** the recommended route colored by stress, its busy crossings marked (and the shortest route's it avoids), minutes at each stress level, the safety score and what it counts, beside the shortest route. Turn-by-turn and spoken directions work as for cars. **Bike stress** under Map layers colors every street in view (zoomed in). From December to March, a note says trails may not be cleared.
- **Stops and the dark:** trips with stops are planned leg by leg, as for cars. After dark, unlit stretches cost more and count in the score: lit as OpenStreetMap says, else streets count as lit and trails as unlit.
- **Not yet:** hills. Trails come from a separate download (see BIKE_MODE.md, "Status").
- **Results:** `python -m app.bike_evaluate` plans 20 Milwaukee bike trips on the running server and compares each with the shortest route, into [BIKE_RESULTS.md](BIKE_RESULTS.md).
- **All bike crashes:** a Community Maps export (see "All reported crashes") cut down to bike crashes: `python -m app.crash_data import <file> --cyclists`.
- **API:** `POST /bike-route` (`origin`, `destination`, `profile`), `GET /bike-stress?south=&west=&north=&east=`.

## Walking and bus trips

The mode switch adds **Walk** with the walking map, and **Transit** with the bus schedule; each shows only when the server has it.

```powershell
.\.venv\Scripts\python.exe -m app.build_graph milwaukee-metro --walk          # the walking map: streets, sidewalks, paths, stairs
.\.venv\Scripts\python.exe -m app.crash_data milwaukee-metro --pedestrians    # fatal crashes where a pedestrian died (FARS)
```

- **Walking** works like biking, with stress from sidewalks instead of traffic: 1 is a sidewalk or path (or a quiet street), 2 a quiet street without a sidewalk, 3 a busy street where no sidewalk is mapped, 4 a busy street without one. Crossing a busy road without a signal costs extra, as do places where a pedestrian was killed and, after dark, unlit stretches. Walkers: Most people (the default), Careful (slower, the calmest streets) and Step-free (no stairs, for a wheelchair or stroller). One-way streets go both ways on foot. The walking map is `data/graphs/milwaukee-metro-walk.json.gz` (`ROUTES_WALK_GRAPH`, `off` for none; `ROUTES_WALK_CRASH_DATA` for the crashes).
- **Transit** plans bus trips from MCTS's schedule, the same GTFS feed the Transit layer draws (`ROUTES_BUS_ROUTES`), fetched at most once a week into the cache folder and read when the server starts (a minute or two in the background; trips wait for it, nothing else does). The search (RAPTOR) finds the fastest way with one bus, with two, and with three, walking up to 800 m to and from the stops (1,600 m where no bus is nearer) and 250 m between stops to transfer. Each option's walks are then planned on the walking map, so they count as walking routes do; waiting at a stop after dark counts 0.2 points a minute and each transfer 1 point. Of the options arriving within 10 minutes (or a quarter of the trip) of the earliest, the safest is recommended. The page shows each option step by step (walk, get on, ride, get off) with its stops on the map; the times are the schedule's, not live. `ROUTES_TRANSIT=off` skips the schedule (it takes about 200 MB of memory while it's read).
- **API:** `POST /walk-route` (as `/bike-route`; profiles `everyday`, `careful`, `stepfree`), `POST /transit-route` (`origin`, `destination`, `departure_offset_min` or `leave_at`; 503 while the schedule is loading).

## Running the engine

From `backend/` with the venv active:

```powershell
# Rank with one profile
python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430 --profile scenic

# See which route every preset picks
python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430 --all-profiles

# Tweak weights on the fly (no extra API calls)
python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430 --profile scenic --set scenic_weight=0.9 --set max_detour_ratio=2

# The exact JSON the HTTP API will return
python -m app.cli --from 37.7955,-122.3937 --to 37.4419,-122.1430 --profile calm --json
```

The output marks Google's own default route with `(G)` and says whether the profile overrode it.

## Caching

Every Routes API response is saved to `backend/.cache/routes/` as one JSON file per distinct request. The request is stored next to the response, but the API key never is. Set the mode with `ROUTES_CACHE_MODE` or `--cache-mode`:

| Mode     | Cache hit       | Cache miss                     | Use it for                                 |
|----------|-----------------|--------------------------------|--------------------------------------------|
| `record` | served from disk | calls Google, saves the answer | development (default)                      |
| `replay` | served from disk | error, no call made            | scripted demos and weight tuning; guarantees $0 |
| `off`    | n/a             | always calls Google            | checking live behavior                     |

The weight-tuning loop:

1. Run each demo trip once in `record` mode.
2. Switch to `replay` and iterate on `--set` overrides or `app/profiles.py` as much as you like.
3. Once the weights feel right, copy them into the presets.

Entries expire after 30 days (`ROUTES_CACHE_MAX_AGE_DAYS`) because Google's terms limit how long Maps content may be stored. Live-traffic answers are fetched again in record mode once they're older than 5 minutes (`ROUTES_TRAFFIC_FRESH_MINUTES`); replay mode still serves them as recorded. The cache directory is gitignored. Delete it to start fresh.

## How a route is chosen

1. **Candidates.** Google returns at most about three routes, often just one. `candidates.py` also asks for an avoid-highways variant. If there are still fewer than 3 distinct routes, it pushes a via waypoint sideways from the midpoint of the trip, so Google routes through a different corridor.
2. **Features.** For each route, `features.py` measures:
   - highway minutes: steps whose no-traffic speed is at least 80 km/h
   - winding minutes: non-highway steps that turn at least 120° per km, used as a stand-in for "scenic"
   - decision maneuvers: turns, merges, forks and roundabouts
   - crossing turns: turns across oncoming traffic, flagged as busy or crash-prone (see below)
   - congestion delay: only when traffic-aware routing is on
   - fuel (liters): each stretch's distance at its average speed (consumption is highest when crawling, lowest around 60 km/h, and rises again at highway speeds), plus each turn's slow-down and speed-up, plus each traffic signal passed (counted on the road graph, since Google's route data doesn't list them), plus stop-and-go in traffic. The model in `fuel.py` approximates a midsize sedan; tune it for the demo car.
3. **Rank.** The default profile ranks by priority: safety, then fuel, then time (see "Default priorities" below). The other presets use a weighted score, where every term is in *perceived minutes*:

   ```
   cost = time_weight * minutes
        + distance_weight * km
        + fuel_weight * fuel_liters
        + highway_weight * highway_minutes
        + traffic_weight * delay_minutes
        + maneuver_weight * turns
        + busy_crossing_weight * busy_crossing_turns
        + hotspot_crossing_weight * crash_prone_crossing_turns
        - scenic_weight * winding_minutes
   ```

   Routes slower than `max_detour_ratio` times the fastest are shown but can't win. The lowest cost wins.

The 80 km/h, 120°/km and 50 km/h thresholds in `features.py` are first guesses. Calibrate them against real routes you've recorded.

## Turn safety: near-side turns at busy and crash-prone junctions

Turns across oncoming traffic cost extra where they're risky. These are lefts where traffic keeps right and rights where it keeps left.

- **Which turns count.** Normal, sharp and slight turns to the far side, U-turns in either direction, and a far-side ramp taken from a surface street. Roundabouts, forks, merges and ramps that exit a divided highway don't count.
- **Driving side.** The CLI takes it from `--country` (two-letter ISO code), else `ROUTES_DEFAULT_COUNTRY`, else keep-right. The web frontend will send the country from the user's Places selection, so no extra API call is needed.
- **Busy junction.** Google's free tier doesn't report traffic volume at each junction, so road class stands in for it.
  - **With the road graph loaded:** each turn on every route, Google's included, is matched to the OSM junction within 30 m. The junction is busy when a tertiary road or bigger meets there (`BUSY_CLASSES` in `roadgraph.py`). This is the same rule the router uses.
  - **Freeway splits and merges never count:** a junction where only freeways and their ramps meet, or a turn with freeway speed on both sides and no junction nearby, is an interchange with no oncoming traffic.
  - **One-way streets:** where every road at a junction is one-way, no turn crosses oncoming traffic. Except on a divided road: when the same road runs the other way within 50 m (`DIVIDED_ROAD_M`), its oncoming traffic is just across the median, so turns there count as usual.
  - **U-turns through a median opening:** OSM draws a median opening as a short link between the two carriageways. The router treats a turn through any segment shorter than 30 m (`CONNECTOR_MAX_M`) as one maneuver, judged from the road before it to the road after it, so coming out pointing back the way the car came is one U-turn across traffic, not a left ramp and a slight left. It's costed like any U-turn (25 s, plus the busy and crash-prone penalties), and the scorer sees the same U-turn on the route.
  - **Without the graph:** a junction is busy when either road's no-traffic speed is 50 km/h or more.
- **Crash-prone junction.** At least `ROUTES_CRASH_MIN_COUNT` recorded crashes within 35 m. Point `ROUTES_CRASH_DATA` at a CSV with `lat,lng[,crashes]` columns. Without crash data, only the busy rule applies.
  - **For Milwaukee:** `python -m app.crash_data milwaukee-metro` downloads NHTSA's Fatality Analysis Reporting System (FARS, public, no key) for 2020–2024 (`--years` to change). It keeps the area's fatal crashes (612 for 2020–2024) and writes `data/crashes/milwaukee-metro.csv`. These are fatal crashes only, so a single one marks a junction: set `ROUTES_CRASH_MIN_COUNT=1`. About 1,000 busy junctions in the road map are within 35 m of one.
  - **All reported crashes (optional):** Wisconsin's Community Maps (UW-Madison TOPS Lab, <https://transportal.cee.wisc.edu/partners/community-maps/crash/>) has every police-reported crash with its location, and a CSV download. Its terms allow research and traffic-safety use, forbid automated or bulk extraction, and ask users to protect personal information, so export the Milwaukee-area crashes from the website by hand, then run `python -m app.crash_data import <the file>.csv`. It finds the latitude, longitude, severity and date columns by name, weighs crashes by severity (fatal 3, serious injury 2, other injury 1; property damage only is left out unless `--include-property-damage`), writes `data/crashes/milwaukee-metro-all.csv`, and prints the `ROUTES_CRASH_DATA` and `ROUTES_CRASH_MIN_COUNT` lines to use (about two injury crashes a year at a junction). For questions about the data: community-maps@topslab.wisc.edu. (Milwaukee's own open crash dataset has no coordinates, so it can't be used.)
  - **Elsewhere:** many cities publish all crashes, for example NYC's "Motor Vehicle Collisions", Chicago's "Traffic Crashes" and the UK's STATS19. Filter to your area first.
- **Weights.** By default each busy crossing turn costs 3 perceived minutes, and a crash-prone one costs 6 more. The `calm` preset raises these to 4 and 8. `fastest` sets both to 0 so it stays a pure-time baseline.

`--turns` lists every crossing turn with its location, road speed and nearby crash count, which is the quickest way to calibrate:

```powershell
python -m app.cli --from 51.5007,-0.1246 --to 51.5194,-0.1270 --country GB --profile calm --turns
```

## Default priorities: safety, then fuel, then time

The default profile, `safe-economy` (used by the CLI and the web map), ranks in strict order:

1. **Safety.** The route with the fewest risky turns and lane closures wins. A risky turn is a turn across traffic at a busy junction, worth 3 points, or at a crash-prone one, worth 6 more. A lane closure (a forced merge in a work zone or past a crash) is worth 3 (`lane_closure_weight`). A route through a closed road can't win at all (see Road events).
2. **Fuel.** Among equally safe routes, the one using the least fuel wins. Routes within 3% of the lowest count as tied (`fuel_tolerance`), because the fuel model is rough.
3. **Time.** Among those, the fastest wins.

Routes slower than 1.6× the fastest (`max_detour_ratio`) are shown but can't win, so the order never turns into a long detour. Each route carries a short `reason` for its place, for example "as safe as any, and uses the least fuel" or "more risky turns than the winner". The CLI and the web map both show it.

Our router designs with the same priorities: it weights risky turns 4× and fuel 2.5× more heavily than the profile's own weights when planning (`PRIORITY_*_BOOST` in `router.py`), so the routes it proposes suit this ranking. Left turns at quiet junctions aren't penalized beyond their real time and fuel.

For example, on downtown Milwaukee → Waukesha with live traffic, our route came out about 0.5 minutes slower than Google's choice, with the same fuel and one fewer busy left turn, and was recommended for that reason.

The other presets use the weighted score instead. `fastest` ignores safety and fuel, for comparison.

## Road events: closures, lane closures and jams

Five sources, in the order of preference set by `ROUTES_ROAD_EVENT_SOURCES` (default `wzdx+wi511+tomtom,google`). A trip uses the first entry that is set up and answers, so the later ones are backups. `a+b` uses both together, when either answers. With the default, a trip gets WZDx work zones plus 511 incidents (once you have the 511 key) plus TomTom's traffic on the OpenStreetMap map (once you have the TomTom key), and falls back to Google's traffic speeds on Google's map only if none answers. Each map takes traffic from its own provider: `google` and `apify` are skipped on the OpenStreetMap map, and `tomtom` on Google's. The web map's **Closures and slowdowns** picker, or the API's `road_events` field, can also pick one source for a trip, or none. The response says which sources were used (`road_events_source`, joined with `+`) and which were tried but unavailable (`road_events_skipped`).

| Source | What it gives | Needs | Cost and caveats |
|---|---|---|---|
| `wzdx` Work Zone Data Exchange | Every work zone in Wisconsin (WZDx 4.2): its stretch of road, direction, dates, and which lanes are closed | Nothing: `https://511wi.gov/api/wzdx` is public | Free, public domain (CC0). About 6 MB, fetched at most every 5 minutes; if a refresh fails, the last copy is used for up to an hour. Work zones only: no crashes or incidents. `ROUTES_WZDX_FILE` reads a saved copy instead, for an offline demo. |
| `wi511` Wisconsin 511 | Official closures, lane closures, construction and incidents, statewide | `WI511_API_KEY` (free; see below) | Free. The feed allows 10 calls a minute, so it's fetched at most every 5 minutes for all trips. Used with `wzdx`, its own roadwork entries are dropped: they're the same work zones in less detail. |
| `tomtom` TomTom traffic | Jams with their delay, closures, lane closures and crashes on every road, side streets included | `TOMTOM_API_KEY` (free plan; see below); the OpenStreetMap map | Free up to 2,500 incident requests and 200,000 traffic tiles a month. One request covers the whole metro, made at most every 10 minutes and only while trips are planned, capped at `ROUTES_TOMTOM_DAILY_REQUESTS` (80) a day. If a refresh fails, the last copy is used for up to 30 minutes. |
| `google` traffic speeds | Slow and jammed stretches along Google's own suggested routes (`TRAFFIC_ON_POLYLINE`) | Live traffic on, our router, the Google key | No new account. Added only to the one suggestions request per trip, but Google bills a traffic-on-polyline request at a higher rate: possibly the Enterprise tier (1,000 free a month) rather than Pro (5,000). Reports no closures. |
| `apify` traffic tiles | Slowdowns and fully closed roads from Google Maps' traffic tiles, around the trip | Listed in `ROUTES_ROAD_EVENT_SOURCES` and `APIFY_TOKEN` set | Paid per tile ($0.001–$0.01; the actor's page lists both), plus Apify platform fees. Up to 81 tiles a trip, at most `ROUTES_APIFY_DAILY_TILES` (300) a day, cached 10 minutes. **Scraping Google Maps is against Google's terms**, which puts this app's Google keys at risk. A first fetch takes tens of seconds while the actor starts. Off unless both set. |

What each kind of event does:
- **Closed road:** our router won't use it (except the segment the car is already on). A route through one, by the scorer's reading, can't win, unless every route goes through one, in which case the data is doubted instead.
- **Lane closure:** the segments cost 1.5× their time, and driving into one costs `lane_closure_weight` once, like a risky turn. It counts toward safety, so in the default ranking a route through a lane closure loses to an equally fuel-efficient one without.
- **Jam / slow:** the segments cost more when our router designs routes, so the designs go around them: by the measured slowdown (511 travel times), by the delay over the stretch's usual time (TomTom's jams), or else 3× / 1.5×. They aren't scored separately: travel times already include them.
- **Matching to the map:** sources often give only the two ends of a stretch. Each end is snapped to the road segment there, running the event's direction, and the event becomes the shortest drive between them along the roads, if that's at most 1.5× the straight distance plus 500 m. Otherwise it stays as given. A stretch affecting both directions becomes one event per direction. A single point matches the nearest road within 30 m. Each event is matched once and remembered. At startup the server fetches the events across the whole road map and matches them in the background (about 40 s for the metro), so trips don't wait for it; `/health` shows `"matched_ahead": true` once that's done.
- **What the map shows:** closed roads in black and lane closures in dashed orange, for the closures within about 300 m of a route shown. The rest of the trip's area (5 km around it) would bury the routes. A closure of both directions of a two-way street is drawn once.
- **Which work zones count:** WZDx entries closing all lanes are closures. "Some lanes closed" (only when a travel lane is closed, not just the shoulder), flagging, alternating one-way traffic and temporary signals are lane closures. Planned work that hasn't started, and suggested detours, are left out. A reduced speed limit makes the work zone's segments slower to match, and workers on site make it count 1.5× as risky.
- **Ramps and mainlines:** descriptions say which road an event is on ("Ramp Full Closure on I-94 EB at OFF RAMP TO HAWLEY RD", "Mainline Left Lane Closed on ..."). A mainline event never matches a ramp. Ramp closures are placed on the freeway beside the ramp, so they're matched to the ramp itself:
  - **Exits and entrances:** the junction where an exit leaves the freeway (or an entrance joins it) heading the event's way, whose ramp reaches the named street, within 1.5 km. The whole ramp is closed, up to the street or a split, and a route only counts as using it if it passes both ends: the first stretch of a ramp runs right beside the freeway.
  - **Freeway-to-freeway ramps** ("System Interchange Full Closure on WIS 175 SB at WIS 175 SB TO I-94 WB"): the ramp that leads onto that freeway in that direction, from where it splits from ramps going elsewhere.
  - **No street named:** the nearest such ramp within 300 m.
  - **No ramp found:** the event is left out rather than closing the freeway. This happens where the ramp isn't on the map, for example ramps rebuilt in the I-94 East-West project after the map was downloaded.

**511 travel times: live traffic without Google.** With the 511 key, the `wi511` source also reads 511WI's travel times, at most every 5 minutes. These are the current and normal travel time of about 220 Milwaukee-area segments, mostly freeways and main highways.
- **Which segments count:** one taking 1.25× its normal time or more is slow, and 2× or more is jammed. Slowdowns are capped at 6×, since higher readings tend to be a detour sign's time rather than traffic.
- **Router:** it slows each segment by its measured factor, so designs go around jams.
- **Trip times:** routes not priced with Google's live traffic (the OpenStreetMap map, or Live traffic off) get the measured delay for the share of each slow segment they drive, and the fuel it burns. TomTom's jams add their delay the same way.
- **Map:** each segment is matched to the road through its waypoints, one to the next. Segments no road matches are left out (about 1 in 5), and the rest draw orange (slow) or red (jammed) under the **Traffic** map layer on the OpenStreetMap map, when there's no TomTom key.
- **With TomTom:** TomTom's jams cover the same freeways and every street besides, so the travel times are left out and 511 gives its closures and incidents only.

**TomTom: traffic on every street.** 511's travel times cover freeways and main highways only. TomTom's traffic incidents cover city streets too. On a weekday afternoon in October 2026 they listed 368 incidents in the metro, 312 of them on streets without a highway number.
- **What counts:** TomTom's road closures are closures. Lane closures count when a travel lane is closed, not just the shoulder. Crashes count as lane closures: the scene takes lanes for a while and invites another crash. Jams carry their delay: "stopped" and "backed up" traffic are jams, "slow traffic" is slow. Roadwork beside the road, a car on the shoulder, and weather are left out.
- **Thinned out:**
  - **Once per closure:** TomTom lists a closure once per direction. Two lines, each the other reversed (ends within 60 m), become one closure of both directions.
  - **Off our map:** closures on roads our map doesn't have (alleys, parking lots, ramps rebuilt since the map was downloaded) are left out, as they can't affect a route. That's about 1 in 10.
  - **Reported twice:** a TomTom closure or lane closure with 3/4 of its stretch on an official one (WZDx, 511) is the same closure. The official one is kept, since it says more (workers on site, the speed limit, which ramp).
  - **One source of traffic:** with TomTom, 511's travel times are left out, so a jam isn't counted twice.

  On that afternoon: 368 incidents, 293 events after the first two rules, 265 on our map, and 29 more already reported by the work zone feed.
- **Traffic map:** with `ROUTES_MAP=osm`, the **Traffic** map layer shows TomTom's traffic tiles. The page asks this server for them (`/traffic-tiles/{z}/{x}/{y}.png`), which fetches each from TomTom with the key and keeps it for 3 minutes for everyone looking at that spot. At most `ROUTES_TOMTOM_DAILY_TILES` (6,000) are fetched a day; a map view takes about 20. With an access code set, the tiles need it too.
- **Usage:** `/health` shows `tomtom_requests_today` and `tomtom_tiles_today`. The daily caps keep a month inside the free plan: 80 × 31 = 2,480 requests, 6,000 × 31 = 186,000 tiles. The last answer is also kept in `.cache/routes/tomtom-incidents.json`, so a server restarted within 10 minutes (with `--reload`, every saved code change restarts it) uses it instead of asking again.

**Getting the TomTom key:**
1. Create a free account at <https://developer.tomtom.com> (no credit card).
2. In the dashboard, copy the API key it made, or create one under **Keys**.
3. Put it in `backend/.env` as `TOMTOM_API_KEY=...`, set `ROUTES_MAP=osm`, and restart the server. `/health` shows `"tomtom_key_set": true`.

Read TomTom's terms before showing its data publicly.

**Getting the Wisconsin 511 key** (from 511wi.gov's developer pages):
1. Create a My 511WI account at <https://511wi.gov/my511/register> and confirm it from the email.
2. Log in at <https://511wi.gov/my511/login>, open <https://511wi.gov/developers/doc>, and under **Developer API Key**, submit the reason you need it (for example: "a safety-first route planner prototype for the Milwaukee area").
3. The key arrives by email once 511WI approves it. The site gives no wait time.
4. Put it in `backend/.env` as `WI511_API_KEY=...` and restart the server. `/health` shows `"wi511_key_set": true`.

The access agreement (linked on the doc page) wasn't reviewed here: read it before showing the data publicly.

## Sun glare and weather

**Sun glare.** Driving toward a low sun, around sunrise and sunset, is a known crash factor: the glare hides signals, brake lights and people crossing. `app/sun.py` works out the sun's position from the time and place (NOAA's formulas, no service needed). While it's up but lower than 25°, every minute a route drives within 25° of its direction counts as a minute of sun glare: 1 risk point a minute (`sun_glare_weight`). Our router's designs steer away from it too. Clouds aren't known, so a cloudy evening still counts.

**Weather.**
- **On the roads:** TomTom's fog, ice, rain, wind and flooding reports become weather hazards on their stretches (OpenStreetMap map). Our router drives them 1.3× slower, entering one costs `weather_hazard_weight` (3), and the map draws them in purple.
- **After dark:** leaving after dusk (the sun more than 6° below the horizon at the start), turns across traffic count 1.25× as risky, in the score and the ranking: gaps in oncoming traffic are judged by headlights alone. Each route says so (`after_dark`), the page notes it, and **Best time to leave?** shows which times are after dark. Counting crashes by hour would mislead without traffic counts by hour (rush hour has more crashes because it has more cars), so darkness, known to make each mile riskier, stands in for time of day.
- **Alerts:** the National Weather Service's alerts where the trip starts (api.weather.gov: free, no key; cached 10 minutes; `ROUTES_WEATHER_ALERTS=off` turns them off) show above the recommendation. Winter ones (snow, ice, freezing rain) make turns across oncoming traffic count 1.5× as risky: stopping takes longer on ice.

## School zones and railroad crossings

Both come from OpenStreetMap with the road map, so they need a rebuild (`python -m app.build_graph milwaukee-metro --refresh`). A road map built before they were added still works, without them; `/health` shows how many it has.

- **Railroad crossings at grade** (`railway=level_crossing` on a road): each one costs 20 s (slowing down, and now and then a train) plus `rail_crossing_weight` (1 by default) in safety.
- **School zones:** streets within 200 m of a school (`amenity=school`), freeways excepted.
  - **When they count:** only during school hours, by default 7:00–9:00 and 14:00–16:30 on weekdays outside July and August, in the server's local time (`ROUTES_SCHOOL_HOURS`). Holidays and each district's calendar aren't known.
  - **Their cost:** the router drives them at 20 mph, entering one costs `school_zone_weight` (3, like a risky turn), and the scorer counts the schools a route passes.
- **Safety ranking:** both count toward safety, the first priority, alongside risky turns and lane closures. The web map lists them with each route and says when school zones are active.

## Results summary

```powershell
.\.venv\Scripts\python.exe -m app.evaluate
```

Runs 20 trips across Milwaukee through the same API the web map uses. It compares our recommended route with the standard route (Google's choice, or our fastest route on the OpenStreetMap map) and writes [RESULTS.md](RESULTS.md) at the repo root, `data/results.csv`, and `frontend/results.json`, the headline numbers the page's **How it works** shows. On the OpenStreetMap map it costs nothing from Google. On Google's map each new trip costs one cheap Google request, and reruns use saved answers; `--live-traffic` uses live times, at up to about 7 requests a trip.

## Own router (Milwaukee metro)

The engine can design its own routes on OpenStreetMap roads, and Google prices them with live traffic.

### Build the road map (once)

From `backend/`:

```powershell
python -m app.build_graph milwaukee-metro
```

This command:
- downloads drivable roads, traffic signals and turn restrictions for Milwaukee, Waukesha, Ozaukee and Washington counties from the public Overpass API, in 9 tiles saved under `data/osm/`
- writes `data/graphs/milwaukee-metro.json.gz`, which is where `ROUTES_GRAPH` points by default
- reuses tiles already on disk, so rebuilding after a code change doesn't download again
- splits a tile into four smaller queries (and those into four again) when Overpass keeps failing on it because it's overloaded or ran out of time; a rerun picks up from the saved pieces
- with `--endpoint https://overpass.kumi.systems/api/interpreter`, uses a mirror if the main server is busy

Routes built from this data must credit **"© OpenStreetMap contributors"** (ODbL) on the map.

### How a trip works

1. Google's own suggestions are fetched with live traffic (1 request).
2. Our router designs up to 3 routes for the profile. With `--all-profiles` it designs each profile's best route, skipping duplicates. The router works like this:
   - It snaps each end of the trip to the nearest junction a car can start from (not a freeway or a ramp). When rerouting a moving car (the API's `heading`), the trip instead starts on the road segment the car is driving along, freeways included.
   - The first route is the best design. Each alternative must share at most half its length with every earlier route (either way round) and with Google's suggestions, so it takes a different corridor rather than a block-long detour. Roads already used cost twice as much in the next search, and alternatives may cost up to twice the best design. The alternatives' searches use a 1.5x inflated A* estimate: much faster, at most 1.5x off the best possible alternative.
   - It searches road segments rather than junctions, so every turn has a cost. The cost of each segment and turn uses the same perceived-minutes weights as the scorer. Turns across oncoming traffic at busy or crash-prone junctions cost extra. At a junction where every road is one-way, no turn counts as crossing, because there's no oncoming traffic; the scorer applies the same rule.
   - It obeys turn restrictions, and only allows U-turns at dead ends, or through a median opening on a divided road, as a costed U-turn.
   - It doesn't cut through service roads (alleys, parking-lot connectors) to save a turn. Turning onto one mid-trip costs 10 perceived minutes. A trip that starts or ends on one isn't affected, and pass-through points are never placed on them.
3. Each design goes to Google as a live-traffic request that must pass through up to 25 points along it, each with the direction of travel:
   - one just after each turn
   - one about every 800 m on stretches of city street without turns, and every 3 km on freeways

   Google takes the quickest path between points, and can cut a corner through the very left turns the design avoided. When its version follows less than 99% of the design, the stretch it skipped gets a point and Google is asked once more. That costs one extra request, the better answer is kept, and the CLI notes it.
4. The scorer ranks everything on Google's numbers. Each of our routes shows `[NN%]`, the share of Google's version that follows our design.

### CLI flags

| Flag | Effect |
|---|---|
| none | Router mode, used whenever the graph file exists |
| `--no-pricing` | Our routes use the graph's own time estimates (marked `~`). Google's suggestions are fetched without traffic if possible. Without an API key they're skipped with a note, and the run continues with our routes only. Use this for free weight tuning. |
| `--no-router` | Google's routes only, the original behavior |

### Limits

- **Time estimates without pricing:** these come from posted speeds (or typical values by road type), a factor for junctions, and 12 s per traffic signal. Live times only come from pricing.
- **Some turn restrictions are skipped.** A restriction that goes through a stretch of road rather than a single junction can't be modeled. These are mostly U-turn bans across medians, and the build summary counts them. The router still only U-turns at dead ends.
- **The start of a planned trip can't be pinned.** Without a heading, Google assumes which way the car faces at the origin, which can add a U-turn at the start of every route, Google's own included. It's the same for all candidates, so it doesn't change the ranking. Reroutes while driving send the car's heading, which removes it.
- **Trips beyond the map:** a trip starting or ending more than 1 km from the map falls back to Google's routes only, with a note.
