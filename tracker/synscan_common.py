#!/usr/bin/env python3
"""Shared SynScan helpers for serial I/O and angle handling."""

from __future__ import annotations

import time
from typing import Optional, Tuple

import serial

def open_port(port: str) -> serial.Serial:
    return serial.Serial(
        port=port,
        baudrate=9600,
        bytesize=8,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1.2,
        write_timeout=1.2,
    )


def read_until_hash(ser: serial.Serial) -> str:
    buf = bytearray()
    t0 = time.monotonic()
    while True:
        byte = ser.read(1)
        if byte:
            buf += byte
            if byte == b"#":
                break
        if time.monotonic() - t0 > ser.timeout:
            break
    return buf.decode("ascii", errors="ignore")


def send_cmd(ser: Optional[serial.Serial], payload: str, dummy: bool = False) -> str:
    if dummy or ser is None:
        return ""
    ser.write(payload.encode("ascii"))
    ser.flush()
    return read_until_hash(ser)


def deg_to_hex16(angle_deg: float) -> str:
    angle = angle_deg % 360.0
    value = int(round(angle * 65536.0 / 360.0)) & 0xFFFF
    return f"{value:04X}"


def clamp_el(angle_deg: float) -> float:
    return max(0.0, min(90.0, angle_deg))


def apply_elevation_offset(angle_deg: float, offset_deg: float = 0.0) -> float:
    angle = clamp_el(angle_deg)
    offset = float(offset_deg) * max(0.0, 1.0 - (angle / 90.0))
    return clamp_el(angle + offset)


def user_el_to_mount_el(
    angle_deg: float,
    *,
    invert_elevation: bool = False,
    elevation_offset_deg: float = 0.0,
) -> float:
    angle = apply_elevation_offset(angle_deg, elevation_offset_deg)
    if invert_elevation:
        return 90.0 - angle
    return angle


def goto_azel(
    ser: Optional[serial.Serial],
    az_deg: float,
    el_deg: float,
    dummy: bool = False,
    invert_elevation: bool = False,
    elevation_offset_deg: float = 0.0,
) -> Tuple[str, bool]:
    el_send = user_el_to_mount_el(
        el_deg,
        invert_elevation=invert_elevation,
        elevation_offset_deg=elevation_offset_deg,
    )
    cmd = f"B{deg_to_hex16(az_deg)},{deg_to_hex16(el_send)}"
    rsp = send_cmd(ser, cmd, dummy=dummy)
    ok = dummy or rsp.endswith("#")
    return cmd, ok
