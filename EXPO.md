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

## 2. In the week before the expo

### Rebuild the road map (recommended)

Downloads fresh OpenStreetMap data and rebuilds the router's map, picking up new ramps (for example from the I-94 East-West rebuild). Ramp closures on ramps that are missing from the map get skipped. It takes a while.

```powershell
.\.venv\Scripts\python.exe -m app.build_graph milwaukee-metro --refresh
```

- **Network:** some workplace networks block the download (Overpass). Use another network if it times out.
- **If the server is busy:** add `--endpoint https://overpass.kumi.systems/api/interpreter`.
- **Afterwards:** restart the server so it loads the new map.

### Run the tests

All tests should pass. They don't call Google or cost anything. Run them after any change, and once more before the expo.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

### Edit `backend\.env`

| Setting | Value | Why |
|---|---|---|
| `WI511_API_KEY` | the key 511WI emails you | Adds crashes and incidents to the work zones. See the README's "Getting the Wisconsin 511 key". |
| `ROUTES_DAILY_LIMIT` | 7 × the trips you expect, e.g. `700` for 100 trips | Each trip with Live traffic on costs about 4–7 Google requests. Google's free allowance for these is 5,000 a month. |
| `ROUTES_CACHE_MODE` | `record` (leave as is) | Live answers, saved as they come. |

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

### Check it's healthy

From a second PowerShell window:

```powershell
curl.exe http://127.0.0.1:3143/health
```

Look for:

- `road_graph` showing `milwaukee-metro`
- `google_api_key_set: true`
- `wi511_key_set: true` (once you have the 511 key)
- `google_requests_today` well under your daily limit

### Rehearse the showcase trips

Open **http://127.0.0.1:3143** and run the 2–3 trips you plan to show.

- **Pick places with the search boxes, not map clicks,** so the trips repeat exactly.
- **This also saves Google's answers** for those trips, which the zero-cost fallback below relies on.

## 4. During the expo

### Watch Google usage

Every hour or so, run the `/health` check above and look at `google_requests_today`.

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

- **Internet is needed no matter what.** The map, place search and work-zone feed all come from the internet. Saved-answers mode only removes the cost of Google's routing.
- **Laptop only.** Driving mode (following your location) works in the laptop's browser, not on visitors' phones, which would need an https address.

## 5. Optional extras

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
- **Replace any key that was ever shared** (pasted into a chat, an email or a screenshot): create a new one under **APIs & Services → Credentials** in the Cloud Console, put it in `backend\.env` or `frontend\config.js`, and delete the old one.
