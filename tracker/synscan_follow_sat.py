#!/usr/bin/env python3
"""Main SynScan tracking loop using TLE prediction and TinyGS state targets."""
# Real mount:
# python3 tracker/synscan_follow_sat.py --port /dev/ttyUSB0 --lat 49.83 --lon 18.17 --alt 240 --tle satellites.tle --state state.json --min-el 10 --interval 0.5 --lead 0.8 --status-file synscan_status.json
#
# Dry run without mount:
# python3 tracker/synscan_follow_sat.py --dummy --lat 49.83 --lon 18.17 --alt 240 --tle satellites.tle --state state.json --interval 0.5 --lead 0.8 --status-file synscan_status.json

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import serial
from skyfield.api import EarthSatellite, load, wgs84

from synscan_common import (
    clamp_el,
    deg_to_hex16,
    open_port,
    send_cmd as serial_send_cmd,
    user_el_to_mount_el,
)

BASE_DIR = Path(__file__).resolve().parents[1]

# ---------- RS-232 ----------
def send_cmd(ser: Optional[serial.Serial], payload: str, dummy: bool) -> str:
    if dummy:
        # In dummy mode, log mount commands explicitly for debugging.
        print(f"\n[DUMMY] -> {payload}")
    return serial_send_cmd(ser, payload, dummy=dummy)

def hc_busy(ser: Optional[serial.Serial], dummy: bool) -> bool:
    if dummy or ser is None:
        return False
    try:
        rsp = serial_send_cmd(ser, "L")
        return rsp.startswith("1")
    except Exception:
        return False

# ---------- TLE file ----------
def load_tles(path: Path) -> List[EarthSatellite]:
    ts = load.timescale()
    if not path.exists():
        raise FileNotFoundError(f"File {path} was not found")

    lines = path.read_text(encoding='utf-8', errors='ignore').splitlines()
    sats: List[EarthSatellite] = []
    i = 0
    while i + 2 < len(lines):
        name = lines[i].strip()
        l1 = lines[i+1].strip()
        l2 = lines[i+2].strip()
        if l1.startswith('1 ') and l2.startswith('2 '):
            try:
                sats.append(EarthSatellite(l1, l2, name=name, ts=ts))
            except Exception:
                pass
            i += 3
        else:
            i += 1
    return sats

def altaz_deg(sat: EarthSatellite, observer, t) -> Tuple[float, float]:
    alt, az, _ = (sat - observer).at(t).altaz()
    return float(alt.degrees), float(az.degrees)

def deltadeg_wrap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)

def shortest_delta(to_deg: float, from_deg: float) -> float:
    """Signed shortest delta from from_deg to to_deg in [-180, 180]."""
    return (to_deg - from_deg + 180.0) % 360.0 - 180.0

def unwrap_series(az_list: List[float]) -> List[float]:
    if not az_list:
        return []
    out = [az_list[0]]
    last = az_list[0]
    for az in az_list[1:]:
        d = shortest_delta(az, last)
        out.append(out[-1] + d)
        last = az
    return out

def predict_pass_series(sat: EarthSatellite, observer, ts, t_start,
                        min_el: float, max_dt_s: float, step_s: float) -> Optional[Tuple[float, float, List[float], List[float]]]:
    """Return (t_aos, t_los, az_list, el_list) for next pass, or None if not found."""
    dt = 0.0
    aos = None
    azs: List[float] = []
    els: List[float] = []
    while dt <= max_dt_s:
        t = t_start + dt / 86400.0
        el, az = altaz_deg(sat, observer, t)
        if aos is None:
            if el >= min_el:
                aos = dt
                azs.append(az)
                els.append(el)
        else:
            azs.append(az)
            els.append(el)
            if el < min_el:
                los = dt
                return (aos, los, azs, els)
        dt += step_s
    return None

