# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

Real-time train tracking for South East Queensland (SEQ) using Translink's open GTFS-RT feeds (no API key required). Two main views: a Leaflet map (`/`) and a railway schematic (`/control.html`).

## Running the Project

```bash
# Start (builds on first run, ~60s due to GTFS static download)
docker compose up --build

# Background
docker compose up --build -d

# Restart backend only (e.g. after code changes)
docker compose restart backend

# Full reset (clear volumes)
docker compose down -v && docker compose up --build
```

The app is served at `http://localhost:8080`. There are no tests or linters configured.

## Architecture

Two Docker services communicate over an internal network:

- **frontend** — Nginx serving static HTML/CSS/JS, reverse-proxies `/api/*` to the backend
- **backend** — Flask + Gunicorn, two background threads:
  1. **Poll thread** (`poll_feeds()`): fetches VehiclePositions + TripUpdates + Alerts every 15s from Translink GTFS-RT, parses protobuf, computes delays and stopped-train alerts, updates a shared in-memory cache
  2. **GTFS loader thread** (`gtfs_loader_thread()`): downloads `SEQ_GTFS.zip` on startup, refreshes every 24h, builds ordered stop lists for 11 schematic lines

Frontend is **vanilla JS** — no build step, no npm.

## Key Configuration (backend/app.py)

| Constant | Default | Purpose |
|---|---|---|
| `POLL_INTERVAL` | 15s | How often to fetch GTFS-RT feeds |
| `SHAPES_REFRESH_HOURS` | 24h | How often to re-download static GTFS |
| `ALERT_SECS` | 180s | GPS staleness threshold to flag a stopped train |

## API Endpoints

| Endpoint | Returns |
|---|---|
| `/api/vehicles` | Live trains with position, colour, delay, next stops, stopped alerts |
| `/api/rail_stops` | Ordered stop lists per schematic line (11 lines) |
| `/api/shapes` | GeoJSON track polylines |
| `/api/stations` | Stations with grouped platforms and upcoming arrivals |
| `/api/alerts` | GTFS-RT service alerts |
| `/api/status` | Health: vehicle count, load status, timestamps |
| `/api/logs` | Last 200 lines of `backend.log` |
| `/api/debug/routes` | All GTFS routes with computed colours |
| `/api/debug/live` | Route IDs currently active in realtime feed |
| `/api/debug/rail_stops` | First/last 3 stops per schematic line |

## Schematic Lines (control.html)

11 lines branching from Roma Street at 45° angles: `FER`, `BEL`, `CAB`, `KCL`, `SHO`, `AIR`, `DOO`, `GOL`, `CLV`, `IPL`, `SPR`.

Official line colours: Red `#e3000f` (Ferny Grove, Beenleigh), Green `#007b40` (Caboolture/Ipswich/Kippa-Ring group), Gold `#f5a400` (Gold Coast, Airport), Blue `#00aeef` (Shorncliffe, Springfield), Purple `#7b2d8b` (Doomben, Cleveland).

## GTFS Data Sources (Translink Open Data)

- Vehicle Positions: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail`
- Trip Updates: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Rail`
- Alerts: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/alerts`
- Static GTFS: `https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip`

Replace `SEQ` with `CNS`, `MHB`, etc. for other QLD regions. Remove `/Rail` to include buses/ferries.

## Logs

Backend logs are written to `./logs/backend.log` (host-mounted volume) and viewable at `/logs.html`.
