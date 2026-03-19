# 🚆 Queensland Live Train Tracker

Real-time train tracking map for South East Queensland, powered by
**Queensland Government Translink GTFS-RT open data** (CC-BY licence).

No API key required — the Translink feed is completely open.

---

## Features

- 🗺️ **Live map** of all active SEQ trains, updated every 15 seconds
- 🟢🔴🔵 **Colour-coded delay status** (on-time / late / early)
- 🧭 **Bearing arrows** showing direction of travel
- ⚡ **Speed indicator** per train (km/h)
- ⚠️ **Service alerts** panel (disruptions, track works)
- 🔍 **Search** by route ID or train label
- 📍 **Click a train** for detailed info: trip ID, next stops, delay at each stop
- ⏸ **Pause/resume** live tracking
- 🏷️ **Toggle labels** on/off

---

## Quick Start

### Requirements
- Docker & Docker Compose (v2+)

### Run

```bash
git clone <this-repo>
cd qld-train-tracker
docker compose up --build
```

Then open **http://localhost:8080** in your browser.

> First startup pulls the Python + nginx images and installs dependencies (~60s).
> Subsequent starts are fast thanks to Docker layer caching.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser  →  http://localhost:8080                  │
│                                                     │
│  ┌──────────────────────┐  ┌───────────────────┐   │
│  │  frontend (nginx)    │  │  backend (Flask)  │   │
│  │  - Leaflet map       │  │  - GTFS-RT parser │   │
│  │  - Dark theme UI     │◄─┤  - 15s poll loop  │   │
│  │  - /api/* proxied    │  │  - /api/vehicles  │   │
│  └──────────────────────┘  │  - /api/alerts    │   │
│                            │  - /api/status    │   │
│                            └────────┬──────────┘   │
└─────────────────────────────────────┼───────────────┘
                                      │ protobuf
                              ┌───────▼──────────────┐
                              │  Translink GTFS-RT   │
                              │  (Queensland Gov)    │
                              │  SEQ VehiclePos/Rail │
                              │  SEQ TripUpdates/Rail│
                              │  SEQ alerts          │
                              └──────────────────────┘
```

---

## Data Sources

| Feed | URL |
|------|-----|
| Vehicle Positions | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail` |
| Trip Updates | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Rail` |
| Alerts | `https://gtfsrt.api.translink.com.au/api/realtime/SEQ/alerts` |
| GTFS Schedule | `https://www.data.qld.gov.au/dataset/general-transit-feed-specification-gtfs-translink` |

Licensed under [Creative Commons CC-BY](https://creativecommons.org/licenses/by/4.0/).
Data provided by [Translink Queensland](https://translink.com.au/about-translink/open-data).

---

## API Endpoints (backend :5000)

| Endpoint | Description |
|----------|-------------|
| `GET /api/vehicles` | All active rail vehicles with positions, speed, delay |
| `GET /api/alerts` | Current service alerts |
| `GET /api/status` | Backend health + feed status |

---

## Customisation

**Poll interval** — edit `POLL_INTERVAL` in `backend/app.py` (default 15s).

**Other regions** — edit the URLs in `backend/app.py`:
- Replace `SEQ` with `CNS` (Cairns), `MHB` (Maryborough–Hervey Bay), etc.
- Remove `/Rail` suffix to include buses, ferries and trams too.

**Map centre** — edit `SEQ_CENTRE` in `frontend/index.html`.

---

## Stopping

```bash
docker compose down
```
