# Ideas to build next

The first eleven ideas on this list are built. Each line says where it lives, for changing it later. New ideas go at the bottom.

## Built

1. **A new driver profile:** Motorcycle. `PRESETS` in `backend/app/profiles.py`, its label in `frontend/core.js`.
2. **Home and Work buttons.** `showSavedPlaces` in `frontend/trips.js`.
3. **Search results from inside the map only.** `photon()` and `nominatim()` in `frontend/maps.js`.
4. **Another city:** Madison and Green Bay. `AREAS` and `CENTERS` in `backend/app/overpass.py`; README, "Another city".
5. **Avoid this road.** `avoided_roads` in `backend/app/main.py`, `avoidOnMap` in `frontend/editing.js`.
6. **Arrive by a time**, for car trips. `plan_at_its_time` in `backend/app/main.py`, `minutesUntil` in `frontend/app.js`.
7. **Buses beyond Milwaukee County:** Ryde Racine. `AllBusRoutes` in `backend/app/transit.py`, `Timetable.from_gtfs` in `backend/app/timetable.py`. Waukesha's published feed ran out in June 2025 and Kenosha publishes none.
8. **Road events kept fresh in the background.** `Engine.keep_events_fresh` in `backend/app/main.py`.
9. **Hills for bike routes.** `backend/app/elevation.py`, the `flat` rider in `backend/app/bike.py`.
10. **Rain and snow in the forecast.** `WeatherForecast` in `backend/app/weather.py`.
11. **Maps that load faster at startup.** `RoadGraph.load` and its fast copy in `backend/app/roadgraph.py`.

## Next

### 12. Arrive by, for bike, walking and bus trips
- **What:** "be there by 8:30" in the other modes. Today only car trips take it.
- **Start in:** `active_route` in `backend/app/main.py`, the way `plan_at_its_time` does it for cars. Bus trips already take a time to leave at (`leave_at`); arriving by one means searching the timetable backward.

### 13. Avoid a whole road, not one stretch
- **What:** one click rules out a road for its whole length ("no I-94").
- **Start in:** `avoided_roads` in `backend/app/main.py`. Today the spot closes the stretch between two junctions. The map knows each segment's road name (`RoadGraph.name`), so the closure can cover every segment of that name.
- **Watch out for:** a trip that starts or ends on the road still has to be able to use it.

### 14. Hills for walking trips
- **What:** a walk's climb, and a step-free walker who also avoids steep streets.
- **Start in:** `_build` in `backend/app/build_graph.py`, which adds the climb to the bike map only. The walking map is three times the size, so check the build time and the map file's size.

### 15. Kenosha and Waukesha buses
- **What:** transit trips in the two counties with no current public feed.
- **Start with:** asking the agencies. `ROUTES_BUS_ROUTES` already takes any number of GTFS feeds.

## Yours

Copy this and fill it in:

```markdown
### N. Name
- **What:** what a visitor sees or can do.
- **Start in:** the file and function to open first.
- **Then:** what else has to change (the page, the Spanish, the tests).
```
