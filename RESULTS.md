# Results: our safer routes vs the standard route

Generated 2026-10-30 17:23 by `python -m app.evaluate` (20 Milwaukee trips, profile safe-economy, live traffic off).

## Headline

- **17 of 20 trips** got a different route than the standard one.
- **Safety score** (0-100, higher is safer): 41 on average for the standard routes, **70** for ours.
- **42 risky turns avoided** in all (2.1 per trip): 51 on the standard routes, 9 on ours.
- **Closed roads driven through:** 0 standard, 0 ours. **Lane closures:** 14 vs 4.
- **School zones:** 0 vs 0. **Rail crossings:** 48 vs 19. (School zones count only at drop-off and pickup times.)
- **Fuel:** ours use 1.99 gal more in all (10.80 gal standard, 12.79 ours).
- **Time:** +6.3 min per trip on average: the price of the safer choice.

## Trip by trip

Standard → ours. A risky turn crosses oncoming traffic at a busy or crash-prone junction.

| Trip | Recommended | Risky turns | Closed roads | Lane closures | Fuel (gal) | Minutes |
|---|---|---|---|---|---|---|
| Downtown → Mitchell Airport | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.38 → 0.48 | 17 → 23 |
| Downtown → Waukesha | Our safer design | 3 → 0 | 0 → 0 | 2 → 0 | 0.80 → 1.04 | 34 → 48 |
| Downtown → Mayfair Mall | Our safer design | 1 → 0 | 0 → 0 | 2 → 1 | 0.51 → 0.51 | 22 → 28 |
| Third Ward → UW-Milwaukee | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.26 → 0.33 | 14 → 19 |
| Bay View → Wauwatosa Village | Our safer design | 2 → 0 | 0 → 0 | 1 → 1 | 0.41 → 0.46 | 20 → 23 |
| Fiserv Forum → American Family Field | Our safer design | 2 → 0 | 0 → 0 | 1 → 0 | 0.31 → 0.29 | 11 → 15 |
| Marquette University → Milwaukee County Zoo | Our safer design | 1 → 1 | 0 → 0 | 2 → 0 | 0.37 → 0.63 | 16 → 25 |
| UW-Milwaukee → Southridge Mall | Our safer design | 1 → 0 | 0 → 0 | 1 → 0 | 0.72 → 0.89 | 33 → 39 |
| Bayshore → Downtown | Standard route | 4 → 4 | 0 → 0 | 0 → 0 | 0.38 → 0.38 | 14 → 14 |
| Menomonee Falls → Downtown | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.83 → 0.92 | 36 → 45 |
| Oak Creek → Fiserv Forum | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.62 → 0.65 | 26 → 28 |
| Brookfield Square → Third Ward | Our safer design | 4 → 0 | 0 → 0 | 1 → 0 | 0.54 → 0.72 | 23 → 35 |
| Mitchell Airport → Brookfield Square | Standard route | 0 → 0 | 0 → 0 | 0 → 0 | 0.58 → 0.58 | 24 → 24 |
| State Fair Park → Bayshore | Our safer design | 2 → 0 | 0 → 0 | 1 → 0 | 0.52 → 0.66 | 24 → 35 |
| Southridge Mall → Marquette University | Standard route | 4 → 4 | 0 → 0 | 1 → 1 | 0.50 → 0.50 | 19 → 19 |
| Wauwatosa Village → Bay View | Our safer design | 1 → 0 | 0 → 0 | 1 → 0 | 0.40 → 0.60 | 21 → 32 |
| Mayfair Mall → Oak Creek | Our safer design | 4 → 0 | 0 → 0 | 0 → 1 | 0.84 → 1.23 | 37 → 53 |
| American Family Field → UW-Milwaukee | Our safer design | 4 → 0 | 0 → 0 | 1 → 0 | 0.44 → 0.51 | 21 → 27 |
| Waukesha → Mitchell Airport | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.90 → 0.88 | 39 → 40 |
| Milwaukee County Zoo → Menomonee Falls | Our safer design | 1 → 0 | 0 → 0 | 0 → 0 | 0.50 → 0.52 | 18 → 23 |

## How to read this

- Safety comes first in the ranking, then fuel, then time, so extra minutes are expected where a safer route exists.
- With the OpenStreetMap map, the standard route is our router's fastest, so both are timed by the same model, with live delays from 511 and TomTom. With Google's map and no live traffic, the standard route's time is Google's no-traffic estimate: close, but not the same model. Run with `--live-traffic` for live times on both.
- Lane closures come from the live work-zone feed, so they change from day to day; school zones count only during school drop-off and pickup times.
