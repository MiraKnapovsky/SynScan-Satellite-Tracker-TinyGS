#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt


RSSI_SNR_RE = re.compile(r"RSSI/SNR:\s*([+-]?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)dB", re.IGNORECASE)
FREQ_ERROR_RE = re.compile(r"Freq error:\s*([+-]?\d+(?:\.\d+)?)Hz", re.IGNORECASE)
NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
BASE_DIR = Path(__file__).resolve().parent


def make_client(client_id: str) -> mqtt.Client:
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)


def atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def ensure_parent_dir(path: Optional[str]) -> None:
    if not path:
        return
    parent = Path(path).parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)


def parse_topic_parts(topic: str) -> Dict[str, Optional[str]]:
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "tinygs":
        return {"user": None, "station": None, "channel": None, "cmd": None, "subcmd": None}
    return {
        "user": parts[1] if len(parts) > 1 else None,
        "station": parts[2] if len(parts) > 2 else None,
        "channel": parts[3] if len(parts) > 3 else None,
        "cmd": parts[4] if len(parts) > 4 else None,
        "subcmd": parts[5] if len(parts) > 5 else None,
    }


def extract_frame_satellite(frame_obj: Any) -> Optional[str]:
    if not isinstance(frame_obj, list):
        return None
    for row in frame_obj:
        if not isinstance(row, list) or len(row) < 5:
            continue
        if row[0] == 1 and row[1] == 0 and row[2] == 0 and row[3] == 0:
            name = str(row[4]).strip()
            if name and name.upper() != "UNKNOWN":
                return name
            return None
    return None


def extract_frame_metrics(frame_obj: Any) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "rssi_db": None,
        "snr_db": None,
        "freq_error_hz": None,
        "decode_status": "unknown",
        "confirmed": False,
        "crc_error": False,
    }
    if not isinstance(frame_obj, list):
        return metrics

    for row in frame_obj:
        if not isinstance(row, list) or len(row) < 5:
            continue
        text = str(row[4]).strip()

        m_rssi = RSSI_SNR_RE.search(text)
        if m_rssi:
            metrics["rssi_db"] = float(m_rssi.group(1))
            metrics["snr_db"] = float(m_rssi.group(2))
            continue

        m_freq = FREQ_ERROR_RE.search(text)
        if m_freq:
            metrics["freq_error_hz"] = float(m_freq.group(1))
            continue

        upper = text.upper()
        if "CRC ERROR" in upper:
            metrics["decode_status"] = "crc_error"
            metrics["crc_error"] = True
        elif "CONFIRMED" in upper:
            metrics["decode_status"] = "confirmed"
            metrics["confirmed"] = True

    return metrics


def normalize_influx_key(name: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z_]", "_", name.strip()).strip("_").lower()
    return normalized or "value"


