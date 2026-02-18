#!/usr/bin/env python3
"""Interactive manual SynScan control for quick Az/El commands and stop."""

# python3 synscan_manual.py --port /dev/ttyUSB0
# Mapování elevace:
#   Uživatel 0° (Horizont) -> Montáž 84.0°
#   Uživatel 90° (Zenit)   -> Montáž 0.0°

import argparse

import serial

from synscan_common import REAL_END, REAL_START, goto_azel, open_port, send_cmd, transform_el


def handshake(ser: serial.Serial) -> None:
    rsp = send_cmd(ser, "Ka")
    print(f"Echo: {repr(rsp) or '—'}")
    rsp = send_cmd(ser, "V")
    print(f"HC version: {rsp.strip('#') or '—'}")


def interactive(ser: serial.Serial) -> None:
    print("\nKorekce elevace:")
    print(f"  Zadáš 0°  -> Montáž jede na {REAL_START}°")
    print(f"  Zadáš 90° -> Montáž jede na {REAL_END}°")

    last_az = 0.0

    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nKonec.")
            return

        if not line:
            continue

        low = line.lower()
        if low in ("exit", "quit", "q"):
            return
        if low == "stop":
            send_cmd(ser, "M")
            print("Zastaveno.")
            continue

        try:
            parts = [part for part in line.replace(",", " ").split() if part]
            if len(parts) != 2:
                print("Zadej dvě čísla: Az El")
                continue

            last_az = float(parts[0]) % 360.0
            user_el = float(parts[1])
            real_el = transform_el(user_el)
            _, ok = goto_azel(ser, last_az, real_el)

            print(f"Jede na: Az={last_az:.2f}°, El_vstup={user_el:.2f}°")
            print(f"REÁLNĚ POSLÁNO: El_real={real_el:.2f}°")
            if not ok:
                print("Varování: montáž nepotvrdila příkaz (#).")
        except ValueError:
            print("Chyba v zadání čísla.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    args = parser.parse_args()

    try:
        with open_port(args.port) as ser:
            handshake(ser)
            interactive(ser)
    except Exception as exc:
        print(f"Chyba: {exc}")


if __name__ == "__main__":
    main()
