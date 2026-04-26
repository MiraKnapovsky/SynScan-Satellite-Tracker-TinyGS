#!/usr/bin/env python3
"""TinyGS MQTT listener: ingest state/frame messages and persist telemetry."""

import argparse
import json
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from mqtt_filters import extract_frame_metrics, extract_frame_satellite, sat_dedupe_key
from mqtt_geo import SatelliteLocator
from mqtt_ingest import make_client, parse_env_float, parse_topic_parts
from mqtt_storage import (
    append_jsonl,
    atomic_write_json,
    ensure_parent_dir,
    extract_state_fields_for_influx,
    load_confirmed_catalog,
    load_gateway_from_config,
    save_confirmed_catalog,
)


BASE_DIR = Path(__file__).resolve().parent
TOPIC_TAG_KEYS = ("user", "station", "channel", "cmd", "subcmd")
GEO_FIELD_KEYS = (
    "sat_lat_deg",
    "sat_lon_deg",
    "sat_alt_km",
    "sat_az_deg",
    "sat_el_deg",
    "slant_range_km",
    "ground_distance_km",
    "tle_pass_max_el_deg",
    "tle_pass_aos_unix_s",
    "tle_pass_culm_unix_s",
    "tle_pass_los_unix_s",
    "tle_pass_duration_s",
)

