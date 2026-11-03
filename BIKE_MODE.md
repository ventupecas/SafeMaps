# Plan: bike mode

## Status

Built: phases 1 to 3, and of phase 4 the cyclist crashes (fatal ones from FARS, and a `--cyclists` option for Community Maps exports) and the evaluation. Its first run, on streets without trails, is in [BIKE_RESULTS.md](BIKE_RESULTS.md): over 20 trips, the safety score went from 55 to 92 on average, minutes on busy streets from 147 to 32, and busy crossings without a signal from 22 to 0, for 2.7 minutes more on average. The decisions below were made as recommended:

1. **Default rider:** most adults ("interested but concerned"), with Family, Confident and E-bike to choose from.
2. **Sidewalks:** not used, until Milwaukee's ordinance is checked.
3. **E-bikes:** the same routes, at a faster speed (the E-bike rider).
4. **Winter:** a note from December to March that trails may not be cleared.
5. **Google's bike directions:** not used; the shortest route is always our own.
6. **Timeline:** phase by phase; the Bike stress layer (phase 1) can be shown on its own.

One change from the plan: the bike map is a separate map, not bike columns on the car map. Adding columns would have touched every car code path that walks the map (snapping, junction kinds, closures, road names), with trails turning up in car routes. The separate bike map costs about 240 MB per server process, and only when it's built.

Still to do:

- **The trails download:** `python -m app.build_graph milwaukee-metro --bike` needs Overpass, which wasn't reachable when the bike map was first built; until it's rerun, the bike map has streets but no trails.
- **The ride check**, then tuning the stress weights if routes feel wrong: rerun `python -m app.bike_evaluate` after any change.
- **Bike crashes from Wisconsin's Community Maps:** export by hand, then `python -m app.crash_data import <file> --cyclists`.
- **Hills:** they need elevation data, another download. (Stops on bike trips and lighting after dark are built.)

## The plan

A plan for safe bicycle routes. It covers what "safe" should mean for someone on a bike, the data it needs, how it fits the code that plans car trips today, how to check it works, and what to decide first.

## The goal

Someone picks **Bike** instead of **Car**, and gets the route they'd be most comfortable riding: trails and quiet streets first, with as few stressful stretches and dangerous crossings as possible, at a detour they choose. It's explained as the car routes are: a safety score, what it avoids, and the standard (shortest) route to compare with.

What it is not: a fastest-bike-route planner. The shortest route is only the comparison, as the fastest route is for cars.

## What makes a bike route safe

Car safety here counts discrete risks (turns across traffic, closures, school zones). For bikes, the main risk is **every stretch shared with fast or heavy traffic**, so the plan uses the established measure for that: **Level of Traffic Stress (LTS)**, from Mekuria, Furth and Nixon's 2012 report for the Mineta Transportation Institute, used by many US cities' bike plans.

| LTS | Who rides it | Typical street |
| --- | --- | --- |
| 1 | Children; anyone | Trails and paths, protected bike lanes, very quiet streets |
| 2 | Most adults ("interested but concerned") | Residential streets, bike lanes on slower streets |
| 3 | Confident riders | Bike lanes on busier streets; mixed traffic at moderate speeds |
| 4 | Only the most confident | Fast, multi-lane arterials without separation |

Each road segment gets an LTS from its tags (speed limit, number of lanes, the kind of bike facility, parking beside a bike lane). Each **crossing** gets one too: crossing a four-lane arterial at an unsignalized junction is LTS 4 even when both side streets are LTS 1. Crossings are where the car mode's "turn across traffic" idea carries over.

A route's risks, for its score:

- **Minutes at each LTS**: the main measure. Minutes at LTS 3 and 4 cost the most.
- **Stressful crossings**: unsignalized crossings of LTS 3 and 4 roads, and left turns across them. A signal halves it, as for cars today.
- **Crash-prone spots for bikes**: bike crashes near the route (data below).
- **Railroad tracks**: crossing at a shallow angle catches wheels; the road map already knows where tracks cross (`rail_crossing_points`).
- **Closures and lane closures**: from the same feeds as cars; a lane closure squeezes a bike into traffic.
- **After dark, on unlit stretches**: OSM's `lit=no`, where known.

The safety score keeps its 0 to 100 shape (100 with no risks, halved every 10 points), with bike weights. Bike and car scores aren't compared with each other; the page never puts them side by side.

## Data

### Roads and paths: OpenStreetMap

The download (`app/overpass.py`) asks for drivable roads only (`DRIVABLE_HIGHWAYS`). Bike mode needs more:

- **Ways bikes can use that cars can't**: `highway=cycleway`; `highway=path` and `footway` where `bicycle=yes` or `designated`; `highway=track` where paved. The Oak Leaf Trail, the Hank Aaron State Trail and the Kinnickinnic River Trail come in this way.
- **Bike tags on roads**: `cycleway`, `cycleway:left/right/both` (`lane`, `track`, `shared_lane`, `buffered_lane`, `separate`), `bicycle=no` (freeways and some arterials), `oneway:bicycle=no` (contraflow), `maxspeed`, `lanes`, `parking:*`, `surface`, `lit`.
- **Crossings**: `crossing=traffic_signals / marked / uncontrolled`, beside the traffic signals the map already has.

Before building anything, **check OSM's coverage in Milwaukee**: how many arterials have `maxspeed` and `lanes`, and whether the trails and bike lanes on the city's bike map are in OSM. Where tags are missing, LTS falls back to the road's class (as the car map falls back to class speeds today). Gaps found can be fixed in OSM itself, which helps everyone.

### Bike crashes

