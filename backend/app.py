import os, io, csv, time, zipfile, threading, logging, re, traceback, math
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
fh  = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
sh  = logging.StreamHandler()
fh.setFormatter(fmt); sh.setFormatter(fmt)
logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GTFS_ZIP_URL          = "https://gtfsrt.api.translink.com.au/GTFS/SEQ_GTFS.zip"
VEHICLE_POSITIONS_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/VehiclePositions/Rail"
TRIP_UPDATES_URL      = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/TripUpdates/Rail"
ALERTS_URL            = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ/alerts"
POLL_INTERVAL         = 15       # seconds
SHAPES_REFRESH_HOURS  = 24       # hours between GTFS static re-downloads
HEADERS               = {"User-Agent": "QLD-Train-Tracker/1.0"}

app = Flask(__name__)
CORS(app)

cache = {
    "vehicles":       [],
    "trip_updates":   {},
    "alerts":         [],
    "shapes":         [],
    "stop_names":     {},   # stop_id -> raw name (with platform suffix)
    "stop_coords":    {},   # stop_id -> (lat, lon)
    "route_names":    {},   # route_id -> short name from GTFS
    "route_colors":   {},   # route_id -> hex from GTFS (for debug only)
    "trip_headsigns": {},   # trip_id  -> headsign string
    "rail_route_ids": set(),
    "shapes_loaded":  False,
    "last_updated":   None,
    "shapes_updated": None,
    "error":          None,
}
cache_lock           = threading.Lock()
position_history     = {}
position_history_lock= threading.Lock()

# Stopped-train alert tracking (persists between polls)
# vid -> {first_stop_id, first_depart_time, last_lat, last_lon, last_gps_time,
#          dwell_start, dwell_stop_id}
ALERT_SECS      = 3 * 60  # 3 minutes
stopped_tracker = {}
st_lock         = threading.Lock()

# ── Colour lookup ─────────────────────────────────────────────────────────────
# Official Translink SEQ line colours.
# route_id format: {origin2}{dest2}-{trip}  e.g. IPCA-4754, BRFG-4754
# Confirmed from /api/debug/live headsign data.
#
# RED    #e3000f  Ferny Grove, Beenleigh
# GREEN  #007b40  Caboolture, Nambour, Ipswich, Rosewood, Kippa-Ring, Redcliffe
# GOLD   #f5a400  Gold Coast, Airport
# BLUE   #00aeef  Shorncliffe, Springfield
# PURPLE #7b2d8b  Doomben, Cleveland

_C = {  # route_id 4-letter code -> hex colour
    # RED
    'BRFG':'#e3000f','FGBR':'#e3000f','BNFG':'#e3000f','FGBN':'#e3000f',
    'BRBR':'#e3000f','BNBN':'#e3000f','BNBR':'#e3000f','BRBN':'#e3000f',
    'BDBN':'#e3000f','BNBD':'#e3000f',
    # GOLD
    'BDVL':'#f5a400','VLBD':'#f5a400','BNVL':'#f5a400','VLBN':'#f5a400',
    'BRVL':'#f5a400','VLBR':'#f5a400',
    'BRAP':'#f5a400','APBR':'#f5a400','BNAP':'#f5a400','APBN':'#f5a400',
    'BDAP':'#f5a400','APBD':'#f5a400','BRBD':'#f5a400','BDBR':'#f5a400','DBBR':'#f5a400',
    # BLUE
    'BRSH':'#00aeef','SHBR':'#00aeef','BNSH':'#00aeef','SHBN':'#00aeef',
    'BDSH':'#00aeef','SHBD':'#00aeef',
    'RPSP':'#00aeef','SPRP':'#00aeef','BRSP':'#00aeef','SPBR':'#00aeef',
    'BNSP':'#00aeef','SPBN':'#00aeef','BDSP':'#00aeef','SPBD':'#00aeef',
    # PURPLE
    'BRDB':'#7b2d8b','DBBN':'#7b2d8b','BRDO':'#7b2d8b','DOBR':'#7b2d8b',
    'BNDO':'#7b2d8b','DOBN':'#7b2d8b',
    'BRCL':'#7b2d8b','CLBR':'#7b2d8b','BNCL':'#7b2d8b','CLBN':'#7b2d8b',
    'BDCL':'#7b2d8b','CLBD':'#7b2d8b',
    # GREEN
    'BDCA':'#007b40','CABD':'#007b40','BNCA':'#007b40','CABN':'#007b40',
    'BRCA':'#007b40','CABR':'#007b40',
    'BRGY':'#007b40','GYBR':'#007b40','BNGY':'#007b40','GYBN':'#007b40',
    'IPCA':'#007b40','CAIP':'#007b40','IPNA':'#007b40','NAIP':'#007b40',
    'IPRW':'#007b40','RWIP':'#007b40','BRRW':'#007b40','RWBR':'#007b40',
    'BNRW':'#007b40','RWBN':'#007b40',
    'RPBR':'#007b40','BRRP':'#007b40','RPBN':'#007b40','BNRP':'#007b40','RPRP':'#007b40',
}
_PREFIX = [  # 2-letter startsWith fallbacks
    ('FG','#e3000f'),('BE','#e3000f'),
    ('VL','#f5a400'),('AP','#f5a400'),
    ('SH','#00aeef'),('SP','#00aeef'),
    ('DO','#7b2d8b'),('CL','#7b2d8b'),
    ('CA','#007b40'),('NA','#007b40'),('GY','#007b40'),
    ('IP','#007b40'),('RW','#007b40'),('RP','#007b40'),('KP','#007b40'),
]
_COLOUR_TO_LINE = {
    '#e3000f':'Ferny Grove / Beenleigh',
    '#f5a400':'Gold Coast / Airport',
    '#00aeef':'Shorncliffe / Springfield',
    '#7b2d8b':'Doomben / Cleveland',
    '#007b40':'Caboolture / Ipswich / Rosewood',
}

