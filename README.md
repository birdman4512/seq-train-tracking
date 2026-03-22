# 🚆 TrainInt — SEQ Live Train Tracker

Real-time train tracking for South East Queensland, powered by **Translink GTFS-RT open data** (CC-BY, no API key required). All station and route data is loaded dynamically from the GTFS feed — zero hardcoded values.

## Features

**🗺️ Map View** (`/`)
- All active SEQ trains updated every 15 seconds with smooth GPS-interpolated animation
- Colour-coded by official Translink line colours (Red, Green, Gold, Blue, Purple)
- Grey track lines with coloured train markers
- Click a train: headsign, delay, speed, heading, next 5 stops with per-stop delay
- Click a station: upcoming arrivals with countdown timers — click any to track that train
- Search sidebar by route code, destination or line name; sorted by delay
- Toggle labels; switch to Control view from header

**🖥️ Control View** (`/control.html`)
- Railway schematic — Roma Street at centre, lines branching outward at 45°
- Every GTFS station placed automatically via geo-projection (no hardcoded lists)
- Direction arrows on train icons showing travel direction
- Smooth animation between schematic positions; trains move along the lines
- Click trains or stations for details; click blank space to deselect
- Pan (drag) and zoom (scroll/pinch) to explore the full network
- Stopped-train alerts: pulsing red ring when a train hasn't moved for 3+ minutes

**⚠️ Stopped-Train Alerts** (backend-computed)
- Still at origin station past scheduled departure time
- GPS position unchanged for 3+ minutes while in transit
- Dwelling at a mid-route station for longer than 3 minutes

---

## Quick Start

```bash
mkdir -p logs
docker compose up --build
```

Open **http://localhost:8080**

> **First build:** ~60s. GTFS static data downloads on first run (~30s) — control view stations appear once loaded.  
> **Backend changes:** `docker compose down && docker compose up --build`  
> **Force GTFS reload:** `docker compose down -v && docker compose up --build`

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
                              │  alerts                      │
                              │  SEQ_GTFS.zip (static)       │
                              └──────────────────────────────┘
```

**Backend** (`backend/app.py`) — two threads:
- **Poll thread**: fetches VehiclePositions + TripUpdates + Alerts every 15s; computes delays, next stops, stopped-train alerts
- **GTFS loader**: downloads schedule ZIP on startup, refreshes every 24h; builds all rail stop lists and shapes dynamically

**Frontend** — static HTML/CSS/JS, no build step. All `/api/*` proxied through nginx.

**Logs** — `./logs/backend.log` (host-mounted)

---

## Line Colours

| Colour | Lines |
|--------|-------|
| 🔴 Red `#e3000f` | Ferny Grove, Beenleigh |
| 🟢 Green `#007b40` | Caboolture, Nambour, Gympie, Ipswich, Rosewood, Kippa-Ring |
| 🟡 Gold `#f5a400` | Gold Coast, Airport |
| 🔵 Blue `#00aeef` | Shorncliffe, Springfield |
| 🟣 Purple `#7b2d8b` | Doomben, Cleveland |

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/vehicles` | Live vehicles — position, colour, delay, next stops, stopped alerts |
| `GET /api/rail_stops` | All unique rail stations with lat/lon (from GTFS static) |
| `GET /api/shapes` | Track shapes as GeoJSON FeatureCollection |
| `GET /api/stations` | Stations with current/upcoming arrivals |
| `GET /api/alerts` | Rail service alerts |
| `GET /api/status` | Backend health and GTFS load status |
| `GET /api/logs` | Last 200 lines of backend log |
| `GET /api/debug/routes` | GTFS route IDs, names and computed colours |
| `GET /api/debug/live` | Route IDs currently active in the realtime feed |

---

## Configuration

| Setting | File | Default |
|---------|------|---------|
| Poll interval | `backend/app.py` → `POLL_INTERVAL` | 15s |
| GTFS refresh | `backend/app.py` → `SHAPES_REFRESH_HOURS` | 24h |
| Stopped-train alert threshold | `backend/app.py` → `ALERT_SECS` | 180s (3 min) |
| Exposed port | `docker-compose.yml` | 8080 |
| Log folder | `docker-compose.yml` | `./logs` |

**Other QLD regions** — replace `SEQ` in the feed URLs in `backend/app.py` with `CNS` (Cairns), `MHB` (Maryborough–Hervey Bay), etc. Remove `/Rail` to include buses and ferries.

---

## Useful Commands

```bash
docker compose up --build -d          # start in background
docker compose up --build             # start in foreground (see logs)
tail -f logs/backend.log              # watch backend log
docker compose restart backend        # restart backend only (re-downloads GTFS)
docker compose down                   # stop everything
docker compose down -v && docker compose up --build  # full reset (clears volumes)
```
