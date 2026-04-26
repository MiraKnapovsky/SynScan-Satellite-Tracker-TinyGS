#!/usr/bin/env python3
"""Export pass-level NOT_CONFIRMED rows used by Grafana pass-based panels."""

from __future__ import annotations

import argparse
import csv
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set
from zoneinfo import ZoneInfo

from export_influx_csv import BASE_DIR, load_source_config, parse_datetime


CSV_FIELDNAMES = [
    "source",
    "own_bucket",
    "status",
    "confirmed_match_count",
    "satellite",
    "tle_satellite",
    "tracked_norad",
    "pass_key",
    "max_el_deg",
    "max_elevation_bin",
    "pass_aos_unix_s",
    "pass_los_unix_s",
    "pass_aos_utc",
    "pass_los_utc",
    "pass_aos_local",
    "pass_los_local",
    "state_sources",
    "state_buckets",
    "state_source_count",
    "state_row_count",
    "state_first_seen_utc",
    "state_last_seen_utc",
]

SOURCE_ORDER = ["helix", "qfh", "linear"]


@dataclass
class StatePassCandidate:
    tracked_norad: str
    pass_key: str
    satellite: str
    tle_satellite: str
    max_el_deg: float
    pass_aos_s: float
    pass_los_s: float
    state_sources: Set[str] = field(default_factory=set)
    state_buckets: Set[str] = field(default_factory=set)
    state_row_count: int = 0
    state_first_seen_utc: str = ""
    state_last_seen_utc: str = ""


def sanitize_for_filename(value: str) -> str:
    return (
        value.replace(":", "")
        .replace("+", "_plus_")
        .replace("-", "-")
        .replace(" ", "_")
        .replace("T", "_")
    )


def default_output_dir(base_dir: Path, start_local: datetime, stop_local: datetime) -> Path:
    start_label = sanitize_for_filename(start_local.strftime("%Y-%m-%d_%H%M"))
    stop_label = sanitize_for_filename(stop_local.strftime("%Y-%m-%d_%H%M"))
    return base_dir / "exports" / f"{start_label}_to_{stop_label}_{start_local.tzname()}"


def parse_sources(raw_sources: str) -> List[str]:
    requested = [part.strip().lower() for part in raw_sources.split(",") if part.strip()]
    if not requested or requested == ["all"]:
        return SOURCE_ORDER[:]

    unknown = [source for source in requested if source not in SOURCE_ORDER]
    if unknown:
        valid = ", ".join(["all", *SOURCE_ORDER])
        raise ValueError(f"Unknown source(s): {', '.join(sorted(unknown))}. Valid values: {valid}")

    return requested


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export pass-level NOT_CONFIRMED rows into CSV.")
    parser.add_argument("--start", required=True, help="Start timestamp, e.g. '2026-04-16 11:00'")
    parser.add_argument("--stop", required=True, help="Stop timestamp, e.g. '2026-04-18 12:00'")
    parser.add_argument("--timezone", default="Europe/Prague", help="Timezone for naive timestamps and local CSV columns.")
    parser.add_argument(
        "--sources",
        default="all",
        help="Comma-separated source list: helix,qfh,linear or 'all'",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where CSV files will be written. Defaults to synscan_tinygs_tracker/exports/...",
    )
    return parser