- **NHTSA FARS** (downloaded today for cars) has a person file: the fatal crashes where a cyclist died can be picked out. Few, so each one counts strongly, as for cars.
- **Wisconsin's Community Maps** (UW-Madison TOPS Lab) has every police-reported crash, including bike crashes, but only by manual export (its terms forbid automated downloads). The existing importer (`python -m app.crash_data import`) would gain a "bike crashes only" filter.

## How it fits the code

### One road map with bike columns, not a second map

The car map takes about 1.2 GB per server process. A second map for bikes would double that, and the load test already showed memory limits workers. Instead, **the one map gains bike columns**:

- **Per segment**: whether bikes may use it, and whether cars may; its bike facility class; its LTS; surface; lit.
- **Bike-only segments** (trails, paths) are added, marked not drivable.
- **The car router** skips segments cars can't use. One check in the search, and car routes come out as they do now. The car router's tests guard this.
- Built by the same `python -m app.build_graph`, from one bigger download. The map file grows, probably by a third (trails and paths are fewer than streets); measure it in phase 1.

### The router

`app/router.py`'s search is already general: segment costs plus turn costs, with a profile's weights. Bike mode adds a **travel mode** to the search:

- **Segment cost**: riding time at about 16 km/h (slower on unpaved surfaces), multiplied by a **stress weight** for its LTS. For example ×1 at LTS 1, ×1.4 at LTS 2, ×3 at LTS 3, ×8 at LTS 4. A "family" profile forbids LTS 4 outright, and LTS 3 too when there's any way around.
- **Turn costs**: crossing or turning left across an LTS 3 or 4 road costs, less at a signal.
- **Rules**: bike oneway and contraflow tags. Most car turn bans don't apply to bikes; OSM marks the exceptions with `except=bicycle`.
- **Detour limit**: as for cars, a profile's most extra time over the shortest route (default: half again as long, at most 15 minutes more).

The weights are judgment calls, like the car weights, and should be tuned with the evaluation below.

### Scoring

`app/features.py` and `app/scoring.py` gain bike features: minutes per LTS, stressful crossings, bike crash spots near the route, railroad crossings, closures, unlit minutes after dark. They gain a bike safety score with its own reference weights, and reasons in the same style ("lowest stress: 2 min on busy streets instead of 9").

### The page

- **A Car / Bike switch** above the trip (and `?mode=bike` in links, so phones open it).
- **The route drawn by stress**: each stretch colored by its LTS (green, yellow, orange, red), the recommended one bold, plus a map key for it. The **Biking** layer turns on with bike mode.
- **The breakdown table** shows minutes per stress level beside the standard (shortest) route's.
- **Turn-by-turn and spoken directions** work as for cars, at bike speeds, with "Cross at the signal" where a route uses a signalized crossing.
- **Spanish**: the new phrases go into `frontend/i18n.js` like the rest ("ciclovía", "carril para bicicletas", "nivel de estrés").
- **Google's map**: Google's Routes API has bicycling directions for the standard route. Using it costs paid requests, as for cars. On the OpenStreetMap map, our shortest route stands in, as it does for cars.

## Checking it works

- **Unit tests** on the synthetic test city (`tests/osm_factory.py`), with a trail and a bike lane added: LTS from tags; car routes never use a trail; bike routes take the trail over a parallel arterial; the family profile never uses LTS 4.
- **An evaluation** like `python -m app.evaluate`: 20 Milwaukee bike trips, our route against the shortest, comparing minutes at LTS 3 and 4, stressful crossings, crash spots and extra minutes. It's written to RESULTS.md, as for cars.
- **A ride check**: a local rider looks over 10 recommended routes on the map, and ideally rides a few. LTS from tags can be wrong where OSM is incomplete, and this catches it.

## Phases

1. **Data and the map.** The bigger download, bike columns, LTS for segments and crossings, the OSM coverage check, and memory measured. On its own it gives an "LTS layer" to look at, before any routing.
2. **Router and scoring.** Travel mode, stress-weighted costs, bike features and score, unit tests, the evaluation.
3. **The page.** The switch, stress-colored routes, breakdown, directions, Spanish.
4. **Bike crashes and tuning.** The FARS cyclist filter, the Community Maps bike import, weights tuned against the evaluation and the ride check.

Each phase ends with something that can be checked; phase 1's LTS map is also a good thing to show at a booth on its own.

## Decisions to make first

1. **The default rider.** "Interested but concerned" (LTS 2: what most people are) or "family" (LTS 1 only)? This sets how far routes detour.
2. **Sidewalks.** Can routes use sidewalks where riding is allowed, and where is it? Check Milwaukee's ordinance before routing on them; until then, sidewalks aren't used.
3. **E-bikes and scooters.** Same routes, faster? They change the speed, not the stress.
4. **Winter.** Trails may not be plowed. Ignore it, add a "winter" switch that avoids unplowed trails, or leave bike mode out of winter expos?
5. **Google's bike directions** for the standard route on Google's map: worth the paid requests, or always use our shortest route?
6. **The expo timeline.** Phases 1 and 2 are the bulk of the work. If the expo is soon, show phase 1's stress map there and ship routing after.

## Risks

- **OSM gaps**: missing speed limits or bike lanes make LTS wrong, and a stressful street would look calm. Mitigated by the coverage check, class fallbacks that assume the worse level, and the ride check.
- **Memory**: the bigger map in every worker. Measured in phase 1. Bike columns are small; trails add segments.
- **Trust**: calling a route "safe" for a cyclist is a stronger claim than for a driver. The page should say "lowest stress we could find", show the stressful stretches that remain, and never hide them.
