# Personalized Routing Engine

Expo prototype. The backend fetches every route Google will offer between two
points, re-scores each one against a driver profile, and returns the winner's
polyline. The frontend just draws it.

**Status:** the backend engine is done: Routes client, cache, candidate generation, scoring, CLI, and the engine's own router on OpenStreetMap roads with Google live-traffic pricing.
The HTTP API (FastAPI) and the web map frontend come next.

```
backend/
  app/
    routes_client.py  Google Routes API (computeRoutes) over httpx, through the cache
    cache.py          File-backed response cache: record / replay / off
    candidates.py     Widens the candidate pool: alternatives, avoid-highways, via-point offsets
    features.py       Raw route JSON -> highway time, winding time, turns, crossing turns, traffic delay
    driving_side.py   Which countries keep left, so "turn across traffic" means the right thing
    hotspots.py       Crash-data index: recorded crashes near a junction
    scoring.py        Pure scoring and ranking; the part you tune
    profiles.py       Preset personas (fastest, highway-averse, scenic, calm, eco)
    overpass.py       Downloads OpenStreetMap roads for an area (Overpass API)
    roadgraph.py      Turns OSM data into a drivable road graph with turn restrictions
    build_graph.py    Command that downloads and builds the graph
    router.py         Our own router: personalized A* search with turn costs, plus alternatives
    pricing.py        Sends our routes to Google for live-traffic times; measures how closely Google followed
    engine.py         The pipeline the API and CLI call
    cli.py            Run and tune from the terminal
  data/               Downloaded OSM tiles and built graphs (gitignored)
  tests/              154 tests, no network needed
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

## Google Cloud setup for $0

1. Create a Cloud project and attach a billing account. Google requires one even when usage stays inside the free allowance.
2. Enable the **Routes API**. The legacy Directions API can't be enabled on new projects, and this backend doesn't use it.
3. Create an API key for the backend and restrict it to the Routes API. The frontend will get its own browser key, restricted by HTTP referrer.
4. Cap usage. In **APIs & Services → Routes API → Quotas**, set a daily request limit, and add a budget alert under **Billing → Budgets**. With a cap in place, a demo loop left running all day can't run up a bill.
5. Leave `ROUTES_TRAFFIC_AWARE=false`. Traffic-aware routing bills at the Pro SKU, which has a smaller free allowance. Every other request this backend sends stays on Essentials.
   See Google's [pricing page](https://developers.google.com/maps/billing-and-pricing/pricing) for the current free caps.

Without the router, each new trip costs 2 Essentials requests (plain + avoid-highways), plus up to 2 via-offset requests when Google offers fewer than 3 distinct routes. A repeat of the same trip costs nothing.

With the router (see below), each trip costs 1 live-traffic request for Google's own suggestions plus 1 per route our router designs: up to 4, all at the **Pro** tier. Live-traffic answers are only reused for 5 minutes in record mode, so set the daily quota cap before a demo.

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
3. **Score.** Every term is in *perceived minutes*:

   ```
   cost = time_weight * minutes
        + distance_weight * km
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
- **Busy junction.** Either road has a no-traffic speed of 50 km/h or more, meaning arterial class or faster. Google's free tier doesn't report traffic volume at each junction, so road class stands in for it.
- **Crash-prone junction.** At least `ROUTES_CRASH_MIN_COUNT` recorded crashes within 35 m. Point `ROUTES_CRASH_DATA` at a CSV with `lat,lng[,crashes]` columns. Many cities publish this data, for example NYC's "Motor Vehicle Collisions", Chicago's "Traffic Crashes" and the UK's STATS19. Filter it to your demo area first. Without crash data, only the busy rule applies.
- **Weights.** By default each busy crossing turn costs 0.5 perceived minutes, and a crash-prone one costs 2 more. The `calm` preset raises these to 1.5 and 4. `fastest` sets both to 0 so it stays a pure-time baseline.

`--turns` lists every crossing turn with its location, road speed and nearby crash count, which is the quickest way to calibrate:

```powershell
python -m app.cli --from 51.5007,-0.1246 --to 51.5194,-0.1270 --country GB --profile calm --turns
```

The engine only picks among the routes Google returns; it can't make Google build a route made of right turns. The more distinct candidates there are, the more often a safer one is available.

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
   - It snaps each end of the trip to the nearest junction a car can start from (not a freeway or a ramp).
   - It searches road segments rather than junctions, so every turn has a cost. The cost of each segment and turn uses the same perceived-minutes weights as the scorer. Turns across oncoming traffic at busy or crash-prone junctions cost extra. A turn between two one-way streets doesn't count as crossing, because there's no oncoming traffic.
   - It obeys turn restrictions, and only allows U-turns at dead ends.
3. Each design goes to Google as a live-traffic request that must pass through up to 25 points along it: one just after each turn, plus one every 3 km on long straights, each with the direction of travel. Google returns its version of the route with a live-traffic time.
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
- **Pass-through points and headings are untested against the live API.** Check the `[NN%]` match on your first real trips. If Google often wanders, add points or remove the headings in `pricing.py`.
- **Trips beyond the map:** a trip starting or ending more than 1 km from the map falls back to Google's routes only, with a note.