def run_influx_raw_query(source_cfg, flux: str) -> Iterator[Dict[str, str]]:
    cmd = [
        "influx",
        "query",
        "--host",
        source_cfg.influx_url,
        "--org",
        source_cfg.influx_org,
        "--token",
        source_cfg.influx_token,
        "--raw",
        flux,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None

    header: Optional[List[str]] = None
    try:
        for line in proc.stdout:
            if not line.strip() or line.startswith("#"):
                continue

            parsed = next(csv.reader([line]))
            if header is None:
                header = parsed
                continue

            yield dict(zip(header, parsed))
    finally:
        proc.stdout.close()

    stderr = proc.stderr.read().strip()
    proc.stderr.close()
    return_code = proc.wait()
    if return_code != 0:
        raise RuntimeError(
            f"Influx query failed for source '{source_cfg.source}' with exit code {return_code}: {stderr}"
        )


def build_state_pass_query(bucket: str, start_utc: str, stop_utc: str) -> str:
    return f"""
from(bucket: "{bucket}")
  |> range(start: {start_utc}, stop: {stop_utc})
  |> filter(fn: (r) => r._measurement == "tinygs_state")
  |> filter(fn: (r) =>
    r._field == "tle_pass_max_el_deg" or
    r._field == "tle_pass_aos_unix_s" or
    r._field == "tle_pass_los_unix_s"
  )
  |> filter(fn: (r) => exists r.tracked_norad)
  |> group()
  |> pivot(
    rowKey: ["_time", "tracked_norad", "satellite", "tle_satellite", "station"],
    columnKey: ["_field"],
    valueColumn: "_value"
  )
  |> filter(fn: (r) => exists r.tle_pass_max_el_deg and exists r.tle_pass_aos_unix_s and exists r.tle_pass_los_unix_s)
  |> keep(columns: [
    "_time",
    "tracked_norad",
    "satellite",
    "tle_satellite",
    "station",
    "tle_pass_max_el_deg",
    "tle_pass_aos_unix_s",
    "tle_pass_los_unix_s"
  ])
  |> sort(columns: ["_time"], desc: false)
""".strip()


def build_confirmed_pass_query(bucket: str, start_utc: str, stop_utc: str) -> str:
    return f"""
from(bucket: "{bucket}")
  |> range(start: {start_utc}, stop: {stop_utc})
  |> filter(fn: (r) => r._measurement == "tinygs_frame")
  |> filter(fn: (r) => r.decode_status == "confirmed")
  |> filter(fn: (r) =>
    r._field == "tle_pass_aos_unix_s" or
    r._field == "tle_pass_los_unix_s"
  )
  |> filter(fn: (r) => exists r.tracked_norad)
  |> group()
  |> pivot(
    rowKey: ["_time", "tracked_norad", "satellite", "tle_satellite", "station"],
    columnKey: ["_field"],
    valueColumn: "_value"
  )
  |> filter(fn: (r) => exists r.tle_pass_aos_unix_s and exists r.tle_pass_los_unix_s)
  |> keep(columns: [
    "_time",
    "tracked_norad",
    "satellite",
    "tle_satellite",
    "station",
    "tle_pass_aos_unix_s",
    "tle_pass_los_unix_s"
  ])
  |> sort(columns: ["_time"], desc: false)
""".strip()


def rounded_pass_second(value: float) -> int:
    return int(value + 0.5) if value >= 0 else int(value - 0.5)


def format_tracked_norad(value: str) -> str:
    return str(int(float(value)))


def build_pass_key(tracked_norad: str, aos_s: float, los_s: float) -> str:
    return f"{tracked_norad}|{rounded_pass_second(aos_s)}|{rounded_pass_second(los_s)}"


def unix_to_utc_iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def utc_to_local_iso(utc_iso: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone(tz).isoformat()


def max_elevation_bin_label(max_el_deg: float) -> str:
    bin_low = int(max_el_deg / 10.0) * 10
    return f"{bin_low}-{bin_low + 9}\N{DEGREE SIGN}"


def collect_state_pass_candidates(source_cfgs, start_utc: str, stop_utc: str) -> Dict[str, StatePassCandidate]:
    candidates: Dict[str, StatePassCandidate] = {}

    for cfg in source_cfgs:
        flux = build_state_pass_query(cfg.influx_bucket, start_utc=start_utc, stop_utc=stop_utc)
        for raw_row in run_influx_raw_query(cfg, flux):
            max_el_deg = float(raw_row["tle_pass_max_el_deg"])
            pass_aos_s = float(raw_row["tle_pass_aos_unix_s"])
            pass_los_s = float(raw_row["tle_pass_los_unix_s"])
            if not (0.0 <= max_el_deg <= 90.0) or pass_los_s < pass_aos_s:
                continue

            tracked_norad = format_tracked_norad(raw_row["tracked_norad"])
            pass_key = build_pass_key(tracked_norad, aos_s=pass_aos_s, los_s=pass_los_s)
            candidate = candidates.get(pass_key)

            if candidate is None:
                candidate = StatePassCandidate(
                    tracked_norad=tracked_norad,
                    pass_key=pass_key,
                    satellite=(raw_row.get("satellite") or "").strip(),
                    tle_satellite=(raw_row.get("tle_satellite") or "").strip(),
                    max_el_deg=max_el_deg,
                    pass_aos_s=pass_aos_s,
                    pass_los_s=pass_los_s,
                    state_first_seen_utc=raw_row.get("_time", ""),
                    state_last_seen_utc=raw_row.get("_time", ""),
                )
                candidates[pass_key] = candidate
            else:
                if max_el_deg > candidate.max_el_deg:
                    candidate.max_el_deg = max_el_deg
                if not candidate.satellite and raw_row.get("satellite"):
                    candidate.satellite = raw_row["satellite"].strip()
                if not candidate.tle_satellite and raw_row.get("tle_satellite"):
                    candidate.tle_satellite = raw_row["tle_satellite"].strip()
                if raw_row.get("_time", "") and raw_row["_time"] < candidate.state_first_seen_utc:
                    candidate.state_first_seen_utc = raw_row["_time"]
                if raw_row.get("_time", "") and raw_row["_time"] > candidate.state_last_seen_utc:
                    candidate.state_last_seen_utc = raw_row["_time"]

            candidate.state_sources.add(cfg.source)
            candidate.state_buckets.add(cfg.influx_bucket)
            candidate.state_row_count += 1

    return candidates


def collect_confirmed_pass_counts(source_cfg, start_utc: str, stop_utc: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    flux = build_confirmed_pass_query(source_cfg.influx_bucket, start_utc=start_utc, stop_utc=stop_utc)
    for raw_row in run_influx_raw_query(source_cfg, flux):
        tracked_norad = format_tracked_norad(raw_row["tracked_norad"])
        pass_aos_s = float(raw_row["tle_pass_aos_unix_s"])
        pass_los_s = float(raw_row["tle_pass_los_unix_s"])
        pass_key = build_pass_key(tracked_norad, aos_s=pass_aos_s, los_s=pass_los_s)
        counts[pass_key] = counts.get(pass_key, 0) + 1
    return counts


def candidate_to_csv_row(
    candidate: StatePassCandidate,
    source_cfg,
    local_tz: ZoneInfo,
    status: str,
    confirmed_match_count: int,
) -> Dict[str, str]:
    pass_aos_utc = unix_to_utc_iso(candidate.pass_aos_s)
    pass_los_utc = unix_to_utc_iso(candidate.pass_los_s)
    return {
        "source": source_cfg.source,
        "own_bucket": source_cfg.influx_bucket,
        "status": status,
        "confirmed_match_count": str(confirmed_match_count),
        "satellite": candidate.satellite,
        "tle_satellite": candidate.tle_satellite,
        "tracked_norad": candidate.tracked_norad,
        "pass_key": candidate.pass_key,
        "max_el_deg": f"{candidate.max_el_deg:.6f}",
        "max_elevation_bin": max_elevation_bin_label(candidate.max_el_deg),
        "pass_aos_unix_s": f"{candidate.pass_aos_s:.6f}",
        "pass_los_unix_s": f"{candidate.pass_los_s:.6f}",
        "pass_aos_utc": pass_aos_utc,
        "pass_los_utc": pass_los_utc,
        "pass_aos_local": utc_to_local_iso(pass_aos_utc, local_tz),
        "pass_los_local": utc_to_local_iso(pass_los_utc, local_tz),
        "state_sources": ",".join(sorted(candidate.state_sources)),
        "state_buckets": ",".join(sorted(candidate.state_buckets)),
        "state_source_count": str(len(candidate.state_sources)),
        "state_row_count": str(candidate.state_row_count),
        "state_first_seen_utc": candidate.state_first_seen_utc,
        "state_last_seen_utc": candidate.state_last_seen_utc,
    }


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    local_tz = ZoneInfo(args.timezone)
    start_local = parse_datetime(args.start, local_tz)
    stop_local = parse_datetime(args.stop, local_tz)
    if stop_local <= start_local:
        raise SystemExit("--stop must be later than --start")

    selected_sources = parse_sources(args.sources)
    all_state_cfgs = [load_source_config(source) for source in SOURCE_ORDER]
    source_cfgs = [load_source_config(source) for source in selected_sources]

    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    stop_utc = stop_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")

    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(BASE_DIR, start_local, stop_local)
    output_dir.mkdir(parents=True, exist_ok=True)

    candidates = collect_state_pass_candidates(all_state_cfgs, start_utc=start_utc, stop_utc=stop_utc)
    confirmed_by_source = {
        cfg.source: collect_confirmed_pass_counts(cfg, start_utc=start_utc, stop_utc=stop_utc)
        for cfg in source_cfgs
    }

    combined_status_path = output_dir / "tinygs_pass_status_all_sources.csv"
    combined_confirmed_path = output_dir / "tinygs_confirmed_passes_all_sources.csv"
    combined_not_confirmed_path = output_dir / "tinygs_not_confirmed_passes_all_sources.csv"
    confirmed_counts_by_source: Dict[str, int] = {cfg.source: 0 for cfg in source_cfgs}
    not_confirmed_counts_by_source: Dict[str, int] = {cfg.source: 0 for cfg in source_cfgs}

    all_status_rows: List[Dict[str, str]] = []
    all_confirmed_rows: List[Dict[str, str]] = []
    all_not_confirmed_rows: List[Dict[str, str]] = []
    for cfg in source_cfgs:
        source_status_rows: List[Dict[str, str]] = []
        source_confirmed_rows: List[Dict[str, str]] = []
        source_not_confirmed_rows: List[Dict[str, str]] = []
        confirmed_counts = confirmed_by_source[cfg.source]
        for candidate in candidates.values():
            confirmed_match_count = confirmed_counts.get(candidate.pass_key, 0)
            status = "CONFIRMED" if confirmed_match_count > 0 else "NOT_CONFIRMED"
            row = candidate_to_csv_row(
                candidate,
                source_cfg=cfg,
                local_tz=local_tz,
                status=status,
                confirmed_match_count=confirmed_match_count,
            )
            source_status_rows.append(row)
            if status == "CONFIRMED":
                source_confirmed_rows.append(row)
            else:
                source_not_confirmed_rows.append(row)

        source_status_rows.sort(key=lambda row: (row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))
        source_confirmed_rows.sort(key=lambda row: (row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))
        source_not_confirmed_rows.sort(key=lambda row: (row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))
        confirmed_counts_by_source[cfg.source] = len(source_confirmed_rows)
        not_confirmed_counts_by_source[cfg.source] = len(source_not_confirmed_rows)

        per_source_status_path = output_dir / f"tinygs_pass_status_{cfg.source}.csv"
        with per_source_status_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(source_status_rows)

        per_source_confirmed_path = output_dir / f"tinygs_confirmed_passes_{cfg.source}.csv"
        with per_source_confirmed_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(source_confirmed_rows)

        per_source_not_confirmed_path = output_dir / f"tinygs_not_confirmed_passes_{cfg.source}.csv"
        with per_source_not_confirmed_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(source_not_confirmed_rows)

        all_status_rows.extend(source_status_rows)
        all_confirmed_rows.extend(source_confirmed_rows)
        all_not_confirmed_rows.extend(source_not_confirmed_rows)

    all_status_rows.sort(key=lambda row: (row["source"], row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))
    all_confirmed_rows.sort(key=lambda row: (row["source"], row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))
    all_not_confirmed_rows.sort(key=lambda row: (row["source"], row["pass_aos_utc"], row["tracked_norad"], row["pass_key"]))

    with combined_status_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_status_rows)

    with combined_confirmed_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_confirmed_rows)

    with combined_not_confirmed_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_not_confirmed_rows)

    print(f"Output directory: {output_dir}")
    print(f"Combined status CSV: {combined_status_path}")
    print(f"Combined CONFIRMED CSV: {combined_confirmed_path}")
    print(f"Combined NOT_CONFIRMED CSV: {combined_not_confirmed_path}")
    print(f"Time range local: {start_local.isoformat()} -> {stop_local.isoformat()}")
    print(f"Time range UTC:   {start_utc} -> {stop_utc}")
    print(f"Shared state pass candidates: {len(candidates)}")
    for cfg in source_cfgs:
        confirmed_pass_key_count = len(confirmed_by_source[cfg.source])
        print(
            f"{cfg.source}: {confirmed_counts_by_source[cfg.source]} CONFIRMED passes, "
            f"{not_confirmed_counts_by_source[cfg.source]} NOT_CONFIRMED passes, "
            f"{confirmed_pass_key_count} confirmed pass keys"
        )
    print(f"total status rows: {len(all_status_rows)}")


if __name__ == "__main__":
    main()
