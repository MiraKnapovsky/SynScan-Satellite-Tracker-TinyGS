#!/usr/bin/env python3
"""Main SynScan tracking loop using TLE prediction and TinyGS state targets."""
# Spouštění (reál):
# python3 synscan_follow_sat.py --port /dev/ttyUSB0 --lat 49.83 --lon 18.17 --alt 240 --tle satellites.tle --state state.json --min-el 10 --interval 0.5 --lead 0.8 --status-file synscan_status.json
#
# Spouštění (dummy test bez montáže):
# python3 synscan_follow_sat.py --dummy --lat 49.83 --lon 18.17 --alt 240 --tle satellites.tle --state state.json --interval 0.5 --lead 0.8 --status-file synscan_status.json

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

from synscan_common import deg_to_hex16, open_port, send_cmd as serial_send_cmd, transform_el

BASE_DIR = Path(__file__).resolve().parent

# ---------- RS-232 ----------
def send_cmd(ser: Optional[serial.Serial], payload: str, dummy: bool) -> str:
    if dummy:
        # V dummy režimu explicitně loguj příkazy pro ladění.
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

# ---------- TLE ZE SOUBORU ----------
def load_tles(path: Path) -> List[EarthSatellite]:
    ts = load.timescale()
    if not path.exists():
        raise FileNotFoundError(f"Soubor {path} nebyl nalezen!")

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

# ---------- STATE.JSON (MQTT) ----------
def read_state(path: Path) -> Tuple[Optional[int], Optional[str]]:
    """Vrací (norad, sat_str) z JSONu; když nejsou, vrací (None, None)."""
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
    # Pouze NORAD mapování (bez fallbacku podle názvu).
    if norad is not None and norad in sats_by_norad:
        return sats_by_norad[norad]
    return None

# ---------- STATUS JSON (pro web) ----------
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

