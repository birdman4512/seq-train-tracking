import os
import io
import csv
import time
import zipfile
import threading
import logging
import re
import traceback
from logging.handlers import RotatingFileHandler
from flask import Flask, jsonify
from flask_cors import CORS
import requests
from google.transit import gtfs_realtime_pb2

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR  = "/logs"
LOG_FILE = os.path.join(LOG_DIR, "backend.log")
os.makedirs(LOG_DIR, exist_ok=True)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
file_handler   = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
stream_handler = logging.StreamHandler()
file_handler.setFormatter(fmt)
stream_handler.setFormatter(fmt)
logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, stream_handler])
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GTFS_ZIP_URL          = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
VEHICLE_POSITIONS_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail"
TRIP_UPDATES_URL      = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Rail"
ALERTS_URL            = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/alerts"
POLL_INTERVAL        = 15
SHAPES_REFRESH_HOURS = 24

app = Flask(__name__)
CORS(app)

cache = {
    "vehicles":      [],
    "trip_updates":  {},
    "alerts":        [],
    "shapes":        [],
    "stop_names":    {},   # stop_id -> stop_name
    "stop_coords":   {},   # stop_id -> (lat, lon)
    "route_names":   {},   # route_id -> short name
    "route_colors":  {},   # route_id -> hex colour from GTFS routes.txt
    "trip_headsigns": {},  # trip_id -> headsign (final destination)
    "shapes_loaded": False,
    "last_updated":  None,
    "shapes_updated": None,
    "error":         None,
    "debug":         {},
}

# Per-vehicle position history for bearing calculation: id -> (lat, lon)
position_history = {}
position_history_lock = threading.Lock()
cache_lock = threading.Lock()

HEADERS = {"User-Agent": "QLD-Train-Tracker/1.0"}


# ── GTFS static loader ────────────────────────────────────────────────────────


# Translink SEQ GTFS route_id format: [LINE][DEST]-[TRIP]
# e.g. SHBR-4727 = Shorncliffe->BRisbane trip 4727
# First 2 letters identify the LINE, which maps to the official colour.
# Also supports the BN-prefix format (BNFG, BNCAB etc) and short codes.
ROUTE_COLOUR_KEYWORDS = [
    # 2-letter LINE prefix (actual format seen in live feed)
    ('SH', '#00aeef'),  # Shorncliffe       — Light Blue
    ('SP', '#1c63b7'),  # Springfield        — Blue
    ('VL', '#f5a400'),  # Varsity Lakes (GC) — Gold
    ('RW', '#f47920'),  # Rosewood/Ipswich   — Orange
    ('FG', '#e3000f'),  # Ferny Grove        — Red
    ('DO', '#e0007f'),  # Doomben            — Pink
    ('AP', '#6b21a8'),  # Airport            — Indigo
    ('CL', '#ffd600'),  # Cleveland          — Yellow
    ('BE', '#6fbe44'),  # Beenleigh          — Green
    ('CA', '#7b2d8b'),  # Caboolture         — Purple
    ('NA', '#7b2d8b'),  # Nambour            — Purple
    ('KP', '#00b5cc'),  # Kippa-Ring         — Teal
    ('IP', '#f47920'),  # Ipswich            — Orange
    ('RP', '#00b5cc'),  # Redcliffe Pen      — Teal
    ('GL', '#f5a400'),  # Gold Coast (alt)   — Gold
    ('IM', '#6fbe44'),  # Inner Metro        — Green
    # BN-prefix format
    ('BNFG',  '#e3000f'), ('BNCAB', '#7b2d8b'), ('BNKCL', '#00b5cc'),
    ('BNIPL', '#f47920'), ('BNSPR', '#1c63b7'), ('BNBEL', '#6fbe44'),
    ('BNCLV', '#ffd600'), ('BNGOL', '#f5a400'), ('BNSH',  '#00aeef'),
    ('BNAIR', '#6b21a8'), ('BNDOO', '#e0007f'), ('BNNAI', '#7b2d8b'),
    # Short fallbacks
    ('FER', '#e3000f'), ('CAB', '#7b2d8b'), ('KCL', '#00b5cc'),
    ('IPL', '#f47920'), ('SPR', '#1c63b7'), ('BEL', '#6fbe44'),
    ('CLV', '#ffd600'), ('GOL', '#f5a400'), ('AIR', '#6b21a8'),
    ('DOO', '#e0007f'), ('IMU', '#6fbe44'), ('NAI', '#7b2d8b'),
    ('ROS', '#f47920'), ('BAN', '#6b21a8'),
]