def update_unwrapped(last_raw: Optional[float], last_unwrapped: Optional[float], new_raw: float) -> Tuple[float, float]:
    if last_raw is None or last_unwrapped is None:
        return new_raw, new_raw
    return new_raw, last_unwrapped + shortest_delta(new_raw, last_raw)

def step_towards_linear(target_deg: float, current_deg: Optional[float], max_step_deg: float) -> float:
    if current_deg is None or max_step_deg <= 0:
        return target_deg
    delta = target_deg - current_deg
    if abs(delta) <= max_step_deg:
        return target_deg
    return current_deg + (max_step_deg if delta > 0 else -max_step_deg)

def step_towards_az(target_deg: float, current_deg: Optional[float], max_step_deg: float) -> float:
    if current_deg is None or max_step_deg <= 0:
        return target_deg % 360.0
    delta = shortest_delta(target_deg, current_deg)
    if abs(delta) <= max_step_deg:
        return target_deg % 360.0
    return (current_deg + (max_step_deg if delta > 0 else -max_step_deg)) % 360.0

def segment_move(
    *,
    current_az_raw: Optional[float],
    current_az_unwrapped: Optional[float],
    current_el_user: Optional[float],
    target_az_raw: float,
    target_el_user: float,
    max_az_step_deg: float,
    max_el_step_deg: float,
    target_az_unwrapped: Optional[float] = None,
) -> Tuple[float, float, float]:
    send_el_user = step_towards_linear(target_el_user, current_el_user, max_el_step_deg)

    if target_az_unwrapped is not None and current_az_unwrapped is not None:
        send_az_unwrapped = step_towards_linear(target_az_unwrapped, current_az_unwrapped, max_az_step_deg)
        send_az_raw = send_az_unwrapped % 360.0
    else:
        send_az_raw = step_towards_az(target_az_raw, current_az_raw, max_az_step_deg)
        if current_az_raw is None or current_az_unwrapped is None:
            send_az_unwrapped = send_az_raw
        else:
            _, send_az_unwrapped = update_unwrapped(current_az_raw, current_az_unwrapped, send_az_raw)

    return send_az_raw, send_az_unwrapped, send_el_user

def choose_wrap_shift(az_unwrapped: List[float], limit: float, margin: float,
                      current_unwrapped: Optional[float]) -> Tuple[int, bool]:
    """Pick k for az_unwrapped + k*360 that best fits in [-limit, limit]."""
    if not az_unwrapped:
        return 0, False
    best = None
    best_k = 0
    for k in range(-2, 3):
        shifted = [v + k * 360.0 for v in az_unwrapped]
        max_abs = max(abs(v) for v in shifted)
        ok = max_abs <= (limit - margin)
        dist = abs(shifted[0] - current_unwrapped) if current_unwrapped is not None else 0.0
        score = (not ok, dist, max_abs)
        if best is None or score < best:
            best = score
            best_k = k
    ok = best[0] is False if best is not None else False
    return best_k, ok