# ---------- HLAVNÍ ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dummy', action='store_true', help='Běh bez montáže: jen vypisuje příkazy')
    ap.add_argument('--port', default=None, help='Např. /dev/ttyUSB0 (není potřeba při --dummy)')

    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lon', type=float, required=True)
    ap.add_argument('--alt', type=float, default=0.0)
    ap.add_argument('--tle', dest='tle_file', required=True)

    ap.add_argument('--state', dest='state_file', default='state.json',
                    help='Cesta k state.json pro NORAD target selection')

    ap.add_argument('--min-el', type=float, default=-10.0)
    ap.add_argument('--interval', type=float, default=0.5)
    ap.add_argument('--lead', type=float, default=0.8)
    ap.add_argument('--min-step', type=float, default=0.10)
    ap.add_argument('--wrap-limit', type=float, default=270.0,
                    help='Soft limit kabelů (stupně od az=0).')
    ap.add_argument('--wrap-margin', type=float, default=10.0,
                    help='Rezerva k wrap-limit (stupně).')
    ap.add_argument('--plan-horizon', type=float, default=2400.0,
                    help='Horizont predikce příštího přeletu (sekundy).')
    ap.add_argument('--plan-step', type=float, default=2.0,
                    help='Krok predikce az/el v pase (sekundy).')
    ap.add_argument('--az-home', type=float, default=0.0,
                    help='Azimut pro bezpečné odmotání před přeletom.')

    ap.add_argument('--center-el', type=float, default=50.0,
                    help='Elevace (uživatelská, ve stupních) pro neutrální pozici když state.json nemá NORAD')

    # --- NOVÉ: status file pro web ---
    ap.add_argument('--status-file', default=None,
                    help='Cesta k JSON souboru, kam se bude zapisovat aktuální stav (pro web UI)')
    ap.add_argument('--status-every', type=float, default=1.0,
                    help='Jak často zapisovat status (sekundy)')

    args = ap.parse_args()

    if not args.dummy and not args.port:
        raise SystemExit("Chybí --port (nebo použij --dummy).")

    tle_path = resolve_runtime_path(args.tle_file)
    state_path = resolve_runtime_path(args.state_file)
    status_path = resolve_runtime_path(args.status_file) if args.status_file else None
    status_last = 0.0

    last_cmd: Optional[str] = None
    last_cmd_ts: Optional[str] = None

    def emit_status(*, phase: str, message: str,
                    tracked: Optional[EarthSatellite],
                    desired_norad: Optional[int],
                    desired_sat_str: Optional[str],
                    state_has_any_key: bool,
                    do_center: bool,
                    az: Optional[float],
                    el_u: Optional[float],
                    el_r: Optional[float],
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
            "message": message,             # lidsky čitelné (pro web)
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

            "state_has_any_key": bool(state_has_any_key),
            "desired_norad": desired_norad,
            "desired_sat": desired_sat_str,

            "tracked_name": tracked.name if tracked else None,
            "tracked_norad": int(tracked.model.satnum) if tracked else None,

            "az_deg": az,
            "el_user_deg": el_u,
            "el_mount_deg": el_r,
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
    print(f"Načteno {len(sats)} satelitů ze souboru {tle_path}")

    # mapování NORAD -> satelit (Skyfield: sat.model.satnum)
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
    state_has_any_key = False  # jestli state obsahuje platný NORAD

    emit_status(
        phase="starting",
        message="Startuji…",
        tracked=tracked,
        desired_norad=desired_norad,
        desired_sat_str=desired_sat_str,
        state_has_any_key=state_has_any_key,
        do_center=False,
        az=last_az,
        el_u=last_el_user,
        el_r=transform_el(last_el_user) if last_el_user is not None else None,
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
                            f"\n[STATE] Cíl: NORAD={desired_norad} -> TLE='{desired_sat_obj.name}' "
                            f"(state sat='{desired_sat_str}')"
                        )
                    else:
                        print(
                            f"\n[STATE] Cíl nenalezen v TLE: NORAD={desired_norad} "
                            f"(state sat='{desired_sat_str}')"
                        )
                else:
                    print(
                        f"\n[STATE] state.json nemá NORAD -> surveillance/neutral režim "
                        f"(Az={args.az_home}°, El={args.center_el}°)"
                    )

            # --- výběr cíle ---
            target_sat: Optional[EarthSatellite] = None
            do_center = False

            if desired_norad is None:
                tracked = None
                do_center = True
            else:
                target_sat = desired_sat_obj
                if target_sat is None:
                    tracked = None

            # --- CENTER režim: neutrální pozice az_home + center_el ---
            if do_center:
                az = float(args.az_home)
                el_u = float(args.center_el)
                el_r = transform_el(el_u)

                if not hc_busy(ser, args.dummy):
                    need_move = (
                        last_az is None or
                        last_el_user is None or
                        max(deltadeg_wrap(az, last_az), abs(el_u - last_el_user)) >= args.min_step
                    )
                    if need_move:
                        cmd = f"B{deg_to_hex16(az)},{deg_to_hex16(el_r)}"
                        last_cmd = cmd
                        last_cmd_ts = iso_now()
                        send_cmd(ser, cmd, args.dummy)
                        last_az, az_unwrapped = update_unwrapped(last_az, az_unwrapped, az)
                        last_el_user = el_u

                    sys.stdout.write(
                        f"\r[SURVEILLANCE] Neutral Az: {az:6.1f}° | El: {el_u:5.1f}° -> Montáž: {el_r:5.2f}°     "
                    )
                    sys.stdout.flush()

                emit_status(
                    phase="center",
                    message=f"[SURVEILLANCE] Neutral Az {az:.1f}° | El {el_u:.1f}° -> Montáž {el_r:.2f}°",
                    tracked=tracked,
                    desired_norad=desired_norad,
                    desired_sat_str=desired_sat_str,
                    state_has_any_key=state_has_any_key,
                    do_center=True,
                    az=az,
                    el_u=el_u,
                    el_r=el_r,
                )

                time.sleep(args.interval)
                continue

            # --- Sledování a pohyb ---
            if target_sat:
                tracked = target_sat
                el_u, az = altaz_deg(tracked, observer, t_target)
                el_r = transform_el(el_u)

                # --- wrap planning / unwind (pouze když satelit není nad min-el) ---
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
                    curr_unwrapped = az_unwrapped if az_unwrapped is not None else last_az
                    if plan_ok and plan_start_unwrapped is not None and curr_unwrapped is not None:
                        desired_start = plan_start_unwrapped + plan_k * 360.0
                        if abs(desired_start - curr_unwrapped) > 180.0:
                            need_unwind = True

                    if need_unwind:
                        unwind_active = True
                        if not hc_busy(ser, args.dummy):
                            if last_az is None or abs(shortest_delta(args.az_home, last_az)) > 1.0:
                                unwind_el_user = last_el_user if last_el_user is not None else float(args.center_el)
                                unwind_el_mount = transform_el(unwind_el_user)
                                cmd = f"B{deg_to_hex16(args.az_home)},{deg_to_hex16(unwind_el_mount)}"
                                last_cmd = cmd
                                last_cmd_ts = iso_now()
                                send_cmd(ser, cmd, args.dummy)
                                last_az, az_unwrapped = update_unwrapped(last_az, az_unwrapped, args.az_home)
                                last_el_user = unwind_el_user
                    else:
                        unwind_active = False

                # Když satelit ještě není nad horizontem, jen čekej.
                if el_u < 0:
                    msg = f"({tracked.name}) Čekám na východ... ({el_u:5.1f}°)"
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
                        el_r=el_r,
                    )

                    time.sleep(1)
                    continue

                # Když satelit není nad min-el, neotáčej.
                if el_u < args.min_el:
                    msg = f"[STATE] ({tracked.name[:18]}) pod min-el {args.min_el}°: {el_u:5.1f}°"
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
                        el_r=el_r,
                    )

                    time.sleep(1)
                    continue

                if not hc_busy(ser, args.dummy):
                    need_move = (
                        last_az is None or
                        max(deltadeg_wrap(az, last_az), abs(el_u - (last_el_user or 0.0))) >= args.min_step
                    )
                    if need_move:
                        cmd = f"B{deg_to_hex16(az)},{deg_to_hex16(el_r)}"
                        last_cmd = cmd
                        last_cmd_ts = iso_now()
                        send_cmd(ser, cmd, args.dummy)
                        last_az, az_unwrapped = update_unwrapped(last_az, az_unwrapped, az)
                        last_el_user = el_u

                    msg = f"Sleduji: {tracked.name[:18]:18s} | El: {el_u:5.1f}° -> Montáž: {el_r:5.2f}°"
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
                        el_r=el_r,
                    )
                else:
                    # když je HC busy, aspoň piš status (bez pohybu)
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
                        el_r=el_r,
                    )

            else:
                msg = "[STATE] Žádný platný cíl z state.json / nenalezen v TLE..."

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
                    el_r=transform_el(last_el_user) if last_el_user is not None else None,
                )

                time.sleep(1)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nUkončeno uživatelem.")
        emit_status(
            phase="stopped",
            message="Ukončeno uživatelem (KeyboardInterrupt).",
            tracked=tracked,
            desired_norad=desired_norad,
            desired_sat_str=desired_sat_str,
            state_has_any_key=state_has_any_key,
            do_center=False,
            az=last_az,
            el_u=last_el_user,
            el_r=transform_el(last_el_user) if last_el_user is not None else None,
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