_unknown_routes_logged = set()

# Human-readable line names keyed by the 2-letter prefix in route_id (e.g. SHBR-4727 → 'SH')
LINE_NAMES = {
    'SH': 'Shorncliffe',
    'SP': 'Springfield',
    'VL': 'Gold Coast',
    'RW': 'Ipswich / Rosewood',
    'FG': 'Ferny Grove',
    'DO': 'Doomben',
    'AP': 'Airport',
    'CL': 'Cleveland',
    'BE': 'Beenleigh',
    'CA': 'Caboolture',
    'NA': 'Nambour / Sunshine Coast',
    'KP': 'Kippa-Ring',
    'IP': 'Ipswich',
    'RP': 'Redcliffe Peninsula',
    'GL': 'Gold Coast',
    'IM': 'Inner Metro',
}

def route_line_name(route_id):
    """Return a friendly line name for a route_id like SHBR-4727."""
    if not route_id:
        return ''
    prefix = route_id[:2].upper()
    return LINE_NAMES.get(prefix, '')

def route_colour(route_id, route_name=''):
    combined = (str(route_id) + ' ' + str(route_name)).upper()
    for keyword, col in ROUTE_COLOUR_KEYWORDS:
        if keyword in combined:
            return col
    if route_id and route_id not in _unknown_routes_logged:
        _unknown_routes_logged.add(route_id)
        logger.warning(f"No colour match: route_id={route_id!r} name={route_name!r}")
    return '#5bb8ff'


