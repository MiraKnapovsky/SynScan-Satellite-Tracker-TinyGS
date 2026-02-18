#!/usr/bin/env python3
"""Storage and serialization helpers for MQTT listener state/catalog files."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mqtt_filters import sat_dedupe_key


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
