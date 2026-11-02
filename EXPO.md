# Expo runbook

Every command needed to run the Safe Route Planner at the expo, what it does, and when to run it.

Run all commands in PowerShell from the backend folder. Start each session with:

```powershell
cd C:\Users\VenturaPerez\routes\routes\backend
```

## 1. One-time setup (only on a new computer)

Create the project's Python environment, install its libraries, and copy the settings template:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Then put the keys in place:

- **Server key** (Routes API): `GOOGLE_MAPS_API_KEY=` in `backend\.env`.
- **Browser key** (Maps JavaScript API and Places API (New), restricted to `http://localhost:3143/*` and `http://127.0.0.1:3143/*`): copy `frontend\config.example.js` to `frontend\config.js` and paste it in.

Both files are gitignored. The README's "Google Cloud setup for $0" and "Web map and API" sections have the full key setup.

Download crash data for the crash-prone junction rule (about 170 MB of public NHTSA files, once):

```powershell
.\.venv\Scripts\python.exe -m app.crash_data milwaukee-metro
```

Then add the two lines it prints (`ROUTES_CRASH_DATA=...` and `ROUTES_CRASH_MIN_COUNT=1`) to `backend\.env`. On this computer this is done.

## 2. In the week before the expo

### Rebuild the road map (needed for school zones and railroad crossings)

Downloads fresh OpenStreetMap data and rebuilds the router's map:

- **School zones and railroad crossings:** they only come with this rebuild; until then the app runs without them.
- **New ramps:** for example from the I-94 East-West rebuild. Ramp closures on ramps missing from the map get skipped.

It takes a while.

```powershell
.\.venv\Scripts\python.exe -m app.build_graph milwaukee-metro --refresh
```

- **Network:** some workplace networks block the download (Overpass). Use another network if it times out.
- **If the server is busy:** add `--endpoint https://overpass.kumi.systems/api/interpreter`.
- **Afterwards:** restart the server so it loads the new map. `curl.exe http://127.0.0.1:3143/health` should then show `rail_crossings` and `schools` above 0 under `hazards`.

### Optional: all reported crashes, not only fatal ones

The crash-prone junction rule uses fatal crashes (NHTSA). Wisconsin's Community Maps has every reported crash: export the Milwaukee-area crashes as CSV from <https://transportal.cee.wisc.edu/partners/community-maps/crash/> (by hand: its terms forbid automated downloading), then:

```powershell
.\.venv\Scripts\python.exe -m app.crash_data import C:\path\to\the-export.csv
```

It prints the two lines to put in `backend\.env` (`ROUTES_CRASH_DATA=...` and `ROUTES_CRASH_MIN_COUNT=...`). Restart the server and check `/health`: `crash_locations` under `hazards` shows how many spots count. If it can't tell which columns hold the coordinates, it stops and lists the columns the file has.

### Make the results summary

Runs 20 Milwaukee trips and writes `RESULTS.md` at the repo root: risky turns avoided, closures, fuel and minutes, compared with the standard route. It's a good thing to print for the table. It also writes `frontend\results.json`, the numbers the page's **How it works** shows visitors. With `ROUTES_MAP=osm` it costs nothing from Google; on Google's map each new trip costs one cheap Google request, and reruns use saved answers. Rerun it after the road map rebuild so school zones and rail crossings show up, and the day before the expo so the numbers reflect that week's closures.

```powershell
.\.venv\Scripts\python.exe -m app.evaluate
```

### Run the tests

All tests should pass. They don't call Google or cost anything. Run them after any change, and once more before the expo.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

### Edit `backend\.env`

| Setting | Value | Why |
|---|---|---|
| `WI511_API_KEY` | the key 511WI emails you | Adds crashes and incidents to the work zones. See the README's "Getting the Wisconsin 511 key". |
| `TOMTOM_API_KEY` | your key from developer.tomtom.com | Live traffic on every street (jams, closures, lane closures, crashes) and the traffic map under the **Traffic** map layer, on the OpenStreetMap map. Free plan; the server keeps each day under 80 requests and 6,000 map tiles (`ROUTES_TOMTOM_DAILY_REQUESTS`, `ROUTES_TOMTOM_DAILY_TILES`). |
| `ROUTES_FUEL_PRICE` | today's price of a gallon, e.g. `3.19` | Each route's fuel cost on the page. Default $3.25. |
| `ROUTES_DAILY_LIMIT` | 7 × the trips you expect, e.g. `700` for 100 trips | Each trip with Live traffic on costs about 4–7 Google requests. Google's free allowance for these is 5,000 a month. |
| `ROUTES_CACHE_MODE` | `record` (leave as is) | Live answers, saved as they come. |
| `ROUTES_MAP` | `google` or `osm` | `osm` shows an OpenStreetMap map with no Google logo and no Google calls at all, so no Google costs. Live traffic then comes from TomTom (every street) and Wisconsin 511, and the **Traffic** map layer shows TomTom's traffic map. `google` keeps Google's map with Google's live traffic, and TomTom isn't used. Restart the server after changing it. |

