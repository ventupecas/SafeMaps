# Results: our safer routes vs the standard route

Generated 2026-10-30 10:06 by `python -m app.evaluate` (20 Milwaukee trips, profile safe-economy, live traffic off).

## Headline

- **19 of 20 trips** got a different route than the standard one.
- **39 risky turns avoided** in all (1.9 per trip): 43 on the standard routes, 4 on ours.
- **Closed roads driven through:** 1 standard, 0 ours. **Lane closures:** 11 vs 4.
- **School zones:** 0 vs 0. **Rail crossings:** 20 vs 15. (School zones count only at drop-off and pickup times.)
- **Fuel:** ours use 1.11 gal more in all (10.41 gal standard, 11.52 ours).
- **Time:** +6.2 min per trip on average: the price of the safer choice.

## Trip by trip

Standard → ours. A risky turn crosses oncoming traffic at a busy or crash-prone junction.

| Trip | Recommended | Risky turns | Closed roads | Lane closures | Fuel (gal) | Minutes |
|---|---|---|---|---|---|---|
| Downtown → Mitchell Airport | Our safer design | 3 → 1 | 0 → 0 | 0 → 1 | 0.38 → 0.40 | 16 → 18 |
| Downtown → Waukesha | Our safer design | 1 → 0 | 0 → 0 | 2 → 0 | 0.75 → 0.87 | 28 → 41 |
| Downtown → Mayfair Mall | Our safer design | 0 → 0 | 0 → 0 | 1 → 0 | 0.46 → 0.54 | 17 → 25 |
| Third Ward → UW-Milwaukee | Our safer design | 2 → 0 | 0 → 0 | 0 → 0 | 0.29 → 0.27 | 14 → 16 |
| Bay View → Wauwatosa Village | Our safer design | 2 → 0 | 0 → 0 | 0 → 0 | 0.37 → 0.43 | 16 → 21 |
| Fiserv Forum → American Family Field | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.27 → 0.29 | 10 → 11 |
| Marquette University → Milwaukee County Zoo | Our safer design | 1 → 0 | 0 → 0 | 1 → 0 | 0.34 → 0.38 | 14 → 18 |
| UW-Milwaukee → Southridge Mall | Our safer design | 3 → 0 | 0 → 0 | 1 → 1 | 0.69 → 0.73 | 28 → 31 |
| Bayshore → Downtown | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.38 → 0.47 | 16 → 23 |
| Menomonee Falls → Downtown | Our safer design | 1 → 0 | 0 → 0 | 2 → 0 | 0.74 → 0.84 | 26 → 38 |
| Oak Creek → Fiserv Forum | Our safer design | 3 → 0 | 0 → 0 | 0 → 0 | 0.57 → 0.65 | 22 → 28 |
| Brookfield Square → Third Ward | Our safer design | 2 → 0 | 1 → 0 | 1 → 0 | 0.45 → 0.72 | 17 → 35 |
| Mitchell Airport → Brookfield Square | Our safer design | 0 → 0 | 0 → 0 | 0 → 0 | 0.64 → 0.58 | 20 → 24 |
| State Fair Park → Bayshore | Our safer design | 1 → 0 | 0 → 0 | 1 → 1 | 0.48 → 0.53 | 19 → 23 |
| Southridge Mall → Marquette University | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.50 → 0.57 | 19 → 22 |
| Wauwatosa Village → Bay View | Standard route | 3 → 3 | 0 → 0 | 1 → 1 | 0.38 → 0.38 | 16 → 16 |
| Mayfair Mall → Oak Creek | Our safer design | 4 → 0 | 0 → 0 | 0 → 0 | 0.86 → 1.00 | 31 → 45 |
| American Family Field → UW-Milwaukee | Our safer design | 2 → 0 | 0 → 0 | 1 → 0 | 0.40 → 0.51 | 18 → 27 |
| Waukesha → Mitchell Airport | Our safer design | 2 → 0 | 0 → 0 | 0 → 0 | 0.93 → 0.87 | 31 → 38 |
| Milwaukee County Zoo → Menomonee Falls | Our safer design | 1 → 0 | 0 → 0 | 0 → 0 | 0.54 → 0.50 | 19 → 20 |

## How to read this

- Safety comes first in the ranking, then fuel, then time, so extra minutes are expected where a safer route exists.
- Without live traffic, our routes' times are our road map's estimates and the standard route's are Google's no-traffic estimates: close, but not the same model. Run with `--live-traffic` for live times on both.
- Lane closures come from the live work-zone feed, so they change from day to day; school zones count only during school drop-off and pickup times.
