#!/usr/bin/env python3
"""Validate synscan_config.json and exec the tracker with safe arguments."""

import json
import os
import sys
from pathlib import Path

TRACKER_DIR = Path(__file__).resolve().parent
BASE_DIR = TRACKER_DIR.parent
CONFIG = BASE_DIR / "synscan_config.json"
SCRIPT = TRACKER_DIR / "synscan_follow_sat.py"

def _err(msg: str) -> None:
    print(f"[runner] {msg}", file=sys.stderr)
    sys.exit(2)

def as_float(x, name):
    try:
        return float(x)
    except Exception:
        _err(f"Invalid number for '{name}': {x!r}")

def as_bool(x, name):
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        raw = x.strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off", ""}:
            return False
    _err(f"Invalid boolean value for '{name}': {x!r}")

def resolve_config_path(p: str, name: str, *, must_exist: bool = False) -> str:
    raw = str(p or "").strip()
    if not raw:
        _err(f"Missing path for '{name}'")

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path

    if must_exist and not path.exists():
        _err(f"File/path for '{name}' does not exist: {path}")
    return str(path)

def main():
    if not CONFIG.exists():
        _err(f"Missing config: {CONFIG}")

    data = json.loads(CONFIG.read_text(encoding="utf-8"))

    dummy = as_bool(data.get("dummy", False), "dummy")

    lat = as_float(data.get("lat"), "lat")
    lon = as_float(data.get("lon"), "lon")
    alt = as_float(data.get("alt", 0), "alt")

    if not (-90.0 <= lat <= 90.0): _err("lat out of range -90..90")
    if not (-180.0 <= lon <= 180.0): _err("lon out of range -180..180")

    tle = resolve_config_path(data.get("tle", ""), "tle", must_exist=True)

    state = resolve_config_path(data.get("state", "state.json"), "state")

    min_el = as_float(data.get("min_el", -10.0), "min_el")
    interval = as_float(data.get("interval", 0.5), "interval")
    lead = as_float(data.get("lead", 0.8), "lead")
    max_az_step = as_float(data.get("max_az_step", 0.0), "max_az_step")
    max_el_step = as_float(data.get("max_el_step", 0.0), "max_el_step")
    wrap_limit = as_float(data.get("wrap_limit", 270.0), "wrap_limit")
    wrap_margin = as_float(data.get("wrap_margin", 10.0), "wrap_margin")
    plan_horizon = as_float(data.get("plan_horizon", 2400.0), "plan_horizon")
    plan_step = as_float(data.get("plan_step", 2.0), "plan_step")
    az_home = as_float(data.get("az_home", 0.0), "az_home")
    invert_elevation = as_bool(data.get("invert_elevation", False), "invert_elevation")
    elevation_offset_deg = as_float(data.get("elevation_offset_deg", 0.0), "elevation_offset_deg")

    if interval <= 0: _err("interval must be > 0")
    if lead < 0: _err("lead must be >= 0")
    if max_az_step < 0: _err("max_az_step must be >= 0")
    if max_el_step < 0: _err("max_el_step must be >= 0")
    if wrap_limit <= 0: _err("wrap_limit must be > 0")
    if wrap_margin < 0: _err("wrap_margin must be >= 0")
    if plan_horizon <= 0: _err("plan_horizon must be > 0")
    if plan_step <= 0: _err("plan_step must be > 0")

    # --- status for web ---
    status_file = data.get("status_file", "synscan_status.json")
    status_every = as_float(data.get("status_every", 1.0), "status_every")
    if status_every <= 0:
        _err("status_every must be > 0")
    if status_file is not None:
        status_file = str(status_file).strip()
        if status_file == "":
            status_file = None
        else:
            status_file = resolve_config_path(status_file, "status_file")


    args = [sys.executable, str(SCRIPT)]

    if dummy:
        args += ["--dummy"]
    else:
        port = str(data.get("port", "")).strip()
        if not port.startswith("/dev/"):
            _err("port must be a /dev/... device, for example /dev/ttyUSB0")
        args += ["--port", port]

    args += [
        "--lat", str(lat),
        "--lon", str(lon),
        "--alt", str(alt),
        "--tle", tle,
        "--min-el", str(min_el),
        "--interval", str(interval),
        "--lead", str(lead),
        "--max-az-step", str(max_az_step),
        "--max-el-step", str(max_el_step),
        "--wrap-limit", str(wrap_limit),
        "--wrap-margin", str(wrap_margin),
        "--plan-horizon", str(plan_horizon),
        "--plan-step", str(plan_step),
        "--az-home", str(az_home),
    ]

    if invert_elevation:
        args += ["--invert-elevation"]
    if elevation_offset_deg != 0.0:
        args += ["--elevation-offset-deg", str(elevation_offset_deg)]

    args += ["--state", state]

    if status_file:
        args += ["--status-file", status_file, "--status-every", str(status_every)]

    os.execv(sys.executable, args)

if __name__ == "__main__":
    main()