def line_colour(route_id):
    """Return official hex colour for a route_id. Single source of truth."""
    if not route_id:
        return '#888888'
    code = route_id.upper().split('-')[0]
    if code in _C:
        return _C[code]
    for pfx, col in _PREFIX:
        if code.startswith(pfx):
            return col
    return '#888888'

def line_name(route_id):
    """Return human-readable line name for a route_id."""
    return _COLOUR_TO_LINE.get(line_colour(route_id), '')

def strip_platform(name):
    """Remove ', platform N' suffix from a stop name."""
    return re.sub(r',?\s*[Pp]latform\s*\d+\s*$', '', name or '').strip()


# ── GTFS static loader ────────────────────────────────────────────────────────

def load_gtfs_static():
    logger.info("Downloading SEQ GTFS static data…")
    resp = requests.get(GTFS_ZIP_URL, timeout=60, headers=HEADERS)
    resp.raise_for_status()
    logger.info(f"  Downloaded {len(resp.content):,} bytes")
    zf = zipfile.ZipFile(io.BytesIO(resp.content))

    # Rail route IDs, names, colours
    rail_route_ids = set()
    route_names    = {}
    route_colors   = {}
    with zf.open("routes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            if row.get("route_type","").strip() != "2":
                continue
            rid = row["route_id"].strip()
            rail_route_ids.add(rid)
            route_names[rid] = (row.get("route_short_name","") or row.get("route_long_name","")).strip()
            color = row.get("route_color","").strip().lstrip("#")
            if color and len(color) == 6:
                route_colors[rid] = "#" + color
    logger.info(f"  {len(rail_route_ids)} rail routes")

    # trips.txt: headsigns and rail shape ids
    trip_headsigns = {}
    rail_shape_ids = set()
    with zf.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            rid = row.get("route_id","").strip()
            if rid not in rail_route_ids:
                continue
            tid = row.get("trip_id","").strip()
            sid = row.get("shape_id","").strip()
            hs  = row.get("trip_headsign","").strip()
            if sid:
                rail_shape_ids.add(sid)
            if tid and hs:
                trip_headsigns[tid] = hs

    # Stop names and coordinates (keep platform suffix for grouping later)
    stop_names  = {}
    stop_coords = {}
    with zf.open("stops.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid  = row.get("stop_id","").strip()
            name = row.get("stop_name","").strip()
            try:
                lat, lon = float(row.get("stop_lat","0")), float(row.get("stop_lon","0"))
            except ValueError:
                lat = lon = 0.0
            if sid and name:
                stop_names[sid] = name
                if lat and lon:
                    stop_coords[sid] = (lat, lon)
    logger.info(f"  {len(stop_names)} stops")

    # Build shapes: one deduplicated polyline per shape_id start/end pair.
    # Colour is no longer used (map lines are grey); we just need the coordinates.
    shape_points = {}
    with zf.open("shapes.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid = row.get("shape_id","").strip()
            if sid not in rail_shape_ids:
                continue
            try:
                shape_points.setdefault(sid, []).append((
                    int(row["shape_pt_sequence"]),
                    float(row["shape_pt_lat"]),
                    float(row["shape_pt_lon"]),
                ))
            except (ValueError, KeyError):
                continue

    # Deduplicate: per canonical start/end pair keep the shortest shape
    sig_map = {}
    for sid, pts in shape_points.items():
        pts.sort(key=lambda x: x[0])
        coords = [[lo, la] for _, la, lo in pts]
        if len(coords) < 2:
            continue
        c = coords
        sig = min(
            (round(c[0][1],3), round(c[0][0],3), round(c[-1][1],3), round(c[-1][0],3)),
            (round(c[-1][1],3), round(c[-1][0],3), round(c[0][1],3), round(c[0][0],3))
        )
        n = len(coords)
        if sig not in sig_map or n < sig_map[sig][1]:
            sig_map[sig] = (coords, n)

    shapes = sorted(
        [{"coords": v[0], "shape_len": v[1]} for v in sig_map.values()],
        key=lambda x: -x["shape_len"]
    )
    logger.info(f"  {len(shapes)} deduplicated track segments")
    return shapes, stop_names, stop_coords, trip_headsigns, route_names, route_colors, rail_route_ids


def gtfs_loader_thread():
    while True:
        try:
            shapes, stop_names, stop_coords, trip_headsigns, route_names, route_colors, rail_ids = load_gtfs_static()
            with cache_lock:
                cache.update({
                    "shapes": shapes, "stop_names": stop_names, "stop_coords": stop_coords,
                    "trip_headsigns": trip_headsigns, "route_names": route_names,
                    "route_colors": route_colors, "rail_route_ids": rail_ids,
                    "shapes_loaded": True, "shapes_updated": time.time(),
                })
            logger.info("GTFS static data loaded")
        except Exception as e:
            logger.error(f"GTFS load failed: {e}\n{traceback.format_exc()}")
        time.sleep(SHAPES_REFRESH_HOURS * 3600)


# ── GTFS-RT helpers ───────────────────────────────────────────────────────────

def fetch_feed(url):
    resp = requests.get(url, timeout=15, headers=HEADERS)
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def safe_enum(descriptor, value, fallback="UNKNOWN"):
    try:
        return descriptor.Name(value)
    except Exception:
        return fallback


def calc_bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def parse_vehicles(feed, stop_names, trip_headsigns):
    vehicles = []
    skipped  = 0
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        try:
            v, pos = entity.vehicle, entity.vehicle.position
            if pos.latitude == 0.0 and pos.longitude == 0.0:
                skipped += 1
                continue

            vid      = entity.id
            route_id = v.trip.route_id
            trip_id  = v.trip.trip_id

            # Bearing: use feed value, or calculate from movement history
            bearing = pos.bearing or 0
            with position_history_lock:
                prev = position_history.get(vid)
                if prev and abs(pos.latitude-prev[0]) + abs(pos.longitude-prev[1]) > 0.0001:
                    bearing = bearing or calc_bearing(prev[0], prev[1], pos.latitude, pos.longitude)
                position_history[vid] = (pos.latitude, pos.longitude)

            vehicles.append({
                "id":                    vid,
                "trip_id":               trip_id,
                "route_id":              route_id,
                "lat":                   pos.latitude,
                "lon":                   pos.longitude,
                "bearing":               round(bearing, 1),
                "speed":                 round(pos.speed * 3.6, 1) if pos.speed else None,
                "label":                 v.vehicle.label or vid,
                "current_stop_sequence": v.current_stop_sequence,
                "current_stop_id":       v.stop_id or None,
                "current_stop_name":     strip_platform(stop_names.get(v.stop_id, "")) if v.stop_id else None,
                "headsign":              trip_headsigns.get(trip_id),
                "current_status":        safe_enum(gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus, v.current_status, "IN_TRANSIT_TO"),
                "colour":                line_colour(route_id),
                "line_name":             line_name(route_id),
            })
        except Exception as e:
            logger.error(f"Vehicle {entity.id}: {e}")
    logger.info(f"  Vehicles: {len(vehicles)} parsed, {skipped} skipped (zero pos)")
    return vehicles


def parse_trip_updates(feed, stop_names):
    updates = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        try:
            tu = entity.trip_update
            updates[tu.trip.trip_id] = {
                "trip_id":  tu.trip.trip_id,
                "route_id": tu.trip.route_id,
                "stop_time_updates": [{
                    "stop_sequence":   s.stop_sequence,
                    "stop_id":         s.stop_id,
                    "stop_name":       strip_platform(stop_names.get(s.stop_id, s.stop_id)),
                    "arrival_delay":   s.arrival.delay   if s.HasField("arrival")   else None,
                    "departure_delay": s.departure.delay if s.HasField("departure") else None,
                    "arrival_time":    s.arrival.time    if s.HasField("arrival")   else None,
                    "departure_time":  s.departure.time  if s.HasField("departure") else None,
                } for s in tu.stop_time_update],
            }
        except Exception as e:
            logger.error(f"TripUpdate {entity.id}: {e}")
    return updates


def parse_alerts(feed, rail_route_ids):
    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        try:
            a = entity.alert
            if not a.informed_entity:
                continue

            # Only keep rail alerts
            is_rail = any(
                (getattr(ie, 'route_type', None) == 2) or
                (ie.route_id and ie.route_id in rail_route_ids)
                for ie in a.informed_entity
            )
            if not is_rail:
                continue

            def txt(field):
                t = next((t.text for t in field.translation if t.language == "en"), "")
                return t or (field.translation[0].text if field.translation else "")

            alerts.append({
                "id":          entity.id,
                "header":      txt(a.header_text),
                "description": txt(a.description_text),
                "cause":       safe_enum(gtfs_realtime_pb2.Alert.Cause,  a.cause,  "UNKNOWN_CAUSE"),
                "effect":      safe_enum(gtfs_realtime_pb2.Alert.Effect, a.effect, "UNKNOWN_EFFECT"),
                "routes":      list({ie.route_id for ie in a.informed_entity if ie.route_id}),
            })
        except Exception as e:
            logger.error(f"Alert {entity.id}: {e}")
    return alerts


# ── Stopped-train alert logic ────────────────────────────────────────────────

def update_stopped_alerts(vehicles, trip_updates, now):
    """
    Adds 'stopped_alert' field to each vehicle dict when:
      1. Train is still at its first scheduled stop after ALERT_SECS past departure time.
      2. Train GPS hasn't moved for ALERT_SECS while in transit between stations.
      3. Train is dwelling at a mid-route station for longer than ALERT_SECS.
    """
    with st_lock:
        seen = set()
        for v in vehicles:
            vid    = v["id"]
            seen.add(vid)
            status = v.get("current_status", "")
            lat, lon = v.get("lat", 0), v.get("lon", 0)
            stop_id  = v.get("current_stop_id") or ""
            tu       = trip_updates.get(v.get("trip_id",""), {})
            all_stops = sorted(tu.get("stop_time_updates", []), key=lambda s: s.get("stop_sequence", 0))

            st = stopped_tracker.setdefault(vid, {
                "last_lat": lat, "last_lon": lon, "last_gps_time": now,
                "dwell_start": None, "dwell_stop_id": None,
                "origin_depart_time": None, "origin_stop_id": None,
            })

            alert = None

            # ── 1. GPS staleness check (in-transit only) ─────────────────────
            moved = abs(lat - st["last_lat"]) + abs(lon - st["last_lon"]) > 0.0002
            if moved:
                st["last_lat"] = lat
                st["last_lon"] = lon
                st["last_gps_time"] = now
            elif status != "STOPPED_AT":
                gps_age = now - st["last_gps_time"]
                if gps_age >= ALERT_SECS:
                    mins = int(gps_age // 60)
                    alert = f"GPS not updated for {mins}m"

            # ── 2. Dwell at mid-route station ────────────────────────────────
            if status == "STOPPED_AT" and stop_id and all_stops:
                # Is this the origin (first stop)?
                first_seq = all_stops[0].get("stop_sequence", 0) if all_stops else 0
                cur_seq   = v.get("current_stop_sequence", 0)
                is_origin = (cur_seq <= first_seq)

                if not is_origin:
                    if st["dwell_stop_id"] != stop_id:
                        st["dwell_start"]   = now
                        st["dwell_stop_id"] = stop_id
                    elif st["dwell_start"] and (now - st["dwell_start"]) >= ALERT_SECS:
                        mins = int((now - st["dwell_start"]) // 60)
                        alert = alert or f"Stopped at {v.get('current_stop_name') or stop_id} for {mins}m"
                else:
                    # Reset dwell if we moved to a new stop
                    if st["dwell_stop_id"] != stop_id:
                        st["dwell_stop_id"] = stop_id
                        st["dwell_start"]   = None

            elif status != "STOPPED_AT":
                # Cleared the station — reset dwell
                st["dwell_start"]   = None
                st["dwell_stop_id"] = None

            # ── 3. Still at origin past scheduled departure ──────────────────
            if status == "STOPPED_AT" and all_stops:
                first_stop = all_stops[0]
                first_seq  = first_stop.get("stop_sequence", 0)
                cur_seq    = v.get("current_stop_sequence", 0)
                if cur_seq <= first_seq:
                    dep_time = first_stop.get("departure_time")
                    if dep_time and dep_time > 0:
                        overdue = now - dep_time
                        if overdue >= ALERT_SECS:
                            mins = int(overdue // 60)
                            alert = alert or f"Held at origin for {mins}m past departure"

            v["stopped_alert"] = alert

        # Purge stale vehicles
        for vid in list(stopped_tracker.keys()):
            if vid not in seen:
                del stopped_tracker[vid]


# ── Poll loop ─────────────────────────────────────────────────────────────────

def poll_feeds():
    logger.info("Poll thread started")
    while True:
        try:
            veh_feed = fetch_feed(VEHICLE_POSITIONS_URL)
            tu_feed  = fetch_feed(TRIP_UPDATES_URL)
            al_feed  = fetch_feed(ALERTS_URL)

            with cache_lock:
                stop_names     = cache["stop_names"]
                trip_headsigns = cache["trip_headsigns"]
                rail_route_ids = cache["rail_route_ids"]

            vehicles     = parse_vehicles(veh_feed, stop_names, trip_headsigns)
            trip_updates = parse_trip_updates(tu_feed, stop_names)
            alerts       = parse_alerts(al_feed, rail_route_ids)
            update_stopped_alerts(vehicles, trip_updates, time.time())

            with cache_lock:
                cache["vehicles"]     = vehicles
                cache["trip_updates"] = trip_updates
                cache["alerts"]       = alerts
                cache["last_updated"] = time.time()
                cache["error"]        = None
            logger.info(f"Poll: {len(vehicles)} vehicles, {len(trip_updates)} trips, {len(alerts)} alerts")

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error: {e}")
            with cache_lock: cache["error"] = str(e)
        except Exception as e:
            logger.error(f"Poll error: {e}\n{traceback.format_exc()}")
            with cache_lock: cache["error"] = str(e)

        time.sleep(POLL_INTERVAL)


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/vehicles")
def api_vehicles():
    with cache_lock:
        vehicles     = list(cache["vehicles"])
        trip_updates = dict(cache["trip_updates"])
        last_updated = cache["last_updated"]
        error        = cache["error"]

    for v in vehicles:
        tu          = trip_updates.get(v["trip_id"], {})
        all_stops   = sorted(tu.get("stop_time_updates", []), key=lambda s: s.get("stop_sequence", 0))
        current_seq = v.get("current_stop_sequence", 0)
        cutoff      = current_seq + 1 if v.get("current_status") == "STOPPED_AT" else current_seq
        upcoming    = [s for s in all_stops if s.get("stop_sequence", 0) >= cutoff] or all_stops
        v["delay_seconds"] = next((s["arrival_delay"] for s in upcoming if s.get("arrival_delay") is not None), 0)
        v["next_stops"]    = upcoming[:5]

    return jsonify({"vehicles": vehicles, "count": len(vehicles),
                    "last_updated": last_updated, "poll_interval": POLL_INTERVAL, "error": error})


@app.route("/api/shapes")
def api_shapes():
    with cache_lock:
        shapes = cache["shapes"]
        loaded = cache["shapes_loaded"]
        updated= cache["shapes_updated"]
    return jsonify({
        "type": "FeatureCollection",
        "loaded": loaded, "shapes_updated": updated, "segment_count": len(shapes),
        "features": [{"type":"Feature",
                      "geometry":{"type":"LineString","coordinates":s["coords"]},
                      "properties":{}}
                     for s in shapes],
    })


@app.route("/api/stations")
def api_stations():
    with cache_lock:
        vehicles     = list(cache["vehicles"])
        trip_updates = dict(cache["trip_updates"])
        stop_names   = dict(cache["stop_names"])
        stop_coords  = dict(cache.get("stop_coords", {}))
        last_updated = cache["last_updated"]

    now         = time.time()
    veh_by_trip = {v["trip_id"]: v for v in vehicles if v.get("trip_id")}
    arrivals    = {}  # stop_id -> [arrival dicts]

    for trip_id, tu in trip_updates.items():
        veh         = veh_by_trip.get(trip_id, {})
        route_id    = tu.get("route_id", "")
        headsign    = veh.get("headsign", "")
        current_seq = veh.get("current_stop_sequence", 0)

        for s in sorted(tu.get("stop_time_updates", []), key=lambda x: x.get("stop_sequence", 0)):
            if s.get("stop_sequence", 0) < current_seq:
                continue
            sid       = s.get("stop_id")
            arr_time  = s.get("arrival_time")
            arr_delay = s.get("arrival_delay") or 0
            if not sid or not arr_time or arr_time < now - 30:
                continue
            dep_time = s.get("departure_time")
            dep_delay= s.get("departure_delay") or arr_delay
            raw_name = stop_names.get(sid, "")
            plat_m   = re.search(r',?\s*[Pp]latform\s*(\d+)\s*$', raw_name)
            arrivals.setdefault(sid, []).append({
                "trip_id":        trip_id,
                "route_id":       route_id,
                "headsign":       headsign,
                "arrival_time":   arr_time + arr_delay,
                "departure_time": (dep_time + dep_delay) if dep_time else (arr_time + arr_delay),
                "delay":          arr_delay,
                "stop_sequence":  s.get("stop_sequence", 0),
                "platform":       f"Platform {plat_m.group(1)}" if plat_m else None,
            })

    for sid in arrivals:
        arrivals[sid].sort(key=lambda x: x["arrival_time"])
        arrivals[sid] = arrivals[sid][:6]

    # Group platform stops into single station entries
    groups = {}
    for sid, trains in arrivals.items():
        coords = stop_coords.get(sid)
        if not coords:
            continue
        raw_name   = stop_names.get(sid, sid)
        plat_match = re.search(r',?\s*[Pp]latform\s*(\d+)\s*$', raw_name)
        plat_label = f"Platform {plat_match.group(1)}" if plat_match else None
        clean_name = re.sub(r',?\s*[Pp]latform\s*\d+\s*$', '', raw_name).strip()

        if clean_name not in groups:
            groups[clean_name] = {"lat": coords[0], "lon": coords[1], "platforms": {}, "all_arrivals": []}
        key = plat_label or "All platforms"
        groups[clean_name]["platforms"].setdefault(key, []).extend(trains)
        groups[clean_name]["all_arrivals"].extend(trains)

    result = []
    for name, data in groups.items():
        data["all_arrivals"].sort(key=lambda x: x["arrival_time"])
        for plat in data["platforms"]:
            data["platforms"][plat].sort(key=lambda x: x["arrival_time"])
            data["platforms"][plat] = data["platforms"][plat][:6]
        result.append({"stop_name": name, "lat": data["lat"], "lon": data["lon"],
                        "platforms": data["platforms"], "arrivals": data["all_arrivals"][:8]})

    return jsonify({"stations": result, "count": len(result), "last_updated": last_updated})


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


@app.route("/api/debug/live")
def api_debug_live():
    """Live route_ids with colour assignments — useful for debugging."""
    with cache_lock:
        vehicles    = list(cache["vehicles"])
        route_colors= dict(cache.get("route_colors", {}))
    live = {}
    for v in vehicles:
        rid = v.get("route_id", "")
        if rid not in live:
            live[rid] = {"route_id": rid, "count": 0,
                         "example_headsign": v.get("headsign"),
                         "gtfs_colour": route_colors.get(rid, ""),
                         "computed_colour": line_colour(rid)}
        live[rid]["count"] += 1
    return jsonify({"live_route_ids": sorted(live.values(), key=lambda x: -x["count"]),
                    "total_vehicles": len(vehicles)})


@app.route("/api/debug/routes")
def api_debug_routes():
    with cache_lock:
        route_names  = dict(cache.get("route_names", {}))
        route_colors = dict(cache.get("route_colors", {}))
    return jsonify({"gtfs_routes": [
        {"route_id": rid, "name": route_names[rid],
         "gtfs_color": route_colors.get(rid,""), "computed_colour": line_colour(rid)}
        for rid in sorted(route_names)
    ][:40]})


@app.route("/api/logs")
def api_logs():
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-200:], "total_lines": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Start ─────────────────────────────────────────────────────────────────────
threading.Thread(target=gtfs_loader_thread, daemon=True).start()
threading.Thread(target=poll_feeds,         daemon=True).start()
logger.info(f"Backend started (pid={os.getpid()})")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
