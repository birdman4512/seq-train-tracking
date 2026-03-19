# 🚆 SEQ Live Train Tracker

Real-time train tracking for South East Queensland, powered by
**Translink GTFS-RT open data** (CC-BY, no API key required).

---

## Features

### 🗺️ Live Map
- All active SEQ trains updated every 15 seconds, animating smoothly between positions
- Colour-coded by line (official Translink colours)
- Bearing arrows showing direction of travel
- Click a station to see upcoming arrivals with countdown timers — click an arrival to jump to that train
- Click a train for full detail: headsign, delay, next stops, trip ID

### 🖥️ Control View (`/control.html`)
- Railway schematic diagram — Roma Street at centre, lines branching outward
- Trains shown as coloured rectangles on the correct line
- Click trains or stations for details and arrival predictions

### 📋 Sidebar
- Each train card shows destination in the line colour, next stop + minutes until arrival
- Sorted by delay (most late first)
- Search by route code, destination or label

### 🔧 Other
- Service alerts panel
- Log viewer at `/logs.html`
- Mobile-friendly (responsive sidebar on phones)
- Deployable at root or any subfolder (relative API paths)

---

## Quick Start

```bash
git clone <this-repo>
cd seq-train-tracker
mkdir -p logs
docker compose up --build
```

Open **http://localhost:8080**

> First build takes ~60s (pulls images, installs Python deps).
> Subsequent starts are fast. GTFS static data is downloaded on first run (~30s).

---

## Architecture

```
Browser  →  http://localhost:8080
             │
     ┌───────▼────────┐       ┌──────────────────┐
     │ nginx (frontend)│──────►│ Flask (backend)  │
     │  index.html     │ /api/ │  polls GTFS-RT   │
     │  control.html   │       │  every 15s       │
     │  logs.html      │       │  parses protobuf │
     └─────────────────┘       └────────┬─────────┘
                                        │ HTTPS
                               ┌────────▼─────────────────┐
                               │  Translink GTFS-RT feeds │
                               │  VehiclePositions/Rail   │
                               │  TripUpdates/Rail        │
                               │  alerts                  │
                               │  SEQ_GTFS.zip (static)   │
                               └──────────────────────────┘
```

**Frontend** — static HTML/CSS/JS served by nginx. All `/api/*` requests proxied to backend.

**Backend** — Python/Flask + gunicorn. Two background threads:
- GTFS-RT poller: fetches VehiclePositions + TripUpdates every 15s
- GTFS static loader: downloads schedule ZIP on startup, refreshes every 24h

**Logs** — written to `./logs/backend.log` (host-mounted folder).

---

## Data Sources

| Feed | URL |
|------|-----|
| Vehicle Positions | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail` |
| Trip Updates | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Rail` |
| Alerts | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/alerts` |
| GTFS Schedule | `https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip` |

Licensed under [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/) — data by [Translink Queensland](https://translink.com.au/about-translink/open-data).

---

## API Endpoints

All endpoints are at `/api/` and work relative to wherever the app is hosted.

| Endpoint | Description |
|----------|-------------|
| `GET /api/vehicles` | Live vehicles — position, speed, delay, next stops |
| `GET /api/shapes` | GTFS track shapes as GeoJSON FeatureCollection |
| `GET /api/stations` | Stations with upcoming arrivals (actual = scheduled + delay) |
| `GET /api/alerts` | Current service alerts |
| `GET /api/status` | Backend health, feed age, GTFS load status |
| `GET /api/logs` | Last 200 lines of backend log |
| `GET /api/debug/routes` | GTFS route IDs, names and colours |
| `GET /api/debug/live` | Live route IDs seen in realtime feed with colour assignments |

---

## Subfolder Deployment

To host at e.g. `https://example.com/trains/`:

1. Configure your reverse proxy to forward `/trains/` to port 8080
2. No code changes needed — all API paths are derived from `window.location.pathname` automatically

---

## Configuration

| Setting | File | Default |
|---------|------|---------|
| Poll interval | `backend/app.py` → `POLL_INTERVAL` | `15` seconds |
| GTFS refresh | `backend/app.py` → `SHAPES_REFRESH_HOURS` | `24` hours |
| Exposed port | `docker-compose.yml` | `8080` |
| Log folder | `docker-compose.yml` | `./logs` |

**Other Queensland regions** — change `SEQ` in the feed URLs to `CNS` (Cairns), `MHB` (Maryborough–Hervey Bay), etc. Remove `/Rail` to include buses and ferries.

---

## Useful Commands

```bash
# Start
docker compose up --build -d

# Watch logs
tail -f logs/backend.log

# Force GTFS reload (clears cached schedule)
docker compose restart backend

# Stop
docker compose down
```
