#!/usr/bin/env python3
"""TLE-based satellite lookup and geometry helpers for MQTT listener."""

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from mqtt_filters import normalize_sat_name


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
