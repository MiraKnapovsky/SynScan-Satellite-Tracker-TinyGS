#!/usr/bin/env python3
"""Validate synscan_config.json and exec the tracker with safe arguments."""

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG = BASE_DIR / "synscan_config.json"
SCRIPT = BASE_DIR / "synscan_follow_sat.py"

def _err(msg: str) -> None:
    print(f"[runner] {msg}", file=sys.stderr)
    sys.exit(2)

def as_float(x, name):
    try:
        return float(x)
    except Exception:
        _err(f"Neplatné číslo pro '{name}': {x!r}")

def ensure_exists(p: str, name: str):
    path = Path(p)
    if not path.exists():
        _err(f"Soubor/cesta pro '{name}' neexistuje: {p}")
    return str(path)

def main():
    if not CONFIG.exists():
        _err(f"Chybí config: {CONFIG}")

    data = json.loads(CONFIG.read_text(encoding="utf-8"))

    dummy = bool(data.get("dummy", False))

    lat = as_float(data.get("lat"), "lat")
    lon = as_float(data.get("lon"), "lon")
    alt = as_float(data.get("alt", 0), "alt")

    if not (-90.0 <= lat <= 90.0): _err("lat mimo rozsah -90..90")
    if not (-180.0 <= lon <= 180.0): _err("lon mimo rozsah -180..180")

    tle = ensure_exists(data.get("tle", ""), "tle")

    state = str(data.get("state", str(BASE_DIR / "state.json")))
    ensure_exists(state, "state")

    min_el = as_float(data.get("min_el", -10.0), "min_el")
    interval = as_float(data.get("interval", 0.5), "interval")
    lead = as_float(data.get("lead", 0.8), "lead")
    wrap_limit = as_float(data.get("wrap_limit", 270.0), "wrap_limit")
    wrap_margin = as_float(data.get("wrap_margin", 10.0), "wrap_margin")
    plan_horizon = as_float(data.get("plan_horizon", 2400.0), "plan_horizon")
    plan_step = as_float(data.get("plan_step", 2.0), "plan_step")
    az_home = as_float(data.get("az_home", 0.0), "az_home")

    if interval <= 0: _err("interval musí být > 0")
    if lead < 0: _err("lead musí být >= 0")
    if wrap_limit <= 0: _err("wrap_limit musí být > 0")
    if wrap_margin < 0: _err("wrap_margin musí být >= 0")
    if plan_horizon <= 0: _err("plan_horizon musí být > 0")
    if plan_step <= 0: _err("plan_step musí být > 0")

    # --- status pro web ---
    status_file = data.get("status_file", str(BASE_DIR / "synscan_status.json"))
    status_every = as_float(data.get("status_every", 1.0), "status_every")
    if status_every <= 0:
        _err("status_every musí být > 0")
    if status_file is not None:
        status_file = str(status_file).strip()
        if status_file == "":
            status_file = None


    args = [sys.executable, str(SCRIPT)]

    if dummy:
        args += ["--dummy"]
    else:
        port = str(data.get("port", "")).strip()
        if not port.startswith("/dev/"):
            _err("port musí být zařízení v /dev/... (např. /dev/ttyUSB0)")
        args += ["--port", port]

    args += [
        "--lat", str(lat),
        "--lon", str(lon),
        "--alt", str(alt),
        "--tle", tle,
        "--min-el", str(min_el),
        "--interval", str(interval),
        "--lead", str(lead),
        "--wrap-limit", str(wrap_limit),
        "--wrap-margin", str(wrap_margin),
        "--plan-horizon", str(plan_horizon),
        "--plan-step", str(plan_step),
        "--az-home", str(az_home),
    ]

    args += ["--state", state]

    if status_file:
        args += ["--status-file", status_file, "--status-every", str(status_every)]

    os.execv(sys.executable, args)

if __name__ == "__main__":
    main()