# ---------- state.json (MQTT) ----------
def read_state(path: Path) -> Tuple[Optional[int], Optional[str]]:
    """Return (norad, sat_str) from JSON, or (None, None) when missing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        norad = data.get("NORAD", None)
        sat_s = data.get("sat", None)

        if isinstance(norad, str):
            norad = norad.strip()
            norad = int(norad) if norad.isdigit() else None
        elif isinstance(norad, (int, float)):
            norad = int(norad)
        else:
            norad = None

        if sat_s is not None:
            sat_s = str(sat_s).strip()
            if sat_s == "":
                sat_s = None

        return norad, sat_s
    except Exception:
        return None, None

def pick_sat_from_state(
    sats_by_norad: Dict[int, EarthSatellite],
    norad: Optional[int],
) -> Optional[EarthSatellite]:
    # NORAD-only mapping; no name fallback.
    if norad is not None and norad in sats_by_norad:
        return sats_by_norad[norad]
    return None

# ---------- status JSON for web ----------
def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def resolve_runtime_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dummy', action='store_true', help='Run without mount movement; only print commands')
    ap.add_argument('--port', default=None, help='For example /dev/ttyUSB0; not needed with --dummy')

    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--alt', type=float, default=0.0)
    ap.add_argument('--tle', dest='tle_file', required=True)

    ap.add_argument('--state', dest='state_file', default='state.json',
                    help='Path to state.json used for NORAD target selection')

    ap.add_argument('--min-el', type=float, default=-10.0)
    ap.add_argument('--interval', type=float, default=0.5)
    ap.add_argument('--lead', type=float, default=0.8)
    ap.add_argument('--min-step', type=float, default=0.10)
    ap.add_argument('--max-az-step', type=float, default=0.0,
                    help='Maximum azimuth change per goto step; 0 disables segmentation.')
    ap.add_argument('--max-el-step', type=float, default=0.0,
                    help='Maximum elevation change per goto step; 0 disables segmentation.')
    ap.add_argument('--wrap-limit', type=float, default=270.0,
                    help='Cable soft limit in degrees from az=0.')
    ap.add_argument('--wrap-margin', type=float, default=10.0,
                    help='Safety margin below wrap-limit, in degrees.')
    ap.add_argument('--plan-horizon', type=float, default=2400.0,
                    help='Next-pass prediction horizon in seconds.')
    ap.add_argument('--plan-step', type=float, default=2.0,
                    help='Az/el prediction step during a pass, in seconds.')
    ap.add_argument('--az-home', type=float, default=0.0,
                    help='Safe azimuth for neutral/unwind positioning before a pass.')
    ap.add_argument('--invert-elevation', action='store_true',
                    help='Send inverted elevation to the mount (mount_el = 90 - el).')
    ap.add_argument('--elevation-offset-deg', type=float, default=0.0,
                    help='Elevation correction at 0 deg, linearly tapering to 0 deg at 90 deg.')

    ap.add_argument('--center-el', type=float, default=50.0,
                    help='User elevation in degrees for neutral position when state.json has no NORAD')

    # --- status file for web ---
    ap.add_argument('--status-file', default=None,
                    help='Path to JSON file where current status is written for the web UI')
    ap.add_argument('--status-every', type=float, default=1.0,
                    help='How often to write status, in seconds')

    args = ap.parse_args()

    if not args.dummy and not args.port:
        raise SystemExit("Missing --port, or use --dummy.")

    tle_path = resolve_runtime_path(args.tle_file)
    state_path = resolve_runtime_path(args.state_file)
    status_path = resolve_runtime_path(args.status_file) if args.status_file else None
    status_last = 0.0

    last_cmd: Optional[str] = None
    last_cmd_ts: Optional[str] = None

    def mount_el(angle_deg: float) -> float:
        return user_el_to_mount_el(
            angle_deg,
            invert_elevation=args.invert_elevation,
            elevation_offset_deg=args.elevation_offset_deg,
        )

    def emit_status(*, phase: str, message: str,
                    tracked: Optional[EarthSatellite],
                    desired_norad: Optional[int],
                    desired_sat_str: Optional[str],
                    state_has_any_key: bool,
                    do_center: bool,
                    az: Optional[float],
                    el_u: Optional[float],
                    el_cmd: Optional[float],
                    force: bool = False) -> None:
        nonlocal status_last
        if status_path is None:
            return
        now_m = time.monotonic()
        if (not force) and (now_m - status_last) < float(args.status_every):
            return

        payload: Dict[str, Any] = {
            "ts": iso_now(),
            "phase": phase,                 # tracking / center / wait_rise / below_min_el / no_target / state_no_target / starting / stopped
            "message": message,             # human-readable message for web
            "mode": "state",
            "dummy": bool(args.dummy),
            "port": None if args.dummy else args.port,
            "lat": args.lat,
            "lon": args.lon,
            "alt": args.alt,
            "tle": str(tle_path),
            "state_file": str(state_path),
            "min_el": float(args.min_el),
            "interval": float(args.interval),
            "lead": float(args.lead),
            "center_el": float(args.center_el),
            "wrap_limit": float(args.wrap_limit),
            "wrap_margin": float(args.wrap_margin),
            "az_home": float(args.az_home),
            "invert_elevation": bool(args.invert_elevation),
            "elevation_offset_deg": float(args.elevation_offset_deg),

            "state_has_any_key": bool(state_has_any_key),
            "desired_norad": desired_norad,
            "desired_sat": desired_sat_str,

            "tracked_name": tracked.name if tracked else None,
            "tracked_norad": int(tracked.model.satnum) if tracked else None,

            "az_deg": az,
            "el_deg": el_cmd,
            "az_unwrapped": az_unwrapped,
            "az_plan_k": plan_k,
            "az_plan_ok": plan_ok,
            "az_plan_aos_s": plan_aos,
            "az_plan_los_s": plan_los,
            "az_plan_start_unwrapped": plan_start_unwrapped,
            "unwind_active": unwind_active,

            "last_cmd": last_cmd,
            "last_cmd_ts": last_cmd_ts,
        }

        try:
            atomic_write_json(status_path, payload)
        except Exception:
            pass
        status_last = now_m

    sats = load_tles(tle_path)
    print(f"Loaded {len(sats)} satellites from {tle_path}")

    # NORAD -> satellite mapping (Skyfield: sat.model.satnum)
    sats_by_norad: Dict[int, EarthSatellite] = {}
    for s in sats:
        try:
            sats_by_norad[int(s.model.satnum)] = s
        except Exception:
            pass

    observer = wgs84.latlon(args.lat, args.lon, elevation_m=args.alt)
    ts = load.timescale()

    ser: Optional[serial.Serial] = None
    if not args.dummy:
        ser = open_port(args.port)

    tracked: Optional[EarthSatellite] = None
    last_az: Optional[float] = None
    last_el_user: Optional[float] = None
    az_unwrapped: Optional[float] = None

    # wrap planning state
    plan_for_satnum: Optional[int] = None
    plan_k: int = 0
    plan_ok: Optional[bool] = None
    plan_aos: Optional[float] = None
    plan_los: Optional[float] = None
    plan_start_unwrapped: Optional[float] = None
    plan_last_mono: float = 0.0
    unwind_active: bool = False

    # state cache
    last_state_mtime = None
    desired_norad: Optional[int] = None
    desired_sat_str: Optional[str] = None
    desired_sat_obj: Optional[EarthSatellite] = None
    state_has_any_key = False  # whether state contains a valid NORAD

    emit_status(
        phase="starting",
        message="Starting...",
        tracked=tracked,
        desired_norad=desired_norad,
        desired_sat_str=desired_sat_str,
        state_has_any_key=state_has_any_key,
        do_center=False,
        az=last_az,
        el_u=last_el_user,
        el_cmd=clamp_el(last_el_user) if last_el_user is not None else None,
        force=True
    )

    try:
        while True:
            t_now = ts.now()
            t_target = t_now + args.lead/86400.0

            try:
                mtime = state_path.stat().st_mtime
            except FileNotFoundError:
                mtime = None

            if mtime is not None and mtime != last_state_mtime:
                last_state_mtime = mtime
                desired_norad, desired_sat_str = read_state(state_path)
                state_has_any_key = desired_norad is not None
                desired_sat_obj = pick_sat_from_state(sats_by_norad, desired_norad)

                if desired_norad is not None:
                    if desired_sat_obj:
                        print(
                            f"\n[STATE] Target: NORAD={desired_norad} -> TLE='{desired_sat_obj.name}' "
                            f"(state sat='{desired_sat_str}')"
                        )
                    else:
                        print(
                            f"\n[STATE] Target not found in TLE: NORAD={desired_norad} "
                            f"(state sat='{desired_sat_str}')"
                        )
                else:
                    print(
                        f"\n[STATE] state.json has no NORAD -> surveillance/neutral mode "
                        f"(Az={args.az_home}°, El={args.center_el}°)"
                    )

            # --- target selection ---
            target_sat: Optional[EarthSatellite] = None
            fallback_tracking = False
            do_center = False

            if desired_norad is None:
                tracked = None
                do_center = True
            else:
                target_sat = desired_sat_obj
                if target_sat is None:
                    if tracked is not None:
                        prev_el_u, _ = altaz_deg(tracked, observer, t_target)
                        if prev_el_u >= args.min_el:
                            target_sat = tracked
                            fallback_tracking = True
                        else:
                            tracked = None

            # --- center mode: neutral position az_home + center_el ---
            if do_center:
                az = float(args.az_home)
                el_u = float(args.center_el)

                if not hc_busy(ser, args.dummy):
                    need_move = (
                        last_az is None or
                        last_el_user is None or
                        max(deltadeg_wrap(az, last_az), abs(el_u - last_el_user)) >= args.min_step
                    )
                    if need_move:
                        send_az, send_az_unwrapped, send_el_user = segment_move(
                            current_az_raw=last_az,
                            current_az_unwrapped=az_unwrapped if az_unwrapped is not None else last_az,
                            current_el_user=last_el_user,
                            target_az_raw=az,
                            target_el_user=el_u,
                            max_az_step_deg=args.max_az_step,
                            max_el_step_deg=args.max_el_step,
                        )
                        cmd = f"B{deg_to_hex16(send_az)},{deg_to_hex16(mount_el(send_el_user))}"
                        last_cmd = cmd
                        last_cmd_ts = iso_now()
                        send_cmd(ser, cmd, args.dummy)
                        last_az = send_az
                        az_unwrapped = send_az_unwrapped
                        last_el_user = send_el_user

                    sys.stdout.write(
                        f"\r[SURVEILLANCE] Neutral Az: {az:6.1f}° | El: {el_u:5.1f}°     "
                    )
                    sys.stdout.flush()

                emit_status(
                    phase="center",
                    message=f"[SURVEILLANCE] Neutral Az {az:.1f}° | El {el_u:.1f}°",
                    tracked=tracked,
                    desired_norad=desired_norad,
                    desired_sat_str=desired_sat_str,
                    state_has_any_key=state_has_any_key,
                    do_center=True,
                    az=az,
                    el_u=el_u,
                    el_cmd=clamp_el(el_u),
                )

                time.sleep(args.interval)
                continue

            # --- tracking and movement ---
            if target_sat:
                tracked = target_sat
                el_u, az = altaz_deg(tracked, observer, t_target)

                # --- wrap planning / unwind (only when satellite is below min-el) ---
                if el_u < args.min_el:
                    now_m = time.monotonic()
                    satnum = None
                    try:
                        satnum = int(tracked.model.satnum)
                    except Exception:
                        satnum = None

                    if satnum is not None and (plan_for_satnum != satnum or (now_m - plan_last_mono) > 5.0):
                        plan_last_mono = now_m
                        plan_for_satnum = satnum
                        plan_ok = None
                        plan_start_unwrapped = None
                        curr_unwrapped = az_unwrapped if az_unwrapped is not None else last_az
                        res = predict_pass_series(tracked, observer, ts, t_now,
                                                  args.min_el, args.plan_horizon, args.plan_step)
                        if res:
                            plan_aos, plan_los, az_list, _ = res
                            az_unw = unwrap_series(az_list)
                            plan_start_unwrapped = az_unw[0] if az_unw else None
                            plan_k, plan_ok = choose_wrap_shift(az_unw, args.wrap_limit, args.wrap_margin, curr_unwrapped)
                        else:
                            plan_aos = None
                            plan_los = None
                            plan_k = 0
                            plan_ok = False

                    need_unwind = False
                    desired_start = None
                    curr_unwrapped = az_unwrapped if az_unwrapped is not None else last_az
                    if plan_ok and plan_start_unwrapped is not None and curr_unwrapped is not None:
                        desired_start = plan_start_unwrapped + plan_k * 360.0
                        if abs(desired_start - curr_unwrapped) > 180.0:
                            need_unwind = True

                    if need_unwind:
                        unwind_active = True
                        if not hc_busy(ser, args.dummy):
                            unwind_el_user = clamp_el(args.min_el)
                            if (
                                last_az is None or
                                last_el_user is None or
                                curr_unwrapped is None or
                                desired_start is None or
                                abs(desired_start - curr_unwrapped) >= args.min_step or
                                abs(unwind_el_user - last_el_user) >= args.min_step
                            ):
                                send_az, send_az_unwrapped, send_el_user = segment_move(
                                    current_az_raw=last_az,
                                    current_az_unwrapped=curr_unwrapped,
                                    current_el_user=last_el_user,
                                    target_az_raw=desired_start % 360.0 if desired_start is not None else args.az_home,
                                    target_el_user=unwind_el_user,
                                    max_az_step_deg=args.max_az_step,
                                    max_el_step_deg=args.max_el_step,
                                    target_az_unwrapped=desired_start,
                                )
                                cmd = f"B{deg_to_hex16(send_az)},{deg_to_hex16(mount_el(send_el_user))}"
                                last_cmd = cmd
                                last_cmd_ts = iso_now()
                                send_cmd(ser, cmd, args.dummy)
                                last_az = send_az
                                az_unwrapped = send_az_unwrapped
                                last_el_user = send_el_user
                    else:
                        unwind_active = False

                # Before the satellite rises, preset azimuth and hold minimum
                # elevation so the rotator is ready at AOS.
                if el_u < 0:
                    wait_rise_el_u = clamp_el(args.min_el)

                    if not hc_busy(ser, args.dummy):
                        need_move = (
                            last_az is None or
                            last_el_user is None or
                            max(deltadeg_wrap(az, last_az), abs(wait_rise_el_u - last_el_user)) >= args.min_step
                        )
                        if need_move:
                            send_az, send_az_unwrapped, send_el_user = segment_move(
                                current_az_raw=last_az,
                                current_az_unwrapped=az_unwrapped if az_unwrapped is not None else last_az,
                                current_el_user=last_el_user,
                                target_az_raw=az,
                                target_el_user=wait_rise_el_u,
                                max_az_step_deg=args.max_az_step,
                                max_el_step_deg=args.max_el_step,
                            )
                            cmd = f"B{deg_to_hex16(send_az)},{deg_to_hex16(mount_el(send_el_user))}"
                            last_cmd = cmd
                            last_cmd_ts = iso_now()
                            send_cmd(ser, cmd, args.dummy)
                            last_az = send_az
                            az_unwrapped = send_az_unwrapped
                            last_el_user = send_el_user

                    msg = (
                        f"({tracked.name}) Waiting for rise... "
                        f"Ready Az {az:6.1f}° | El {wait_rise_el_u:5.1f}° "
                        f"(sat {el_u:5.1f}°)"
                    )
                    sys.stdout.write(f"\r{msg}  ")
                    sys.stdout.flush()

                    emit_status(
                        phase="wait_rise",
                        message=msg,
                        tracked=tracked,
                        desired_norad=desired_norad,
                        desired_sat_str=desired_sat_str,
                        state_has_any_key=state_has_any_key,
                        do_center=False,
                        az=az,
                        el_u=el_u,
                        el_cmd=wait_rise_el_u,
                    )

                    time.sleep(1)
                    continue

                # When satellite is below min-el, do not track actively.
                if el_u < args.min_el:
                    msg = f"[STATE] ({tracked.name[:18]}) below min-el {args.min_el}°: {el_u:5.1f}°"
                    sys.stdout.write(f"\r{msg:80s}")
                    sys.stdout.flush()

                    emit_status(
                        phase="below_min_el",
                        message=msg,
                        tracked=tracked,
                        desired_norad=desired_norad,
                        desired_sat_str=desired_sat_str,
                        state_has_any_key=state_has_any_key,
                        do_center=False,
                        az=az,
                        el_u=el_u,
                        el_cmd=clamp_el(el_u),
                    )

                    time.sleep(1)
                    continue

                if not hc_busy(ser, args.dummy):
                    need_move = (
                        last_az is None or
                        max(deltadeg_wrap(az, last_az), abs(el_u - (last_el_user or 0.0))) >= args.min_step
                    )
                    if need_move:
                        send_az, send_az_unwrapped, send_el_user = segment_move(
                            current_az_raw=last_az,
                            current_az_unwrapped=az_unwrapped if az_unwrapped is not None else last_az,
                            current_el_user=last_el_user,
                            target_az_raw=az,
                            target_el_user=el_u,
                            max_az_step_deg=args.max_az_step,
                            max_el_step_deg=args.max_el_step,
                        )
                        cmd = f"B{deg_to_hex16(send_az)},{deg_to_hex16(mount_el(send_el_user))}"
                        last_cmd = cmd
                        last_cmd_ts = iso_now()
                        send_cmd(ser, cmd, args.dummy)
                        last_az = send_az
                        az_unwrapped = send_az_unwrapped
                        last_el_user = send_el_user

                    if fallback_tracking:
                        msg = (
                            f"[FALLBACK {desired_norad}] Continuing track: "
                            f"{tracked.name[:18]:18s} | El: {el_u:5.1f}°"
                        )
                    else:
                        msg = f"Tracking: {tracked.name[:18]:18s} | El: {el_u:5.1f}°"
                    sys.stdout.write(f"\r{msg:80s}")
                    sys.stdout.flush()

                    emit_status(
                        phase="tracking",
                        message=msg,
                        tracked=tracked,
                        desired_norad=desired_norad,
                        desired_sat_str=desired_sat_str,
                        state_has_any_key=state_has_any_key,
                        do_center=False,
                        az=az,
                        el_u=el_u,
                        el_cmd=clamp_el(el_u),
                    )
                else:
                    # When HC is busy, at least keep status fresh without moving.
                    if fallback_tracking:
                        msg = (
                            f"HC busy… [FALLBACK {desired_norad}] "
                            f"{tracked.name[:18]:18s} | El {el_u:5.1f}°"
                        )
                    else:
                        msg = f"HC busy… {tracked.name[:18]:18s} | El {el_u:5.1f}°"
                    emit_status(
                        phase="busy",
                        message=msg,
                        tracked=tracked,
                        desired_norad=desired_norad,
                        desired_sat_str=desired_sat_str,
                        state_has_any_key=state_has_any_key,
                        do_center=False,
                        az=az,
                        el_u=el_u,
                        el_cmd=clamp_el(el_u),
                    )

            else:
                msg = "[STATE] No valid target from state.json / not found in TLE..."

                sys.stdout.write(f"\r{msg:80s}")
                sys.stdout.flush()

                emit_status(
                    phase="no_target",
                    message=msg,
                    tracked=tracked,
                    desired_norad=desired_norad,
                    desired_sat_str=desired_sat_str,
                    state_has_any_key=state_has_any_key,
                    do_center=False,
                    az=last_az,
                    el_u=last_el_user,
                    el_cmd=clamp_el(last_el_user) if last_el_user is not None else None,
                )

                time.sleep(1)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nStopped by user.")
        emit_status(
            phase="stopped",
            message="Stopped by user (KeyboardInterrupt).",
            tracked=tracked,
            desired_norad=desired_norad,
            desired_sat_str=desired_sat_str,
            state_has_any_key=state_has_any_key,
            do_center=False,
            az=last_az,
            el_u=last_el_user,
            el_cmd=clamp_el(last_el_user) if last_el_user is not None else None,
            force=True
        )
    finally:
        if ser is not None:
            try:
                send_cmd(ser, 'M', dummy=False)  # stop
            except Exception:
                pass
            ser.close()

if __name__ == '__main__':
    main()