def main() -> None:
    ap = argparse.ArgumentParser(description="TinyGS MQTT listener (state + frame)")
    ap.add_argument("--host", default="mqtt.tinygs.com")
    ap.add_argument("--port", type=int, default=8883)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", default=os.getenv("TINYGS_PASS"))
    ap.add_argument("--cafile", default=os.getenv("TINYGS_CAFILE"), help="Optional path to CA bundle PEM")
    ap.add_argument("--station", default="YOUR_ACTIVE_STATION")
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
    ap.add_argument(
        "--tracker-status-file",
        default=os.getenv("TINYGS_TRACKER_STATUS_FILE", str(BASE_DIR / "synscan_status.json")),
        help="Path to synscan tracker status JSON (tracked_norad source)",
    )
    ap.add_argument(
        "--tracker-status-max-age-s",
        type=float,
        default=float(os.getenv("TINYGS_TRACKER_STATUS_MAX_AGE_S", "10")),
        help="Ignore tracked_norad when tracker status file is older than this many seconds (0 disables)",
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
    if args.tracker_status_max_age_s < 0:
        raise SystemExit("--tracker-status-max-age-s must be >= 0")

    state_topic = args.state_topic or f"tinygs/{args.user}/{args.station}/cmnd/begine"
    frame_topic = args.frame_topic or f"tinygs/{args.user}/{args.station}/cmnd/frame/0"
    topics = [state_topic, frame_topic]

    ensure_parent_dir(args.out)
    ensure_parent_dir(args.rx_out)
    ensure_parent_dir(args.confirmed_catalog_out)
    if args.raw_dir:
        Path(args.raw_dir).mkdir(parents=True, exist_ok=True)

    tracker_status_path: Optional[Path] = None
    tracker_status_file = str(args.tracker_status_file or "").strip()
    if tracker_status_file:
        tracker_status_path = Path(tracker_status_file).expanduser()
    tracker_status_mtime_ns: Optional[int] = None
    tracker_status_cached_norad: Optional[int] = None

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

    def read_tracked_norad(now_dt: datetime) -> Optional[int]:
        nonlocal tracker_status_mtime_ns, tracker_status_cached_norad
        if tracker_status_path is None:
            return None
        try:
            st = tracker_status_path.stat()
        except OSError:
            return None

        if args.tracker_status_max_age_s > 0:
            age_s = now_dt.timestamp() - float(st.st_mtime)
            if age_s > args.tracker_status_max_age_s:
                return None

        if tracker_status_mtime_ns == st.st_mtime_ns:
            return tracker_status_cached_norad

        tracker_status_mtime_ns = st.st_mtime_ns
        tracker_status_cached_norad = None
        try:
            status_obj = json.loads(tracker_status_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to read tracker status {tracker_status_path}: {e}")
            return None
        if not isinstance(status_obj, dict):
            return None
        raw_norad = status_obj.get("tracked_norad")
        try:
            norad = int(raw_norad) if raw_norad is not None else None
        except (TypeError, ValueError):
            norad = None
        if norad is None or norad <= 0:
            return None
        tracker_status_cached_norad = norad
        return tracker_status_cached_norad

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

    frame_last_seen_by_sat: Dict[str, Tuple[float, str]] = {}
    last_state_norad: Optional[int] = None

    def parse_norad_value(raw_value: Any) -> Optional[int]:
        try:
            norad_value = int(float(raw_value))
        except (TypeError, ValueError):
            return None
        if norad_value <= 0:
            return None
        return norad_value

    def extract_message_norad(obj: Any) -> Optional[int]:
        if not isinstance(obj, dict):
            return None
        for key in ("tracked_norad", "NORAD", "norad"):
            norad_value = parse_norad_value(obj.get(key))
            if norad_value is not None:
                return norad_value
        return None

    def resolve_geo_norad(
        now_dt: datetime,
        obj: Any = None,
        sat_name: Optional[str] = None,
    ) -> Optional[int]:
        tracker_norad = read_tracked_norad(now_dt=now_dt)
        if tracker_norad is not None:
            return tracker_norad

        message_norad = extract_message_norad(obj)
        if message_norad is not None:
            return message_norad

        if last_state_norad is not None:
            return last_state_norad

        if sat_locator and sat_name:
            return sat_locator.find_norad_by_name(sat_name)

        return None

    def compact_sat_name(name: Optional[str]) -> str:
        return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

    def sat_names_match(reported_name: Optional[str], tle_name: Optional[str]) -> bool:
        reported_key = sat_dedupe_key(str(reported_name or ""))
        tle_key = sat_dedupe_key(str(tle_name or ""))
        if reported_key and tle_key and reported_key == tle_key:
            return True

        reported_compact = compact_sat_name(reported_name)
        tle_compact = compact_sat_name(tle_name)
        if not reported_compact or not tle_compact:
            return False
        return (
            reported_compact == tle_compact
            or reported_compact.startswith(tle_compact)
            or tle_compact.startswith(reported_compact)
        )

    def locate_with_name_fallback(
        now_dt: datetime,
        tracked_norad: Optional[int],
        sat_name: Optional[str],
    ) -> Tuple[Optional[int], Dict[str, Any]]:
        geo_info: Dict[str, Any] = {"tle_found": False}
        if not sat_locator:
            return tracked_norad, geo_info

        name_norad = sat_locator.find_norad_by_name(sat_name) if sat_name else None

        if tracked_norad is not None:
            try:
                candidate_geo = sat_locator.locate(tracked_norad, now_dt)
            except Exception as e:
                print(f"[WARN] GEO locate failed for tracked_norad={tracked_norad}: {e}")
            else:
                if candidate_geo.get("tle_found"):
                    located_name = str(candidate_geo.get("tle_satellite") or "").strip()
                    if not sat_name or sat_names_match(sat_name, located_name):
                        return tracked_norad, candidate_geo
                    print(
                        "[GEO] NORAD/name mismatch, trying sat-name fallback "
                        f"(tracked_norad={tracked_norad}, satellite={sat_name}, tle_satellite={located_name})"
                    )

        if name_norad is not None:
            try:
                candidate_geo = sat_locator.locate(name_norad, now_dt)
            except Exception as e:
                print(f"[WARN] GEO locate failed for sat-name fallback norad={name_norad}: {e}")
            else:
                if candidate_geo.get("tle_found"):
                    if sat_name and not sat_names_match(sat_name, candidate_geo.get("tle_satellite")):
                        candidate_geo = dict(candidate_geo)
                        candidate_geo["tle_satellite"] = sat_name
                    return name_norad, candidate_geo

        return tracked_norad, geo_info

    client_station = "".join(ch for ch in str(args.station) if ch.isalnum())[:18] or "station"
    client = make_client(client_id=f"tgs-{client_station}")
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

    def handle_state_message(
        obj: Dict[str, Any],
        now_dt: datetime,
        topic_info: Dict[str, Optional[str]],
    ) -> None:
        nonlocal last_state_norad
        state_obj = dict(obj)
        state_obj["last_update"] = datetime.now().strftime("%H:%M:%S")
        try:
            atomic_write_json(args.out, state_obj)
        except OSError as e:
            print(f"[WARN] Nepodarilo se zapsat {args.out}: {e}")

        state_sat_name = str(obj.get("sat") or "").strip()
        state_geo_info: Dict[str, Any] = {"tle_found": False}
        tracked_norad = resolve_geo_norad(now_dt=now_dt, obj=obj, sat_name=state_sat_name)
        tracked_norad, state_geo_info = locate_with_name_fallback(
            now_dt=now_dt,
            tracked_norad=tracked_norad,
            sat_name=state_sat_name,
        )
        if tracked_norad is not None and state_geo_info.get("tle_found"):
            last_state_norad = tracked_norad

        if not influx_point_cls:
            return

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
        for key in TOPIC_TAG_KEYS:
            value = topic_info.get(key)
            if value:
                point = point.tag(key, str(value))
        if state_sat_name:
            point = point.tag("satellite", state_sat_name)
        if tracked_norad is not None:
            point = point.tag("tracked_norad", str(tracked_norad))
            point = point.field("tracked_norad", float(tracked_norad))
        if obj.get("mode"):
            point = point.tag("mode", str(obj["mode"]))
        point = point.field("state_seen", True)
        point = point.field("geo_enabled", geo_enabled)
        point = point.field("tle_found", bool(state_geo_info.get("tle_found", False)))
        if state_geo_info.get("tle_found"):
            tle_name = str(state_geo_info.get("tle_satellite") or "").strip()
            if tle_name:
                point = point.tag("tle_satellite", tle_name)
            for key in GEO_FIELD_KEYS:
                if key in state_geo_info:
                    point = point.field(key, float(state_geo_info[key]))
        for field_key, field_value in extract_state_fields_for_influx(
            obj, gateway_lat=curr_lat, gateway_lon=curr_lon, gateway_alt_m=curr_alt_m
        ).items():
            point = point.field(field_key, field_value)
        write_influx(point)
        # Keep all-time unique confirmed count fresh in Influx for dashboard stats.
        write_confirmed_catalog_meta(now_dt=now_dt)

    def handle_frame_message(
        msg_topic: str,
        payload: str,
        obj: Any,
        json_ok: bool,
        now_dt: datetime,
        now_utc: str,
        topic_info: Dict[str, Optional[str]],
    ) -> bool:
        frame_filtered = False
        geo_info: Dict[str, Any] = {"tle_found": False}
        sat_name = extract_frame_satellite(obj if json_ok else None)
        frame_metrics = extract_frame_metrics(obj if json_ok else None)
        tracked_norad = resolve_geo_norad(now_dt=now_dt, obj=obj, sat_name=sat_name)
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
            current_status = str(frame_metrics.get("decode_status") or "unknown")
            prev_info = frame_last_seen_by_sat.get(key)
            if prev_info is not None:
                prev_ts, prev_status = prev_info
                within_window = (now_ts - prev_ts) <= args.frame_dedupe_window_s
                prefer_confirmed = current_status == "confirmed" and prev_status != "confirmed"
                if within_window and not prefer_confirmed:
                    frame_filtered = True
                    print(
                        "[FILTER] Dropped frame: duplicate satellite frame "
                        f"(satellite={sat_name}, window={args.frame_dedupe_window_s:.1f}s)"
                    )
                else:
                    frame_last_seen_by_sat[key] = (now_ts, current_status)
            else:
                frame_last_seen_by_sat[key] = (now_ts, current_status)

        if not frame_filtered and sat_locator:
            tracked_norad, geo_info = locate_with_name_fallback(
                now_dt=now_dt,
                tracked_norad=tracked_norad,
                sat_name=sat_name,
            )

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
                "topic": msg_topic,
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
                for key in GEO_FIELD_KEYS:
                    if key in geo_info:
                        rx_record[key] = geo_info[key]
            if tracked_norad is not None:
                rx_record["tracked_norad"] = tracked_norad
            try:
                append_jsonl(args.rx_out, rx_record)
            except OSError as e:
                print(f"[WARN] Nepodarilo se zapsat rx data: {e}")

        if not frame_filtered and influx_point_cls:
            point = influx_point_cls(args.influx_measurement_frame).time(now_dt)
            for key in TOPIC_TAG_KEYS:
                value = topic_info.get(key)
                if value:
                    point = point.tag(key, str(value))
            if sat_name:
                point = point.tag("satellite", sat_name)
            if tracked_norad is not None:
                point = point.tag("tracked_norad", str(tracked_norad))
                point = point.field("tracked_norad", float(tracked_norad))
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
                for key in GEO_FIELD_KEYS:
                    if key in geo_info:
                        point = point.field(key, float(geo_info[key]))
            write_influx(point)

        return frame_filtered

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
            handle_state_message(obj=obj, now_dt=now_dt, topic_info=topic_info)

        if msg.topic == frame_topic:
            frame_filtered = handle_frame_message(
                msg_topic=msg.topic,
                payload=payload,
                obj=obj,
                json_ok=json_ok,
                now_dt=now_dt,
                now_utc=now_utc,
                topic_info=topic_info,
            )

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

    def on_disconnect(client, userdata, disconnect_flags=None, reason_code=None, properties=None):
        # Support both legacy and current paho-mqtt callback signatures.
        reason = reason_code if reason_code is not None else disconnect_flags
        rc = getattr(reason, "value", reason)
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
    if tracker_status_path is not None:
        if args.tracker_status_max_age_s > 0:
            print(
                f"[TRACKER] tracked_norad source: {tracker_status_path} "
                f"(max age {args.tracker_status_max_age_s:.1f}s)"
            )
        else:
            print(f"[TRACKER] tracked_norad source: {tracker_status_path} (no max age)")
    else:
        print("[TRACKER] tracked_norad source disabled")
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