def load_gtfs_static():
    """Download SEQ GTFS zip and extract:
       - Rail route shapes (for drawing tracks)
       - Stop id → name mapping
       - Trip id → headsign (destination) mapping
    """
    logger.info("Downloading SEQ GTFS static data…")
    resp = requests.get(GTFS_ZIP_URL, timeout=60, headers=HEADERS)
    resp.raise_for_status()
    logger.info(f"  Downloaded {len(resp.content):,} bytes")

    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    # 1. Rail route IDs (route_type == 2)
    #    Also read route_color directly from routes.txt (official hex from Translink)
    #    and route_short_name / route_long_name as fallback for colour matching
    rail_route_ids  = set()
    route_names     = {}   # route_id -> display name
    route_colors    = {}   # route_id -> hex colour from GTFS
    with zf.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("route_type", "").strip() == "2":
                rid   = row["route_id"].strip()
                rail_route_ids.add(rid)
                name  = row.get("route_short_name","").strip() or row.get("route_long_name","").strip()
                color = row.get("route_color","").strip().lstrip("#")
                route_names[rid]  = name
                if color and len(color) == 6:
                    route_colors[rid] = "#" + color
    sample = [(rid, route_names.get(rid,""), route_colors.get(rid,"")) for rid in list(rail_route_ids)[:6]]
    logger.info(f"  {len(rail_route_ids)} rail routes — sample: {sample}")

    # 2. Trip headsigns + shape IDs for rail trips
    #    Also map shape_id -> route_id so shapes can be coloured
    rail_shape_ids   = set()
    trip_headsigns   = {}   # trip_id -> trip_headsign
    shape_to_route   = {}   # shape_id -> route_id
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid = row.get("route_id", "").strip()
            if rid not in rail_route_ids:
                continue
            tid = row.get("trip_id",      "").strip()
            sid = row.get("shape_id",     "").strip()
            hs  = row.get("trip_headsign","").strip()
            if sid:
                rail_shape_ids.add(sid)
                shape_to_route.setdefault(sid, rid)  # first route wins
            if tid and hs:
                trip_headsigns[tid] = hs
    logger.info(f"  {len(rail_shape_ids)} shape IDs, {len(trip_headsigns)} trip headsigns")

    # 3. Stop names and coordinates
    stop_names  = {}
    stop_coords = {}
    with zf.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid  = row.get("stop_id",   "").strip()
            name = row.get("stop_name", "").strip()
            try:
                lat = float(row.get("stop_lat", "0"))
                lon = float(row.get("stop_lon", "0"))
            except ValueError:
                lat = lon = 0.0
            if sid and name:
                # Strip ", platform N" suffix (e.g. "Roma Street station, platform 1")
                # Only strip if it ends with ", platform <digits>" — be precise
                clean_name = re.sub(r',\s*[Pp]latform\s*\d+\s*$', '', name).strip()
                stop_names[sid]  = clean_name
                if lat and lon:
                    stop_coords[sid] = (lat, lon)
    logger.info(f"  {len(stop_names)} stops loaded, {len(stop_coords)} with coords")

    # 4. Build deduplicated track shapes
    shape_points = {}
    with zf.open("shapes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid = row.get("shape_id", "").strip()
            if sid not in rail_shape_ids:
                continue
            try:
                seq = int(row["shape_pt_sequence"])
                lat = float(row["shape_pt_lat"])
                lon = float(row["shape_pt_lon"])
            except (ValueError, KeyError):
                continue
            shape_points.setdefault(sid, []).append((seq, lat, lon))

    seen_sigs     = set()
    geojson_lines = []
    for sid, pts in shape_points.items():
        pts.sort(key=lambda x: x[0])
        coords = [[lon, lat] for _, lat, lon in pts]
        if len(coords) < 2:
            continue
        sig = (round(coords[0][1], 3), round(coords[0][0], 3),
               round(coords[-1][1], 3), round(coords[-1][0], 3))
        rev = (sig[2], sig[3], sig[0], sig[1])
        if sig in seen_sigs or rev in seen_sigs:
            continue
        seen_sigs.add(sig)
        rid    = shape_to_route.get(sid, '')
        # Prefer the official route_color from routes.txt, fall back to name matching
        colour = route_colors.get(rid) or route_colour(rid, route_names.get(rid, ''))
        geojson_lines.append({"coords": coords, "route_id": rid, "colour": colour})

    logger.info(f"  {len(geojson_lines)} deduplicated track segments")
    return geojson_lines, stop_names, stop_coords, trip_headsigns, route_names, route_colors


def gtfs_loader_thread():
    while True:
        try:
            shapes, stop_names, stop_coords, trip_headsigns, route_names, route_colors = load_gtfs_static()
            with cache_lock:
                cache["shapes"]         = shapes
                cache["stop_names"]     = stop_names
                cache["stop_coords"]    = stop_coords
                cache["route_names"]    = route_names
                cache["route_colors"]   = route_colors
                cache["trip_headsigns"] = trip_headsigns
                cache["shapes_loaded"]  = True
                cache["shapes_updated"] = time.time()
            logger.info("GTFS static data loaded and cached")
        except Exception as e:
            logger.error(f"Failed to load GTFS static data: {e}")
            logger.error(traceback.format_exc())
        time.sleep(SHAPES_REFRESH_HOURS * 3600)


# ── GTFS-RT helpers ───────────────────────────────────────────────────────────

def fetch_feed(url):
    logger.debug(f"GET {url}")
    resp = requests.get(url, timeout=15, headers=HEADERS)
    logger.debug(f"  HTTP {resp.status_code}  bytes={len(resp.content)}")
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    logger.debug(f"  Parsed {len(feed.entity)} entities")
    return feed, len(resp.content)


def safe_enum_name(descriptor, value, fallback="UNKNOWN"):
    try:
        return descriptor.Name(value)
    except Exception:
        return fallback


def calculate_bearing(lat1, lon1, lat2, lon2):
    """Calculate compass bearing (0=N, 90=E, 180=S, 270=W) from point 1 to point 2."""
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def parse_vehicles(feed, stop_names, trip_headsigns):
    vehicles     = []
    skipped_zero = 0
    errors       = 0

    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        try:
            v   = entity.vehicle
            pos = v.position
            if pos.latitude == 0.0 and pos.longitude == 0.0:
                skipped_zero += 1
                continue

            try:
                occupancy = safe_enum_name(
                    gtfs_realtime_pb2.VehiclePosition.OccupancyStatus, v.occupancy_status)
            except Exception:
                occupancy = "UNKNOWN"

            current_stop_id   = v.stop_id if v.stop_id else None
            current_stop_name = stop_names.get(current_stop_id, current_stop_id) if current_stop_id else None

            trip_id    = v.trip.trip_id
            headsign   = trip_headsigns.get(trip_id, None)

            # Use feed bearing if provided, otherwise calculate from last known position
            feed_bearing = pos.bearing if pos.bearing else None
            calc_bearing = None
            vid = entity.id
            with position_history_lock:
                if vid in position_history:
                    prev_lat, prev_lon = position_history[vid]
                    dist = abs(pos.latitude - prev_lat) + abs(pos.longitude - prev_lon)
                    if dist > 0.0001:  # only recalculate if moved meaningfully
                        calc_bearing = calculate_bearing(prev_lat, prev_lon, pos.latitude, pos.longitude)
                position_history[vid] = (pos.latitude, pos.longitude)
            bearing = feed_bearing if feed_bearing is not None else (calc_bearing if calc_bearing is not None else 0)

            vehicles.append({
                "id":                    entity.id,
                "trip_id":               trip_id,
                "route_id":              v.trip.route_id,
                "direction_id":          v.trip.direction_id,
                "lat":                   pos.latitude,
                "lon":                   pos.longitude,
                "bearing":               round(bearing, 1),
                "bearing_source":        "feed" if feed_bearing else ("calculated" if calc_bearing else "none"),
                "speed":                 round(pos.speed * 3.6, 1) if pos.speed else None,
                "label":                 v.vehicle.label if v.vehicle.label else entity.id,
                "current_stop_sequence": v.current_stop_sequence,
                "current_stop_id":       current_stop_id,
                "current_stop_name":     current_stop_name,
                "headsign":              headsign,
                "current_status":        safe_enum_name(
                                             gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus,
                                             v.current_status, "IN_TRANSIT_TO"),
                "timestamp":             v.timestamp,
                "congestion":            safe_enum_name(
                                             gtfs_realtime_pb2.VehiclePosition.CongestionLevel,
                                             v.congestion_level, "UNKNOWN_CONGESTION_LEVEL"),
                "occupancy":             occupancy,
            })
        except Exception as e:
            errors += 1
            logger.error(f"Error parsing vehicle {entity.id}: {e}")
            logger.debug(traceback.format_exc())

    if vehicles:
        sample_routes = list({v["route_id"] for v in vehicles if v.get("route_id")})[:10]
        logger.info(f"  Sample route_ids from feed: {sample_routes}")
    logger.info(f"  Vehicles: {len(vehicles)} ok, {skipped_zero} zero-pos, {errors} errors")
    return vehicles


def parse_trip_updates(feed, stop_names):
    updates = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        try:
            tu      = entity.trip_update
            trip_id = tu.trip.trip_id
            stop_times = []
            for stu in tu.stop_time_update:
                stop_id   = stu.stop_id
                stop_name = stop_names.get(stop_id, stop_id)
                stop_times.append({
                    "stop_sequence":   stu.stop_sequence,
                    "stop_id":         stop_id,
                    "stop_name":       stop_name,
                    "arrival_delay":   stu.arrival.delay   if stu.HasField("arrival")   else None,
                    "departure_delay": stu.departure.delay if stu.HasField("departure") else None,
                    "arrival_time":    stu.arrival.time    if stu.HasField("arrival")   else None,
                    "departure_time":  stu.departure.time  if stu.HasField("departure") else None,
                })
            delay = 0
            if stop_times and stop_times[0]["arrival_delay"] is not None:
                delay = stop_times[0]["arrival_delay"]
            updates[trip_id] = {
                "trip_id":           trip_id,
                "route_id":          tu.trip.route_id,
                "delay":             delay,
                "stop_time_updates": stop_times,  # keep all — filtered per-vehicle in api_vehicles
            }
        except Exception as e:
            logger.error(f"Error parsing trip update {entity.id}: {e}")
    return updates


def parse_alerts(feed):
    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        try:
            a           = entity.alert
            header      = next((t.text for t in a.header_text.translation      if t.language == "en"), "")
            description = next((t.text for t in a.description_text.translation if t.language == "en"), "")
            if not header      and a.header_text.translation:      header      = a.header_text.translation[0].text
            if not description and a.description_text.translation: description = a.description_text.translation[0].text
            informed = list({ie.route_id for ie in a.informed_entity if ie.route_id})
            alerts.append({
                "id":          entity.id,
                "header":      header,
                "description": description,
                "cause":       safe_enum_name(gtfs_realtime_pb2.Alert.Cause,  a.cause,  "UNKNOWN_CAUSE"),
                "effect":      safe_enum_name(gtfs_realtime_pb2.Alert.Effect, a.effect, "UNKNOWN_EFFECT"),
                "routes":      informed,
            })
        except Exception as e:
            logger.error(f"Error parsing alert {entity.id}: {e}")
    return alerts


# ── Poll loop ─────────────────────────────────────────────────────────────────

def poll_feeds():
    logger.info(f"Poll thread started (pid={os.getpid()})")
    while True:
        debug_info = {}
        try:
            logger.info("--- Polling GTFS-RT feeds ---")
            veh_feed, veh_bytes = fetch_feed(VEHICLE_POSITIONS_URL)
            tu_feed,  _         = fetch_feed(TRIP_UPDATES_URL)
            al_feed,  _         = fetch_feed(ALERTS_URL)

            debug_info["vehicle_feed_entities"] = len(veh_feed.entity)
            debug_info["vehicle_feed_bytes"]    = veh_bytes

            with cache_lock:
                stop_names     = cache["stop_names"]
                trip_headsigns = cache["trip_headsigns"]

            vehicles     = parse_vehicles(veh_feed, stop_names, trip_headsigns)
            trip_updates = parse_trip_updates(tu_feed, stop_names)
            alerts       = parse_alerts(al_feed)

            debug_info["vehicles_parsed"]     = len(vehicles)
            debug_info["trip_updates_parsed"] = len(trip_updates)
            if vehicles:
                debug_info["sample_vehicle"] = vehicles[0]

            with cache_lock:
                cache["vehicles"]     = vehicles
                cache["trip_updates"] = trip_updates
                cache["alerts"]       = alerts
                cache["last_updated"] = time.time()
                cache["error"]        = None
                cache["debug"]        = debug_info

            logger.info(f"Cache updated: {len(vehicles)} vehicles, "
                        f"{len(trip_updates)} trip updates, {len(alerts)} alerts")

        except requests.exceptions.ConnectionError as e:
            msg = f"Network error: {e}"
            logger.error(msg)
            with cache_lock:
                cache["error"] = msg
        except requests.exceptions.HTTPError as e:
            msg = f"HTTP error: {e}"
            logger.error(msg)
            with cache_lock:
                cache["error"] = msg
        except Exception as e:
            msg = f"Unexpected error: {e}"
            logger.error(msg)
            logger.error(traceback.format_exc())
            with cache_lock:
                cache["error"] = msg

        time.sleep(POLL_INTERVAL)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/vehicles")
def api_vehicles():
    with cache_lock:
        vehicles     = list(cache["vehicles"])
        trip_updates = dict(cache["trip_updates"])
        last_updated = cache["last_updated"]
        error        = cache["error"]
    for v in vehicles:
        tu          = trip_updates.get(v["trip_id"], {})
        all_stops   = tu.get("stop_time_updates", [])
        current_seq = v.get("current_stop_sequence", 0)
        status      = v.get("current_status", "")

        # Sort by stop_sequence ascending so filtering is reliable
        all_stops = sorted(all_stops, key=lambda s: s.get("stop_sequence", 0))

        # For IN_TRANSIT_TO / INCOMING_AT the train hasn't reached current_seq yet —
        # show current_seq and onwards.
        # For STOPPED_AT the train is AT current_seq — show the next stop onwards
        # (current_seq + 1) so we don't show the station the train is already at.
        if status == "STOPPED_AT":
            cutoff = current_seq + 1
        else:
            cutoff = current_seq

        upcoming = [s for s in all_stops if s.get("stop_sequence", 0) >= cutoff]

        # Fallback: if filtering left nothing, show all remaining
        if not upcoming and all_stops:
            upcoming = all_stops

        # Delay from the first upcoming stop
        delay = 0
        for s in upcoming:
            if s.get("arrival_delay") is not None:
                delay = s["arrival_delay"]
                break

        v["delay_seconds"] = delay
        v["next_stops"]    = upcoming[:5]
        v["line_name"]     = route_line_name(v.get("route_id", ""))
    return jsonify({
        "vehicles":      vehicles,
        "count":         len(vehicles),
        "last_updated":  last_updated,
        "poll_interval": POLL_INTERVAL,
        "error":         error,
    })


@app.route("/api/shapes")
def api_shapes():
    with cache_lock:
        shapes         = cache["shapes"]
        shapes_loaded  = cache["shapes_loaded"]
        shapes_updated = cache["shapes_updated"]
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": s["coords"]},
            "properties": {"route_id": s["route_id"], "colour": s["colour"]},
        }
        for s in shapes
    ]
    return jsonify({
        "type":           "FeatureCollection",
        "features":       features,
        "loaded":         shapes_loaded,
        "shapes_updated": shapes_updated,
        "segment_count":  len(shapes),
    })


