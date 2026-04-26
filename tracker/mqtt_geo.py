#!/usr/bin/env python3
"""TLE-based satellite lookup and geometry helpers for MQTT listener."""

import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mqtt_filters import sat_dedupe_key

COMPACT_NAME_RE = re.compile(r"[^a-z0-9]+")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_km * c


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
        self.by_norad: Dict[int, Any] = {}
        self.by_name: Dict[str, int] = {}
        self.by_compact_name: Dict[str, int] = {}
        self.tle_mtime_ns: Optional[int] = None
        self.pass_cache: Dict[int, Dict[str, float]] = {}
        self.reload(force=True)

    @staticmethod
    def _compact_name_key(name: str) -> str:
        return COMPACT_NAME_RE.sub("", str(name or "").strip().lower())

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
        by_norad: Dict[int, Any] = {}
        by_name: Dict[str, int] = {}
        by_compact_name: Dict[str, int] = {}
        for sat in sats:
            try:
                satnum = int(getattr(getattr(sat, "model", None), "satnum", 0))
            except (TypeError, ValueError):
                continue
            if satnum <= 0:
                continue
            by_norad.setdefault(satnum, sat)
            sat_name = str(getattr(sat, "name", "") or "").strip()
            sat_key = sat_dedupe_key(sat_name)
            if sat_key:
                by_name.setdefault(sat_key, satnum)
            compact_key = self._compact_name_key(sat_name)
            if compact_key:
                by_compact_name.setdefault(compact_key, satnum)

        self.satellites = sats
        self.by_norad = by_norad
        self.by_name = by_name
        self.by_compact_name = by_compact_name
        self.tle_mtime_ns = st.st_mtime_ns
        self.pass_cache.clear()

    def find_satellite(self, norad_id: Optional[int]) -> Optional[Any]:
        if norad_id is None:
            return None

        try:
            self.reload(force=False)
        except OSError:
            return None

        try:
            norad_int = int(norad_id)
        except (TypeError, ValueError):
            return None
        if norad_int <= 0:
            return None
        return self.by_norad.get(norad_int)

    def find_norad_by_name(self, sat_name: Optional[str]) -> Optional[int]:
        if not sat_name:
            return None

        try:
            self.reload(force=False)
        except OSError:
            return None

        sat_key = sat_dedupe_key(str(sat_name))
        if not sat_key:
            sat_key = ""
        norad_id = self.by_name.get(sat_key)
        if norad_id is not None:
            return norad_id

        compact_key = self._compact_name_key(str(sat_name))
        if not compact_key:
            return None

        norad_id = self.by_compact_name.get(compact_key)
        if norad_id is not None:
            return norad_id

        prefix_matches = {
            candidate_norad
            for key, candidate_norad in self.by_compact_name.items()
            if key.startswith(compact_key) or compact_key.startswith(key)
        }
        if len(prefix_matches) == 1:
            return next(iter(prefix_matches))
        return None

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _predict_pass_peak(self, sat: Any, when_dt: datetime) -> Optional[Dict[str, float]]:
        try:
            satnum = int(getattr(getattr(sat, "model", None), "satnum", 0))
        except (TypeError, ValueError):
            return None
        if satnum <= 0:
            return None

        when_utc = self._as_utc(when_dt)
        when_ts = float(when_utc.timestamp())
        cached = self.pass_cache.get(satnum)
        if cached and cached.get("valid_from_ts", 0.0) <= when_ts <= cached.get("valid_to_ts", 0.0):
            return cached

        t0 = self.ts.from_datetime(when_utc - timedelta(hours=4))
        t1 = self.ts.from_datetime(when_utc + timedelta(hours=4))
        try:
            times, events = sat.find_events(self.observer, t0, t1, altitude_degrees=0.0)
        except Exception:
            return None

        event_rows: List[Dict[str, Any]] = []
        for ti, ev in zip(times, events):
            ev_dt = ti.utc_datetime().replace(tzinfo=timezone.utc)
            try:
                topocentric = (sat - self.observer).at(ti)
                alt = float(topocentric.altaz()[0].degrees)
            except Exception:
                alt = -999.0
            event_rows.append({"dt": ev_dt, "ev": int(ev), "alt": alt})

        if not event_rows:
            return None

        passes: List[Dict[str, Any]] = []
        aos_row: Optional[Dict[str, Any]] = None
        culm_row: Optional[Dict[str, Any]] = None
        for row in event_rows:
            ev = row["ev"]
            if ev == 0:
                aos_row = row
                culm_row = None
            elif ev == 1:
                if aos_row is not None:
                    culm_row = row
            elif ev == 2:
                if aos_row is None:
                    continue
                if culm_row is None:
                    culm_row = row
                passes.append(
                    {
                        "aos_dt": aos_row["dt"],
                        "culm_dt": culm_row["dt"],
                        "los_dt": row["dt"],
                        "max_el_deg": float(culm_row["alt"]),
                    }
                )
                aos_row = None
                culm_row = None

        selected: Optional[Dict[str, Any]] = None
        if passes:
            for p in passes:
                if p["aos_dt"] <= when_utc <= p["los_dt"]:
                    selected = p
                    break
            if selected is None:
                selected = min(passes, key=lambda p: abs((p["culm_dt"] - when_utc).total_seconds()))
        else:
            culms = [r for r in event_rows if r["ev"] == 1]
            if not culms:
                return None
            c = min(culms, key=lambda r: abs((r["dt"] - when_utc).total_seconds()))
            selected = {"aos_dt": c["dt"], "culm_dt": c["dt"], "los_dt": c["dt"], "max_el_deg": float(c["alt"])}

        aos_ts = float(selected["aos_dt"].timestamp())
        culm_ts = float(selected["culm_dt"].timestamp())
        los_ts = float(selected["los_dt"].timestamp())
        out = {
            "tle_pass_max_el_deg": float(selected["max_el_deg"]),
            "tle_pass_aos_unix_s": aos_ts,
            "tle_pass_culm_unix_s": culm_ts,
            "tle_pass_los_unix_s": los_ts,
            "tle_pass_duration_s": max(0.0, los_ts - aos_ts),
            "valid_from_ts": aos_ts - 600.0,
            "valid_to_ts": los_ts + 600.0,
        }
        self.pass_cache[satnum] = out
        return out

    def locate(self, norad_id: Optional[int], when_dt: datetime) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tle_found": False}
        sat = self.find_satellite(norad_id)
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
                "tle_norad": int(getattr(getattr(sat, "model", None), "satnum", 0)),
                "sat_lat_deg": sat_lat,
                "sat_lon_deg": sat_lon,
                "sat_alt_km": float(subpoint.elevation.km),
                "sat_az_deg": float(az.degrees),
                "sat_el_deg": float(alt.degrees),
                "slant_range_km": float(distance.km),
                "ground_distance_km": haversine_km(self.gateway_lat, self.gateway_lon, sat_lat, sat_lon),
            }
        )
        pass_peak = self._predict_pass_peak(sat=sat, when_dt=when_dt)
        if pass_peak:
            out.update(
                {
                    "tle_pass_max_el_deg": float(pass_peak["tle_pass_max_el_deg"]),
                    "tle_pass_aos_unix_s": float(pass_peak["tle_pass_aos_unix_s"]),
                    "tle_pass_culm_unix_s": float(pass_peak["tle_pass_culm_unix_s"]),
                    "tle_pass_los_unix_s": float(pass_peak["tle_pass_los_unix_s"]),
                    "tle_pass_duration_s": float(pass_peak["tle_pass_duration_s"]),
                }
            )
        return out
