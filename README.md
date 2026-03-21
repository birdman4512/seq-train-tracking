# 🚆 SEQ Live Train Tracker

Real-time train tracking for South East Queensland, powered by **Translink GTFS-RT open data** (CC-BY, no API key required).

## Features

**🗺️ Map View**
- All active SEQ trains updated every 15 seconds with smooth animation between GPS fixes
- Colour-coded by official Translink line colours (Red, Green, Gold, Blue, Purple)
- Bearing arrows showing direction of travel
- Click a train for detail panel: headsign, delay, speed, heading, next stops with arrival times and per-stop delay status
- Click a station marker to see upcoming arrivals — click any arrival to pan to and track that train
- Search sidebar by route code, destination or line name
- Toggle Labels, Live/Pause, and switch to Control view from header

**🖥️ Control View** (`/control.html`)
- Railway schematic with Roma Street at centre, lines branching outward at 45°
- Trains shown as coloured rectangles on the correct line segment
- Click trains or stations for details and next arrivals

**📋 Sidebar**
- Train cards show destination in line colour, next stop + minutes
- Sorted by delay (most late first)
- Collapsed by default — toggle with ☰ button

**📱 Mobile**
- Responsive header fits small screens
- Sidebar collapses to a drawer
- Info panel adapts to viewport width

**⚠️ Alerts**
- Filtered to rail-only service alerts from the Translink feed

---

## Quick Start

```bash
mkdir -p logs
docker compose up --build
```

Open **http://localhost:8080**

> First build: ~60s. GTFS static data downloads on first run (~30s).  
> Rebuild after backend changes: `docker compose down && docker compose up --build`  
> Force GTFS reload: `docker compose down -v && docker compose up --build`

---

## Architecture

```
Browser → http://localhost:8080
           │
   ┌───────▼──────────┐       ┌──────────────────────┐
   │ nginx (frontend) │──/api/►│ Flask + gunicorn     │
   │  index.html      │       │  polls GTFS-RT/15s   │
   │  control.html    │       │  parses protobuf      │
   │  logs.html       │       │  serves JSON API      │
   └──────────────────┘       └──────────┬───────────┘
                                          │ HTTPS
                              ┌───────────▼──────────────────┐
                              │  Translink GTFS-RT feeds     │
                              │  VehiclePositions/Rail       │
                              │  TripUpdates/Rail            │
                              │  alerts (all modes)          │
                              │  SEQ_GTFS.zip (static)       │
                              └──────────────────────────────┘
```

**Backend** (`backend/app.py`) — two threads:
- **Poll thread**: fetches VehiclePositions + TripUpdates + Alerts every 15s
- **GTFS loader**: downloads schedule ZIP on startup, refreshes every 24h

**Frontend** — static HTML/CSS/JS, no build step. All `/api/*` proxied through nginx to backend.

**Logs** — `./logs/backend.log` (host-mounted).

---

## Line Colours

| Colour | Lines |
|--------|-------|
| 🔴 Red `#e3000f` | Ferny Grove, Beenleigh |
| 🟢 Green `#007b40` | Caboolture, Nambour, Gympie, Ipswich, Rosewood, Kippa-Ring, Redcliffe |
| 🟡 Gold `#f5a400` | Gold Coast, Airport |
| 🔵 Blue `#00aeef` | Shorncliffe, Springfield |
| 🟣 Purple `#7b2d8b` | Doomben, Cleveland |

---

## API Endpoints

All at `/api/` — work at root or any subfolder deployment.

| Endpoint | Description |
|----------|-------------|
| `GET /api/vehicles` | Live vehicles with position, colour, delay, next stops |
| `GET /api/shapes` | Track shapes as GeoJSON FeatureCollection |
| `GET /api/stations` | Stations with upcoming arrivals |
| `GET /api/alerts` | Rail service alerts |
| `GET /api/status` | Backend health and GTFS load status |
| `GET /api/logs` | Last 200 lines of backend log |
| `GET /api/debug/routes` | GTFS route IDs, names and computed colours |
| `GET /api/debug/live` | Route IDs currently active in realtime feed |

---

## Subfolder Deployment

Host at e.g. `https://example.com/trains/` — configure your reverse proxy to forward `/trains/*` to port 8080. No code changes needed; API paths derive from `window.location.pathname`.

---

## Configuration

| Setting | Location | Default |
|---------|----------|---------|
| Poll interval | `backend/app.py` → `POLL_INTERVAL` | 15s |
| GTFS refresh | `backend/app.py` → `SHAPES_REFRESH_HOURS` | 24h |
| Exposed port | `docker-compose.yml` | 8080 |
| Log folder | `docker-compose.yml` | `./logs` |

**Other QLD regions** — replace `SEQ` in feed URLs with `CNS` (Cairns), `MHB` (Maryborough–Hervey Bay), etc. Remove `/Rail` to include buses and ferries.

---

## Useful Commands

```bash
docker compose up --build -d          # start in background
tail -f logs/backend.log              # watch logs
docker compose restart backend        # force GTFS re-download
docker compose down                   # stop
docker compose down -v && docker compose up --build  # full reset
```