@app.route("/api/stations")
def api_stations():
    """Return per-stop upcoming arrivals derived from live trip updates."""
    with cache_lock:
        vehicles     = list(cache["vehicles"])
        trip_updates = dict(cache["trip_updates"])
        stop_names   = dict(cache["stop_names"])
        last_updated = cache["last_updated"]

    # Build stop_id -> list of upcoming trains
    arrivals = {}  # stop_id -> [{route_id, headsign, arrival_time, arrival_delay, trip_id}]

    now = time.time()

    # Build vehicle lookup by trip_id for quick headsign/sequence access
    veh_by_trip = {v["trip_id"]: v for v in vehicles if v.get("trip_id")}

    for trip_id, tu in trip_updates.items():
        route_id = tu.get("route_id", "")
        veh      = veh_by_trip.get(trip_id, {})
        headsign = veh.get("headsign", "")
        current_seq = veh.get("current_stop_sequence", 0)

        # Only include upcoming stops
        for s in sorted(tu.get("stop_time_updates", []), key=lambda x: x.get("stop_sequence", 0)):
            if s.get("stop_sequence", 0) < current_seq:
                continue
            sid      = s.get("stop_id")
            arr_time = s.get("arrival_time")
            arr_delay = s.get("arrival_delay") or 0
            if not sid or not arr_time:
                continue
            # Skip stops already passed (more than 30s ago)
            if arr_time < now - 30:
                continue
            # actual_arrival = scheduled time + delay offset
            actual_arrival = arr_time + arr_delay
            arrivals.setdefault(sid, []).append({
                "trip_id":        trip_id,
                "route_id":       route_id,
                "headsign":       headsign,
                "scheduled_time": arr_time,
                "arrival_time":   actual_arrival,   # what we display
                "delay":          arr_delay,
                "stop_name":      stop_names.get(sid, sid),
            })

    # Sort each stop's list by arrival_time, keep next 6
    for sid in arrivals:
        arrivals[sid].sort(key=lambda x: x["arrival_time"])  # sorted by actual (scheduled+delay)
        arrivals[sid] = arrivals[sid][:6]

    # Also include GPS coords from stop_names isn't enough — we need stop coords
    # Return flat list of stops that have arrivals, with coords from cache
    stop_coords = cache.get("stop_coords", {})

    result = []
    for sid, trains in arrivals.items():
        coords = stop_coords.get(sid)
        if not coords:
            continue
        result.append({
            "stop_id":   sid,
            "stop_name": stop_names.get(sid, sid),
            "lat":       coords[0],
            "lon":       coords[1],
            "arrivals":  trains,
        })

    return jsonify({
        "stations":    result,
        "count":       len(result),
        "last_updated": last_updated,
    })