### Do a test trip from the command line

Ranks downtown → Waukesha using saved Google answers only, so it costs nothing. It should print a ranked table of routes.

```powershell
.\.venv\Scripts\python.exe -m app.cli --from 43.0389,-87.9065 --to 43.0117,-88.2315 --cache-mode replay
```

## 3. Morning of the expo

### Start the server

Leave this window open all day: closing it stops the app.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 3143
```

- **No `--reload`:** that option is for development. Without it, a stray file save can't restart the server mid-demo.
- **Keep `--port 3143`:** the map key only works on that port.
- **Wait for "Ready for trips":** the window prints `Started: ...` at once, then `Ready for trips: ... matched to the map in N s` about a minute later. Trips work before that, just slower.
- **Restarted within 10 minutes?** TomTom's last answer is reused, so a restart doesn't use another TomTom request.

### Check it's healthy

From a second PowerShell window:

```powershell
curl.exe http://127.0.0.1:3143/health
```

Look for:

- `road_graph` showing `milwaukee-metro`
- `google_api_key_set: true` (on Google's map)
- `wi511_key_set: true` (once you have the 511 key)
- `tomtom_key_set: true` (on the OpenStreetMap map)
- `matched_ahead: true`: the closures and jams across the metro are matched to the road map, so trips don't wait for it. It takes about a minute after the server starts; check again if it's still `false`.
- `google_requests_today` well under your daily limit, and `tomtom_requests_today` under 80

### Turn on demo mode on the booth screen

Open **http://127.0.0.1:3143/?demo=1** on the screen visitors see. After 45 seconds with no one touching it, the page plans sample trips by itself (Fiserv Forum to Bay View, and three more), one every 20 seconds, with a "Sample trip" banner. Any touch stops it, so visitors can plan their own; it starts again once they leave. The browser remembers the setting; turn it off under **Advanced**. Sample trips cost no paid requests.

### Rehearse the showcase trips

Open **http://127.0.0.1:3143** and run the 2–3 trips you plan to show.

- **Pick places with the search boxes, not map clicks,** so the trips repeat exactly.
- **Try "Extra time you'd accept for a safer route":** "Up to 10 minutes" stops a much slower route from winning on a long trip. Visitors can pick their own.
- **Good things to show:** the safety score ring; **Best time to leave?** near sunset (waiting until the sun is down often scores higher); a trip with a stop; the **New driver** profile.
- **This also saves Google's answers** for those trips, which the zero-cost fallback below relies on.

### Get the map ready for a Wi-Fi drop

On the OpenStreetMap map (`ROUTES_MAP=osm`), the booth screen keeps a copy of the page and of every map image it shows, so it keeps working if the venue's internet drops. The server runs on the laptop, so planning still works too. Before doors open, on the booth screen:

- Open the page once, then **reload it** (the copy starts with the second load).
- **Pan and zoom over the area you'll show**, in each map type you'll use (Default, Satellite, the dark map at night). Only images you've looked at are kept: the map services don't allow downloading areas ahead.

Without internet, place search, addresses for clicked spots, live traffic and new closures stop; clicking the map and the trips you rehearsed still work. Google's map isn't kept (its terms don't allow it), so it needs the internet.

## 4. During the expo

### Visitors' answers

Under each recommendation the page asks "Would you take this route?". See the count at any time at **http://127.0.0.1:3143/feedback-summary**: yes and no, and how much safer and longer the routes were for each. No places are kept.

### The stats screen

Open **http://127.0.0.1:3143/stats.html** on a second screen: today's trips, how many got a safer route than the standard one, the risky turns, railroad crossings, school zones and closed roads they avoided, and the share who'd take the safer route. It updates every 20 seconds. Opened from the tunnel's https link, it also shows a QR code for phones. Sample trips (demo mode) and automatic re-plans aren't counted, and no places are kept (`backend/.cache/routes/trips.jsonl`).

### Watch usage

Every hour or so, run the `/health` check above and look at `google_requests_today` (Google's map), or `tomtom_requests_today` and `tomtom_tiles_today` (the OpenStreetMap map). When TomTom's daily cap is reached, trips still work with work zones and 511, and the **Traffic** map layer stops showing new map squares until midnight.

### Fallback 1: Google limit used up, or Google not answering

Untick **Live traffic** on the page. Trips then use our router's own time estimates, Google's routing isn't called for them, and work zones still show.

### Fallback 2: guarantee zero cost

Stop the server (Ctrl+C), then restart it in saved-answers-only mode:

```powershell
$env:ROUTES_CACHE_MODE = "replay"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 3143
```

Any trip still works with Live traffic off, and the rehearsed trips also show the standard route.

To go back to live mode, stop the server, run this, then start it again:

```powershell
Remove-Item Env:ROUTES_CACHE_MODE
```

### Limits to know

- **Internet:** Google's map, place search, live traffic and the work-zone feed all need it. On the OpenStreetMap map the booth screen keeps working without it (see "Get the map ready for a Wi-Fi drop"). Saved-answers mode only removes the cost of Google's routing.
- **Fallback 3: no Google at all.** Set `ROUTES_MAP=osm` in `backend\.env` and restart the server. You get the OpenStreetMap map, live traffic from TomTom and Wisconsin 511 instead of Google, and no Google costs or Google key.
- **Phones need the tunnel.** Driving mode (following your location) works in the laptop's browser. Visitors' phones need the https link from "Let visitors use their phones" below.
- **Sound on.** Spoken directions come through the laptop's speakers; untick **Spoken directions while driving** for quiet.
- **School zones** only count at drop-off and pickup times (weekdays 7–9 and 2–4:30), so they may not show during the expo.

## 5. Optional extras

### Let visitors use their phones

A free Cloudflare tunnel gives the laptop a public https address, so phones can open the app and use driving mode. The access code stops strangers from spending your Google requests.

1. Install the tunnel tool (once):
   ```powershell
   winget install --id Cloudflare.cloudflared
   ```
2. Add an access code to `backend\.env`, for example `ROUTES_ACCESS_CODE=expo2026`, and restart the server.
3. Start the tunnel in a new PowerShell window. Leave it open; it prints an address like `https://something.trycloudflare.com`.
   ```powershell
   cloudflared tunnel --url http://localhost:3143
   ```
