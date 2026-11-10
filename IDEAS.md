# Ideas to build next

Features that fit how the app already works, smallest first. Each one says what it adds and where in the code to start. Add your own at the bottom.

## An afternoon each

### 1. A new driver profile
- **What:** another choice under **Driver profile**, for example "Motorcycle" (wary of railroad crossings and lane closures) or "Night driver" (wary of unlit turns).
- **Start in:** `PRESETS` in `backend/app/profiles.py`. A profile is one `ScoringProfile` with its weights.
- **Then:** its label in `PROFILE_LABELS` (`frontend/core.js`) and its Spanish in `PHRASES` (`frontend/i18n.js`).
- **Check it with:** `python -m app.cli --from LAT,LNG --to LAT,LNG --all-profiles` from `backend/`, which shows the route every profile picks for the same trip.

### 2. Home and Work buttons
- **What:** two saved places a visitor sets once and picks with one click.
- **Start in:** `frontend/trips.js`. Recent trips are already kept in the browser's `localStorage`; saved places work the same way.
- **Then:** two buttons beside **Use my location** in `frontend/index.html`, calling `setPoint` (`frontend/editing.js`).

### 3. Search results from inside the map only
- **What:** the search box stops suggesting places the map doesn't cover.
- **Start in:** `photon()` in `frontend/maps.js`. Photon takes a `bbox=west,south,east,north` parameter; the map's area is in `state.area`.
- **Why:** today a place outside the dashed line can be picked from search, and the trip is refused only when it's planned.

### 4. Another city
- **What:** the same planner for Madison, Green Bay or Chicago.
- **Start in:** `AREAS` in `backend/app/overpass.py`: add a name and a box, then build its maps as README's "Build the road map" says and point `ROUTES_GRAPH` at them.
- **Then:** the map's starting center (`MILWAUKEE` in `frontend/core.js`) and the search's home (`SEARCH_BIAS` in `frontend/maps.js`) should come from the server's `GET /app-config` instead of being fixed.

## A weekend each

### 5. "Avoid this road"
- **What:** click a road on the map to keep routes off it.
- **Start in:** `prepare()` in `backend/app/main.py`. The router already steers around closures, and a closure is a `RoadEvent` (`backend/app/road_events.py`). A road the visitor rules out is one more closed `RoadEvent`, with `source="visitor"`.
- **Then:** a list of lines in `TripRequest`, and a click handler on the page that sends them.

### 6. Arrive by a time
- **What:** "be there by 8:30" instead of "leave in 30 minutes".
- **Start in:** `POST /leave-times` in `backend/app/main.py`, which already judges the trip at several departure times. Pick the latest departure whose arrival is in time.
- **Then:** a second choice beside **Leave** on the page (`frontend/app.js`, `tripPayload`).

### 7. Buses for Racine, Kenosha and Waukesha
- **What:** transit trips and the Transit layer for the counties the map now reaches. Today only Milwaukee County's buses are read.
- **Start in:** `ROUTES_BUS_ROUTES` in `backend/app/config.py`, which takes one GTFS feed. Let it take several, and merge them in `BusRoutes` (`backend/app/transit.py`) and `TimetableSource` (`backend/app/timetable.py`).

### 8. Road events kept fresh in the background
- **What:** no trip ever waits for a feed download. Today the first trip after five quiet minutes fetches the feeds itself, which adds about a second.
- **Start in:** `Engine.warm_up` in `backend/app/main.py`, which already fetches and matches them once at startup.
- **Watch out for:** TomTom's daily request limit (`ROUTES_TOMTOM_DAILY_REQUESTS`). Refresh only while trips are being planned, not all night.

## Bigger

### 9. Hills for bike routes
- **What:** each bike route's climb in feet, and a rider profile that avoids steep streets.
- **Start in:** the bike map's build (`build_graph(mode="bike")` in `backend/app/roadgraph.py`): give each segment its rise from USGS elevation data. Then cost it in `backend/app/bike.py`.

### 10. Rain and snow in the forecast, not only alerts
- **What:** a trip leaving in an hour is judged by that hour's forecast. Today only National Weather Service alerts count.
- **Start in:** `backend/app/weather.py`. The same service has an hourly forecast for a point; `for_the_weather` already adjusts a profile for slippery roads.

### 11. Maps that load faster at startup
- **What:** the server answers sooner after a restart. Loading the three maps takes about 11 seconds (car 2.5, bike 2.3, walking 6.1), nearly all of it unzipping and reading JSON.
- **Start in:** `RoadGraph.save` and `RoadGraph.load` in `backend/app/roadgraph.py`. The columns are already numpy arrays in memory, so a binary file of them (`numpy.savez`) would load in a fraction of the time.

## Yours

Copy this and fill it in:

```markdown
### N. Name
- **What:** what a visitor sees or can do.
- **Start in:** the file and function to open first.
- **Then:** what else has to change (the page, the Spanish, the tests).
```