@app.route("/api/debug/live")
def api_debug_live():
    """Show the raw route_ids currently in the realtime feed — use this to fix colour matching."""
    with cache_lock:
        vehicles    = list(cache["vehicles"])
        route_colors = dict(cache.get("route_colors", {}))
        route_names  = dict(cache.get("route_names", {}))
    
    # Collect unique route_ids from live feed
    live = {}
    for v in vehicles:
        rid = v.get("route_id", "")
        if rid not in live:
            live[rid] = {
                "route_id": rid,
                "count": 0,
                "example_headsign": v.get("headsign", ""),
                "gtfs_colour": route_colors.get(rid, ""),
                "computed_colour": route_colour(rid, route_names.get(rid, "")),
            }
        live[rid]["count"] += 1
    
    return jsonify({
        "live_route_ids": sorted(live.values(), key=lambda x: -x["count"]),
        "total_vehicles": len(vehicles),
    })


@app.route("/api/debug/routes")
def api_debug_routes():
    """Show actual route_ids and names from GTFS + colour assignments — for debugging."""
    with cache_lock:
        route_names = dict(cache.get("route_names", {}))
        vehicles    = list(cache["vehicles"])
    rt_vehicle = sorted({v["route_id"] for v in vehicles if v.get("route_id")})
    route_colors = dict(cache.get("route_colors", {}))
    routes_coloured = [
        {"route_id": rid, "name": route_names.get(rid,""),
         "gtfs_color": route_colors.get(rid,""),
         "computed_colour": route_colour(rid, route_names.get(rid,""))}
        for rid in sorted(route_names.keys())
    ]
    return jsonify({
        "live_route_ids":   rt_vehicle,
        "gtfs_routes":      routes_coloured[:40],
        "unknown_routes":   list(_unknown_routes_logged),
    })