4. Add `https://*.trycloudflare.com/*` to the browser key's allowed websites in the Cloud Console, under **APIs & Services → Credentials**. It can take a few minutes to apply.
5. On the laptop, open `https://something.trycloudflare.com/?code=expo2026` (your address and code) and click **Open on your phone**. It shows a QR code of that address with the code, for visitors to scan. The page remembers the code, so they scan once.

Things to know:

- **New address each time:** the address changes every time the tunnel starts, so open the new one and show its QR code.
- **On `127.0.0.1`,** **Open on your phone** explains these steps instead: phones can't reach the laptop's own address.
- **Shared limit:** every visitor's trips count toward your daily Google limit.
- **When done:** press Ctrl+C in the tunnel window, then remove the `trycloudflare.com` entry from the key.

### Save a copy of the work zones

In case the 511WI feed goes down. Do it shortly before the expo: work zones that end after you save the copy drop out of it.

```powershell
curl.exe -o data\wzdx.json https://511wi.gov/api/wzdx
```

Then add `ROUTES_WZDX_FILE=data/wzdx.json` to `backend\.env` and restart the server. `backend\data\` is gitignored.

### Back up the project

Git ignores the keys (`.env`, `config.js`), so they won't be included.

```powershell
cd ..
git add -A
git commit -m "Expo version"
```

### Show the API

**http://127.0.0.1:3143/docs** lets you send a trip to the server by hand, which is handy for showing the technical side.

## 6. After the expo

- **Stop the server:** press Ctrl+C in its window.
- **Keep the visitors' answers:** copy `backend\.cache\routes\feedback.jsonl` somewhere safe (one answer a line), and save the `/feedback-summary` numbers for your write-up.
- **Replace any key that was ever shared** (pasted into a chat, an email or a screenshot): create a new one under **APIs & Services → Credentials** in the Cloud Console, put it in `backend\.env` or `frontend\config.js`, and delete the old one.
