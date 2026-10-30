# Results: our safer routes vs the standard route

Generated 2026-10-30 13:43 by `python -m app.evaluate` (20 Milwaukee trips, profile safe-economy, live traffic off).

## Headline

- **19 of 20 trips** got a different route than the standard one.
- **67 risky turns avoided** in all (3.4 per trip): 67 on the standard routes, 0 on ours.
- **Closed roads driven through:** 3 standard, 0 ours. **Lane closures:** 8 vs 0.
- **School zones:** 0 vs 0. **Rail crossings:** 64 vs 19. (School zones count only at drop-off and pickup times.)
- **Fuel:** ours use 1.54 gal more in all (10.99 gal standard, 12.53 ours).
- **Time:** +6.5 min per trip on average: the price of the safer choice.

## Trip by trip

Standard → ours. A risky turn crosses oncoming traffic at a busy or crash-prone junction.

| Trip | Recommended | Risky turns | Closed roads | Lane closures | Fuel (gal) | Minutes |
|---|---|---|---|---|---|---|
| Downtown → Mitchell Airport | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.38 → 0.53 | 17 → 24 |
| Downtown → Waukesha | Our safer design | 6 → 0 | 0 → 0 | 0 → 0 | 0.87 → 0.93 | 39 → 46 |
| Downtown → Mayfair Mall | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.58 → 0.56 | 27 → 30 |
| Third Ward → UW-Milwaukee | Our safer design | 5 → 0 | 0 → 0 | 0 → 0 | 0.28 → 0.27 | 15 → 17 |
| Bay View → Wauwatosa Village | Our safer design | 3 → 0 | 1 → 0 | 0 → 0 | 0.39 → 0.62 | 22 → 32 |
| Fiserv Forum → American Family Field | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.31 → 0.28 | 14 → 15 |
| Marquette University → Milwaukee County Zoo | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.34 → 0.40 | 18 → 22 |
| UW-Milwaukee → Southridge Mall | Our safer design | 2 → 0 | 1 → 0 | 0 → 0 | 0.81 → 0.91 | 40 → 50 |
| Bayshore → Downtown | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.40 → 0.40 | 18 → 26 |
| Menomonee Falls → Downtown | Our safer design | 3 → 0 | 0 → 0 | 2 → 0 | 0.83 → 0.84 | 34 → 38 |
| Oak Creek → Fiserv Forum | Our safer design | 3 → 0 | 1 → 0 | 0 → 0 | 0.62 → 0.75 | 26 → 38 |
| Brookfield Square → Third Ward | Our safer design | 6 → 0 | 0 → 0 | 1 → 0 | 0.56 → 0.67 | 23 → 29 |
| Mitchell Airport → Brookfield Square | Standard route | 0 → 0 | 0 → 0 | 0 → 0 | 0.58 → 0.58 | 25 → 25 |
| State Fair Park → Bayshore | Our safer design | 3 → 0 | 0 → 0 | 1 → 0 | 0.49 → 0.66 | 21 → 32 |
| Southridge Mall → Marquette University | Our safer design | 4 → 0 | 0 → 0 | 1 → 0 | 0.50 → 0.64 | 19 → 29 |
| Wauwatosa Village → Bay View | Our safer design | 1 → 0 | 0 → 0 | 1 → 0 | 0.40 → 0.58 | 23 → 33 |
| Mayfair Mall → Oak Creek | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.83 → 1.00 | 35 → 45 |
| American Family Field → UW-Milwaukee | Our safer design | 4 → 0 | 0 → 0 | 1 → 0 | 0.44 → 0.51 | 21 → 27 |
| Waukesha → Mitchell Airport | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.89 → 0.87 | 36 → 38 |
| Milwaukee County Zoo → Menomonee Falls | Our safer design | 1 → 0 | 0 → 0 | 1 → 0 | 0.50 → 0.52 | 19 → 23 |

## How to read this

- Safety comes first in the ranking, then fuel, then time, so extra minutes are expected where a safer route exists.
- With the OpenStreetMap map, the standard route is our router's fastest, so both are timed by the same model, with live delays from 511 and TomTom. With Google's map and no live traffic, the standard route's time is Google's no-traffic estimate: close, but not the same model. Run with `--live-traffic` for live times on both.
- Lane closures come from the live work-zone feed, so they change from day to day; school zones count only during school drop-off and pickup times.