@app.route("/api/alerts")
def api_alerts():
    with cache_lock:
        return jsonify({"alerts": cache["alerts"], "last_updated": cache["last_updated"]})


@app.route("/api/status")
def api_status():
    with cache_lock:
        return jsonify({
            "status":          "error" if cache["error"] else "ok",
            "vehicle_count":   len(cache["vehicles"]),
            "last_updated":    cache["last_updated"],
            "shapes_loaded":   cache["shapes_loaded"],
            "shapes_segments": len(cache["shapes"]),
            "stops_loaded":    len(cache["stop_names"]),
            "poll_interval":   POLL_INTERVAL,
            "error":           cache["error"],
        })


@app.route("/api/debug")
def api_debug():
    with cache_lock:
        vehicles = list(cache["vehicles"])
    bearing_summary = {
        "feed":       sum(1 for v in vehicles if v.get("bearing_source") == "feed"),
        "calculated": sum(1 for v in vehicles if v.get("bearing_source") == "calculated"),
        "none":       sum(1 for v in vehicles if v.get("bearing_source") == "none"),
        "samples":    [{"id": v["id"], "route": v["route_id"], "bearing": v["bearing"], "source": v.get("bearing_source")}
                       for v in vehicles[:5]],
    }
    with cache_lock:
        return jsonify({
            "error":          cache["error"],
            "vehicle_count":  len(cache["vehicles"]),
            "last_updated":   cache["last_updated"],
            "bearing_summary": bearing_summary,
            "debug":          cache["debug"],
        })


@app.route("/api/logs")
def api_logs():
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-200:], "total_lines": len(lines), "log_file": LOG_FILE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Start background threads ──────────────────────────────────────────────────

_gtfs_thread = threading.Thread(target=gtfs_loader_thread, daemon=True)
_gtfs_thread.start()

_poll_thread = threading.Thread(target=poll_feeds, daemon=True)
_poll_thread.start()

logger.info(f"Backend started (pid={os.getpid()})")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