def extract_state_fields_for_influx(
    state_obj: Dict[str, Any],
    gateway_lat: Optional[float] = None,
    gateway_lon: Optional[float] = None,
    gateway_alt_m: Optional[float] = None,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    skip = {"sat", "mode", "last_update"}

    for key, value in state_obj.items():
        if key in skip or value is None:
            continue

        field_key = normalize_influx_key(key)
        if isinstance(value, bool):
            fields[field_key] = value
        elif isinstance(value, (int, float)):
            # Influx field type is fixed per key, so normalize all numerics to float.
            fields[field_key] = float(value)

    if gateway_lat is not None:
        fields["station_lat_deg"] = float(gateway_lat)
    if gateway_lon is not None:
        fields["station_lon_deg"] = float(gateway_lon)
    if gateway_alt_m is not None:
        fields["station_alt_m"] = float(gateway_alt_m)

    return fields


def parse_env_float(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] Ignoring invalid float in env {name}: {raw!r}")
        return None


def normalize_sat_name(name: str) -> str:
    text = (name or "").strip().lower().replace("_", "-")
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-").replace("\u2013", "-")
    text = text.replace("\u2014", "-").replace("\u2015", "-")
    text = NAME_NORMALIZE_RE.sub("-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def sat_dedupe_key(name: str) -> str:
    normalized = normalize_sat_name(name)
    if normalized:
        return normalized
    return name.strip().casefold()


def load_confirmed_catalog(path: str) -> Dict[str, str]:
    catalog: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return catalog

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] Failed to read confirmed catalog {path}: {e}")
        return catalog

    satellites: List[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("satellites"), list):
        satellites = [str(x) for x in raw["satellites"] if isinstance(x, str)]
    elif isinstance(raw, list):
        satellites = [str(x) for x in raw if isinstance(x, str)]

    for sat in satellites:
        name = sat.strip()
        if not name:
            continue
        key = sat_dedupe_key(name)
        if key and key not in catalog:
            catalog[key] = name

    return catalog


def save_confirmed_catalog(path: str, catalog: Dict[str, str]) -> None:
    names = sorted(catalog.values(), key=lambda x: x.casefold())
    atomic_write_json(
        path,
        {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "count": len(names),
            "satellites": names,
        },
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_km * c


def load_gateway_from_config(path: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if not path:
        return None, None, None
    cfg = Path(path)
    if not cfg.exists():
        return None, None, None
    try:
        obj = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None
    lat = obj.get("lat")
    lon = obj.get("lon")
    alt = obj.get("alt")
    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
        alt_f = float(alt) if alt is not None else None
    except (TypeError, ValueError):
        return None, None, None
    return lat_f, lon_f, alt_f


class SatelliteLocator:
    def __init__(self, tle_file: str, gateway_lat: float, gateway_lon: float, gateway_alt_m: float) -> None:
        from skyfield.api import EarthSatellite, load, wgs84

        self._EarthSatellite = EarthSatellite
        self._load = load
        self._wgs84 = wgs84
        self.tle_path = Path(tle_file)
        self.gateway_lat = gateway_lat
        self.gateway_lon = gateway_lon
        self.gateway_alt_m = gateway_alt_m

        try:
            self.ts = load.timescale(builtin=True)
        except TypeError:
            self.ts = load.timescale()

        self.observer = wgs84.latlon(gateway_lat, gateway_lon, elevation_m=gateway_alt_m)
        self.satellites: List[Any] = []
        self.by_exact: Dict[str, Any] = {}
        self.by_norm: Dict[str, Any] = {}
        self.tle_mtime_ns: Optional[int] = None
        self.reload(force=True)

    def _parse_tle_file(self) -> List[Any]:
        if not self.tle_path.exists():
            raise FileNotFoundError(f"TLE file not found: {self.tle_path}")

        lines = [line.strip() for line in self.tle_path.read_text(encoding="utf-8", errors="ignore").splitlines()]
        sats: List[Any] = []
        i = 0
        while i + 2 < len(lines):
            name = lines[i]
            l1 = lines[i + 1]
            l2 = lines[i + 2]
            if l1.startswith("1 ") and l2.startswith("2 "):
                try:
                    sats.append(self._EarthSatellite(l1, l2, name=name, ts=self.ts))
                except Exception:
                    pass
                i += 3
            else:
                i += 1
        return sats

    def reload(self, force: bool = False) -> None:
        st = self.tle_path.stat()
        if not force and self.tle_mtime_ns == st.st_mtime_ns:
            return

        sats = self._parse_tle_file()
        by_exact: Dict[str, Any] = {}
        by_norm: Dict[str, Any] = {}
        for sat in sats:
            name = str(getattr(sat, "name", "") or "").strip()
            if not name:
                continue
            by_exact.setdefault(name.casefold(), sat)
            norm = normalize_sat_name(name)
            if norm:
                by_norm.setdefault(norm, sat)

        self.satellites = sats
        self.by_exact = by_exact
        self.by_norm = by_norm
        self.tle_mtime_ns = st.st_mtime_ns

    def find_satellite(self, sat_name: str) -> Optional[Any]:
        if not sat_name:
            return None

        try:
            self.reload(force=False)
        except OSError:
            return None

        exact = sat_name.strip().casefold()
        if exact in self.by_exact:
            return self.by_exact[exact]

        norm = normalize_sat_name(sat_name)
        if norm in self.by_norm:
            return self.by_norm[norm]

        if norm:
            collapsed = norm.replace("-", "")
            for key, sat in self.by_norm.items():
                if norm in key or key in norm:
                    return sat
                if collapsed and (collapsed in key.replace("-", "") or key.replace("-", "") in collapsed):
                    return sat
        return None

    def locate(self, sat_name: str, when_dt: datetime) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tle_found": False}
        sat = self.find_satellite(sat_name)
        if sat is None:
            return out

        t = self.ts.from_datetime(when_dt)
        geocentric = sat.at(t)
        subpoint = self._wgs84.subpoint(geocentric)
        topocentric = (sat - self.observer).at(t)
        alt, az, distance = topocentric.altaz()
        sat_lat = float(subpoint.latitude.degrees)
        sat_lon = float(subpoint.longitude.degrees)

        out.update(
            {
                "tle_found": True,
                "tle_satellite": str(getattr(sat, "name", "") or "").strip(),
                "sat_lat_deg": sat_lat,
                "sat_lon_deg": sat_lon,
                "sat_alt_km": float(subpoint.elevation.km),
                "sat_az_deg": float(az.degrees),
                "sat_el_deg": float(alt.degrees),
                "slant_range_km": float(distance.km),
                "ground_distance_km": haversine_km(self.gateway_lat, self.gateway_lon, sat_lat, sat_lon),
            }
        )
        return out


def main() -> None:
    ap = argparse.ArgumentParser(description="TinyGS MQTT listener (state + frame)")
    ap.add_argument("--host", default="mqtt.tinygs.com")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", default=os.getenv("TINYGS_PASS"))
    ap.add_argument("--cafile", default=os.getenv("TINYGS_CAFILE"), help="Optional path to CA bundle PEM")
    ap.add_argument("--station", default="KNA0047Rotator")
    ap.add_argument("--state-topic", default=None, help="Override state topic (default: tinygs/<user>/<station>/cmnd/begine)")
    ap.add_argument("--frame-topic", default=None, help="Override frame topic (default: tinygs/<user>/<station>/cmnd/frame/0)")
    ap.add_argument("--out", default=str(BASE_DIR / "state.json"), help="File with latest begin config")
    ap.add_argument("--rx-out", default=None, help="JSONL output for frame packets")
    ap.add_argument("--raw-dir", default=None, help="Optional dir for raw JSONL mirror")
    ap.add_argument("--influx-url", default=os.getenv("INFLUXDB_URL"), help="InfluxDB URL, e.g. http://127.0.0.1:8086")
    ap.add_argument("--influx-org", default=os.getenv("INFLUXDB_ORG"), help="InfluxDB organization")
    ap.add_argument("--influx-bucket", default=os.getenv("INFLUXDB_BUCKET"), help="InfluxDB bucket")
    ap.add_argument("--influx-token", default=os.getenv("INFLUXDB_TOKEN"), help="InfluxDB API token")
    ap.add_argument("--influx-measurement-frame", default=os.getenv("INFLUXDB_MEAS_FRAME", "tinygs_frame"))
    ap.add_argument("--influx-measurement-state", default=os.getenv("INFLUXDB_MEAS_STATE", "tinygs_state"))
    ap.add_argument("--influx-measurement-meta", default=os.getenv("INFLUXDB_MEAS_META", "tinygs_meta"))
    ap.add_argument("--influx-timeout-ms", type=int, default=int(os.getenv("INFLUXDB_TIMEOUT_MS", "10000")))
    ap.add_argument(
        "--confirmed-catalog-out",
        default=os.getenv("TINYGS_CONFIRMED_CATALOG_OUT", str(BASE_DIR / "confirmed_satellites.json")),
        help="Persistent JSON list of unique satellites seen with CONFIRMED decode",
    )
    ap.add_argument(
        "--frame-dedupe-window-s",
        type=float,
        default=float(os.getenv("TINYGS_FRAME_DEDUPE_WINDOW_S", "5")),
        help="Drop duplicate frame packets from same satellite received within this many seconds (0 disables)",
    )
    ap.add_argument(
        "--max-slant-range-km",
        type=float,
        default=float(os.getenv("TINYGS_MAX_SLANT_RANGE_KM", "5000")),
        help="Drop frame packets with computed slant range above this value (0 disables)",
    )
    ap.add_argument("--tle-file", default=os.getenv("TINYGS_TLE_FILE", str(BASE_DIR / "satellites.tle")))
    ap.add_argument(
        "--gateway-config",
        default=os.getenv("TINYGS_GATEWAY_CONFIG", str(BASE_DIR / "synscan_config.json")),
        help="JSON config fallback with keys lat/lon/alt",
    )
    ap.add_argument("--gateway-lat", type=float, default=parse_env_float("TINYGS_GATEWAY_LAT"))
    ap.add_argument("--gateway-lon", type=float, default=parse_env_float("TINYGS_GATEWAY_LON"))
    ap.add_argument("--gateway-alt-m", type=float, default=parse_env_float("TINYGS_GATEWAY_ALT_M"))
    ap.add_argument("--disable-geo", action="store_true", help="Disable TLE-based geo enrichment")
    args = ap.parse_args()

    if not args.password:
        raise SystemExit("Chybi heslo: dej --password nebo exportuj TINYGS_PASS")
    if args.frame_dedupe_window_s < 0:
        raise SystemExit("--frame-dedupe-window-s must be >= 0")
    if args.max_slant_range_km < 0:
        raise SystemExit("--max-slant-range-km must be >= 0")

    state_topic = args.state_topic or f"tinygs/{args.user}/{args.station}/cmnd/begine"
    frame_topic = args.frame_topic or f"tinygs/{args.user}/{args.station}/cmnd/frame/0"
    topics = [state_topic, frame_topic]

    ensure_parent_dir(args.out)
    ensure_parent_dir(args.rx_out)
    ensure_parent_dir(args.confirmed_catalog_out)
    if args.raw_dir:
        Path(args.raw_dir).mkdir(parents=True, exist_ok=True)

    confirmed_catalog = load_confirmed_catalog(args.confirmed_catalog_out)
    print(
        f"[CATALOG] Loaded unique CONFIRMED satellites: {len(confirmed_catalog)} "
        f"({args.confirmed_catalog_out})"
    )

    gateway_lat = args.gateway_lat
    gateway_lon = args.gateway_lon
    gateway_alt_m = args.gateway_alt_m
    cfg_lat, cfg_lon, cfg_alt = load_gateway_from_config(args.gateway_config)
    if gateway_lat is None:
        gateway_lat = cfg_lat
    if gateway_lon is None:
        gateway_lon = cfg_lon
    if gateway_alt_m is None:
        gateway_alt_m = cfg_alt if cfg_alt is not None else 0.0

    sat_locator: Optional[SatelliteLocator] = None
    geo_enabled = False
    if not args.disable_geo:
        if gateway_lat is None or gateway_lon is None:
            print(
                "[GEO] Disabled: missing gateway lat/lon. Set --gateway-lat/--gateway-lon "
                "or define lat/lon in gateway config."
            )
        else:
            try:
                sat_locator = SatelliteLocator(
                    tle_file=args.tle_file,
                    gateway_lat=float(gateway_lat),
                    gateway_lon=float(gateway_lon),
                    gateway_alt_m=float(gateway_alt_m),
                )
                geo_enabled = True
                print(
                    f"[GEO] Enabled: tle={args.tle_file}, gateway=({gateway_lat:.6f}, {gateway_lon:.6f}, "
                    f"alt={gateway_alt_m:.1f}m), satellites={len(sat_locator.satellites)}"
                )
            except ImportError as e:
                print(f"[GEO] Disabled: missing dependency skyfield ({e})")
            except Exception as e:
                print(f"[GEO] Disabled: failed to initialize TLE data: {e}")
    else:
        print("[GEO] Disabled by --disable-geo")

    influx_client = None
    influx_write_api = None
    influx_point_cls = None
    influx_cfg = [args.influx_url, args.influx_org, args.influx_bucket, args.influx_token]
    if any(influx_cfg):
        if not all(influx_cfg):
            raise SystemExit("Pro InfluxDB je potreba zadat: --influx-url, --influx-org, --influx-bucket, --influx-token")
        try:
            from influxdb_client import InfluxDBClient, Point
            from influxdb_client.client.write_api import SYNCHRONOUS
        except ImportError as e:
            raise SystemExit(f"Chybi balicek influxdb-client: {e}") from e

        influx_client = InfluxDBClient(
            url=args.influx_url,
            token=args.influx_token,
            org=args.influx_org,
            timeout=args.influx_timeout_ms,
        )
        influx_write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        influx_point_cls = Point

    def write_influx(record: Any) -> None:
        if not influx_write_api:
            return
        try:
            influx_write_api.write(bucket=args.influx_bucket, org=args.influx_org, record=record)
        except Exception as e:
            print(f"[WARN] InfluxDB write failed: {e}")

    def write_confirmed_catalog_meta(now_dt: Optional[datetime] = None) -> None:
        if not influx_point_cls:
            return
        point_time = now_dt or datetime.now(timezone.utc)
        point = influx_point_cls(args.influx_measurement_meta).time(point_time)
        point = point.tag("user", str(args.user))
        point = point.tag("station", str(args.station))
        point = point.field("unique_confirmed_all", float(len(confirmed_catalog)))
        write_influx(point)

    def bootstrap_confirmed_catalog_from_influx() -> int:
        if not influx_client:
            return 0
        if confirmed_catalog:
            return 0
        try:
            query_api = influx_client.query_api()
            flux = f'''
from(bucket: "{args.influx_bucket}")
  |> range(start: 0, stop: now())
  |> filter(fn: (r) => r._measurement == "{args.influx_measurement_frame}")
  |> filter(fn: (r) => r._field == "confirmed")
  |> filter(fn: (r) => r.decode_status == "confirmed")
  |> filter(fn: (r) => r._value == true)
  |> filter(fn: (r) => exists r.satellite)
  |> group(columns: ["satellite"])
  |> first(column: "_value")
  |> keep(columns: ["satellite"])
'''
            tables = query_api.query(org=args.influx_org, query=flux)
        except Exception as e:
            print(f"[WARN] Failed to bootstrap confirmed catalog from InfluxDB: {e}")
            return 0

        added = 0
        for table in tables:
            for record in table.records:
                sat = str(record.values.get("satellite") or "").strip()
                if not sat:
                    continue
                key = sat_dedupe_key(sat)
                if key and key not in confirmed_catalog:
                    confirmed_catalog[key] = sat
                    added += 1
        return added

    boot_added = bootstrap_confirmed_catalog_from_influx()
    if boot_added > 0:
        try:
            save_confirmed_catalog(args.confirmed_catalog_out, confirmed_catalog)
            print(
                f"[CATALOG] Bootstrapped from InfluxDB: +{boot_added}, "
                f"total={len(confirmed_catalog)}"
            )
        except OSError as e:
            print(f"[WARN] Failed to write confirmed catalog bootstrap: {e}")

    write_confirmed_catalog_meta()

    frame_last_seen_ts_by_sat: Dict[str, float] = {}

    client = make_client(client_id=f"tinygs-json-{int(time.time())}")
    client.username_pw_set(args.user, args.password)
    if args.cafile:
        tls_ctx = ssl.create_default_context()
        tls_ctx.load_verify_locations(cafile=args.cafile)
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            tls_ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        client.tls_set_context(tls_ctx)
    else:
        client.tls_set()
    client.tls_insecure_set(False)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        rc = getattr(reason_code, "value", reason_code)
        if rc != 0:
            print(f"[MQTT] Connect failed, reason_code={rc}")
            return
        print(f"[MQTT] Connected. Subscribing: {', '.join(topics)}")
        for t in topics:
            client.subscribe(t, qos=0)

    def on_message(client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        now_dt = datetime.now(timezone.utc)
        now_utc = now_dt.isoformat()
        topic_info = parse_topic_parts(msg.topic)
        frame_filtered = False

        obj: Any = None
        json_ok = False
        try:
            obj = json.loads(payload)
            json_ok = True
            print(json.dumps(obj, ensure_ascii=False, sort_keys=True))
        except json.JSONDecodeError:
            print(f"[{msg.topic}] (non-JSON) {payload}")

        if json_ok and msg.topic == state_topic and isinstance(obj, dict):
            state_obj = dict(obj)
            state_obj["last_update"] = datetime.now().strftime("%H:%M:%S")
            try:
                atomic_write_json(args.out, state_obj)
            except OSError as e:
                print(f"[WARN] Nepodarilo se zapsat {args.out}: {e}")

            state_sat_name = str(obj.get("sat") or "").strip()
            state_geo_info: Dict[str, Any] = {"tle_found": False}
            if sat_locator and state_sat_name:
                try:
                    state_geo_info = sat_locator.locate(state_sat_name, now_dt)
                except Exception as e:
                    print(f"[WARN] GEO locate failed for state satellite {state_sat_name!r}: {e}")

            if influx_point_cls:
                # Reload gateway config on every state packet so lat/lon changes
                # in synscan_config.json are reflected without restarting this process.
                curr_lat = gateway_lat
                curr_lon = gateway_lon
                curr_alt_m = gateway_alt_m
                cfg_lat_now, cfg_lon_now, cfg_alt_now = load_gateway_from_config(args.gateway_config)
                if cfg_lat_now is not None:
                    curr_lat = cfg_lat_now
                if cfg_lon_now is not None:
                    curr_lon = cfg_lon_now
                if cfg_alt_now is not None:
                    curr_alt_m = cfg_alt_now

                point = influx_point_cls(args.influx_measurement_state).time(now_dt)
                for key in ("user", "station", "channel", "cmd", "subcmd"):
                    value = topic_info.get(key)
                    if value:
                        point = point.tag(key, str(value))
                if state_sat_name:
                    point = point.tag("satellite", state_sat_name)
                if obj.get("mode"):
                    point = point.tag("mode", str(obj["mode"]))
                point = point.field("state_seen", True)
                point = point.field("geo_enabled", geo_enabled)
                point = point.field("tle_found", bool(state_geo_info.get("tle_found", False)))
                if state_geo_info.get("tle_found"):
                    tle_name = str(state_geo_info.get("tle_satellite") or "").strip()
                    if tle_name:
                        point = point.tag("tle_satellite", tle_name)
                    for key in (
                        "sat_lat_deg",
                        "sat_lon_deg",
                        "sat_alt_km",
                        "sat_az_deg",
                        "sat_el_deg",
                        "slant_range_km",
                        "ground_distance_km",
                    ):
                        if key in state_geo_info:
                            point = point.field(key, float(state_geo_info[key]))
                for field_key, field_value in extract_state_fields_for_influx(
                    obj, gateway_lat=curr_lat, gateway_lon=curr_lon, gateway_alt_m=curr_alt_m
                ).items():
                    point = point.field(field_key, field_value)
                write_influx(point)
                # Keep all-time unique confirmed count fresh in Influx for dashboard stats.
                write_confirmed_catalog_meta(now_dt=now_dt)

        if msg.topic == frame_topic:
            sat_name = extract_frame_satellite(obj if json_ok else None)
            frame_metrics = extract_frame_metrics(obj if json_ok else None)
            if frame_metrics["crc_error"] and not sat_name:
                frame_filtered = True
                print("[FILTER] Dropped frame: crc_error without satellite")

            if (
                not frame_filtered
                and sat_name
                and args.frame_dedupe_window_s > 0
            ):
                key = sat_dedupe_key(sat_name)
                now_ts = time.time()
                prev_ts = frame_last_seen_ts_by_sat.get(key)
                if prev_ts is not None and (now_ts - prev_ts) <= args.frame_dedupe_window_s:
                    frame_filtered = True
                    print(
                        "[FILTER] Dropped frame: duplicate satellite frame "
                        f"(satellite={sat_name}, window={args.frame_dedupe_window_s:.1f}s)"
                    )
                else:
                    frame_last_seen_ts_by_sat[key] = now_ts

            if not frame_filtered:
                geo_info: Dict[str, Any] = {"tle_found": False}
                if sat_locator and sat_name:
                    try:
                        geo_info = sat_locator.locate(sat_name, now_dt)
                    except Exception as e:
                        print(f"[WARN] GEO locate failed for {sat_name!r}: {e}")

                if (
                    not frame_filtered
                    and args.max_slant_range_km > 0
                    and geo_info.get("tle_found")
                ):
                    slant_range_km = geo_info.get("slant_range_km")
                    if isinstance(slant_range_km, (int, float)) and float(slant_range_km) > args.max_slant_range_km:
                        frame_filtered = True
                        sat_label = sat_name or str(geo_info.get("tle_satellite") or "unknown")
                        print(
                            "[FILTER] Dropped frame: slant range above threshold "
                            f"(satellite={sat_label}, slant_range_km={float(slant_range_km):.1f}, "
                            f"max_slant_range_km={args.max_slant_range_km:.1f})"
                        )

            if not frame_filtered and sat_name and frame_metrics["confirmed"]:
                key = sat_dedupe_key(sat_name)
                if key not in confirmed_catalog:
                    confirmed_catalog[key] = sat_name
                    try:
                        save_confirmed_catalog(args.confirmed_catalog_out, confirmed_catalog)
                        print(
                            f"[CATALOG] Added unique CONFIRMED satellite: {sat_name} "
                            f"(total={len(confirmed_catalog)})"
                        )
                    except OSError as e:
                        print(f"[WARN] Failed to update confirmed catalog: {e}")
                    write_confirmed_catalog_meta(now_dt=now_dt)

            if not frame_filtered and sat_name and args.rx_out:
                rx_record: Dict[str, Any] = {
                    "ts_utc": now_utc,
                    "topic": msg.topic,
                    "user": topic_info.get("user"),
                    "station": topic_info.get("station"),
                    "channel": topic_info.get("channel"),
                    "cmd": topic_info.get("cmd"),
                    "subcmd": topic_info.get("subcmd"),
                    "satellite": sat_name,
                    "payload": payload,
                    "json": obj if json_ok else None,
                }
                if geo_info.get("tle_found"):
                    rx_record["tle_satellite"] = geo_info.get("tle_satellite")
                    for key in (
                        "sat_lat_deg",
                        "sat_lon_deg",
                        "sat_alt_km",
                        "sat_az_deg",
                        "sat_el_deg",
                        "slant_range_km",
                        "ground_distance_km",
                    ):
                        if key in geo_info:
                            rx_record[key] = geo_info[key]
                try:
                    append_jsonl(args.rx_out, rx_record)
                except OSError as e:
                    print(f"[WARN] Nepodarilo se zapsat rx data: {e}")

            if not frame_filtered and influx_point_cls:
                point = influx_point_cls(args.influx_measurement_frame).time(now_dt)
                for key in ("user", "station", "channel", "cmd", "subcmd"):
                    value = topic_info.get(key)
                    if value:
                        point = point.tag(key, str(value))
                if sat_name:
                    point = point.tag("satellite", sat_name)
                point = point.tag("decode_status", str(frame_metrics["decode_status"]))
                point = point.field("frame_seen", True)
                point = point.field("has_json", json_ok)
                if frame_metrics["rssi_db"] is not None:
                    point = point.field("rssi_db", float(frame_metrics["rssi_db"]))
                if frame_metrics["snr_db"] is not None:
                    point = point.field("snr_db", float(frame_metrics["snr_db"]))
                if frame_metrics["freq_error_hz"] is not None:
                    point = point.field("freq_error_hz", float(frame_metrics["freq_error_hz"]))
                point = point.field("confirmed", bool(frame_metrics["confirmed"]))
                point = point.field("crc_error", bool(frame_metrics["crc_error"]))
                point = point.field("payload_bytes", float(len(payload.encode("utf-8"))))
                point = point.field("geo_enabled", geo_enabled)
                point = point.field("tle_found", bool(geo_info.get("tle_found", False)))
                if geo_info.get("tle_found"):
                    tle_name = str(geo_info.get("tle_satellite") or "").strip()
                    if tle_name:
                        point = point.tag("tle_satellite", tle_name)
                    for key in (
                        "sat_lat_deg",
                        "sat_lon_deg",
                        "sat_alt_km",
                        "sat_az_deg",
                        "sat_el_deg",
                        "slant_range_km",
                        "ground_distance_km",
                    ):
                        if key in geo_info:
                            point = point.field(key, float(geo_info[key]))
                write_influx(point)

        if args.raw_dir:
            if msg.topic == frame_topic and frame_filtered:
                return
            raw_record = {
                "ts_utc": now_utc,
                "topic": msg.topic,
                "payload": payload,
                "json": obj if json_ok else None,
            }
            raw_path = str(Path(args.raw_dir) / "raw_messages.jsonl")
            try:
                append_jsonl(raw_path, raw_record)
            except OSError as e:
                print(f"[WARN] Nepodarilo se zapsat raw data: {e}")

    def on_disconnect(client, userdata, reason_code, properties=None):
        rc = getattr(reason_code, "value", reason_code)
        print(f"[MQTT] Disconnected, reason_code={rc}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    print(f"[MQTT] Connecting to {args.host}:{args.port} (TLS), topics={', '.join(topics)}")
    print(f"[FILE] Writing latest state to: {args.out}")
    if args.rx_out:
        print(f"[FILE] Appending frame packets to: {args.rx_out}")
    if args.raw_dir:
        print(f"[FILE] Appending raw mirror to: {Path(args.raw_dir) / 'raw_messages.jsonl'}")
    print(f"[FILE] Confirmed catalog: {args.confirmed_catalog_out}")
    if args.frame_dedupe_window_s > 0:
        print(f"[FILTER] Frame dedupe window: {args.frame_dedupe_window_s:.1f}s per satellite")
    else:
        print("[FILTER] Frame dedupe disabled")
    if args.max_slant_range_km > 0:
        print(f"[FILTER] Max slant range: {args.max_slant_range_km:.1f} km")
    else:
        print("[FILTER] Max slant range disabled")
    if influx_client:
        print(
            f"[INFLUX] Enabled: url={args.influx_url}, org={args.influx_org}, "
            f"bucket={args.influx_bucket}, frame={args.influx_measurement_frame}, "
            f"state={args.influx_measurement_state}, meta={args.influx_measurement_meta}"
        )
    else:
        print("[INFLUX] Disabled (set INFLUXDB_* or --influx-* to enable)")

    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever()
    finally:
        if influx_client:
            influx_client.close()


if __name__ == "__main__":
    main()
