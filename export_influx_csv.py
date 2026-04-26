#!/usr/bin/env python3
"""Export TinyGS frame telemetry from local InfluxDB buckets into CSV files."""

from __future__ import annotations

import argparse
import csv
import heapq
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent

ENV_FILES = {
    "active": BASE_DIR / "mqtt_tinygs_listen.env",
    "passive1": BASE_DIR / "mqtt_tinygs_listen_passive1.env",
    "passive2": BASE_DIR / "mqtt_tinygs_listen_passive2.env",
}

CSV_FIELDNAMES = [
    "source",
    "bucket",
    "station",
    "received_utc",
    "received_local",
    "status",
    "satellite",
    "tle_satellite",
    "tracked_norad",
    "payload_bytes",
    "freq_error_hz",
    "snr_db",
    "rssi_db",
    "elevation_deg",
    "distance_km",
    "latitude",
    "longitude",
    "tle_pass_max_el_deg",
    "tle_pass_aos_unix_s",
    "tle_pass_culm_unix_s",
    "tle_pass_los_unix_s",
    "tle_pass_duration_s",
    "user",
    "channel",
    "cmd",
    "subcmd",
]

RAW_TO_CSV = {
    "_time": "received_utc",
    "decode_status": "status",
    "sat_el_deg": "elevation_deg",
    "slant_range_km": "distance_km",
    "sat_lat_deg": "latitude",
    "sat_lon_deg": "longitude",
}


@dataclass(frozen=True)
class SourceConfig:
    source: str
    env_path: Path
    influx_url: str
    influx_org: str
    influx_bucket: str
    influx_token: str
    station: str


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def load_source_config(source: str) -> SourceConfig:
    env_path = ENV_FILES[source]
    if not env_path.exists():
        raise FileNotFoundError(f"Missing env file for source '{source}': {env_path}")

    env = parse_env_file(env_path)
    required = ["INFLUXDB_URL", "INFLUXDB_ORG", "INFLUXDB_BUCKET", "INFLUXDB_TOKEN", "TINYGS_STATION"]
    missing = [key for key in required if not env.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Env file {env_path} is missing required keys: {joined}")

    return SourceConfig(
        source=source,
        env_path=env_path,
        influx_url=env["INFLUXDB_URL"],
        influx_org=env["INFLUXDB_ORG"],
        influx_bucket=env["INFLUXDB_BUCKET"],
        influx_token=env["INFLUXDB_TOKEN"],
        station=env["TINYGS_STATION"],
    )


def parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return dt.astimezone(tz)

    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def build_flux_query(bucket: str, start_utc: str, stop_utc: str) -> str:
    return f"""
from(bucket: "{bucket}")
  |> range(start: {start_utc}, stop: {stop_utc})
  |> filter(fn: (r) => r._measurement == "tinygs_frame")
  |> filter(fn: (r) => exists r.satellite and exists r.decode_status)
  |> filter(fn: (r) =>
    r._field == "freq_error_hz" or
    r._field == "payload_bytes" or
    r._field == "rssi_db" or
    r._field == "sat_el_deg" or
    r._field == "sat_lat_deg" or
    r._field == "sat_lon_deg" or
    r._field == "slant_range_km" or
    r._field == "snr_db" or
    r._field == "tracked_norad" or
    r._field == "tle_pass_aos_unix_s" or
    r._field == "tle_pass_culm_unix_s" or
    r._field == "tle_pass_duration_s" or
    r._field == "tle_pass_los_unix_s" or
    r._field == "tle_pass_max_el_deg"
  )
  |> group()
  |> pivot(
    rowKey: ["_time", "user", "station", "channel", "cmd", "subcmd", "satellite", "tle_satellite", "decode_status"],
    columnKey: ["_field"],
    valueColumn: "_value"
  )
  |> keep(columns: [
    "_time",
    "user",
    "station",
    "channel",
    "cmd",
    "subcmd",
    "satellite",
    "tle_satellite",
    "decode_status",
    "freq_error_hz",
    "payload_bytes",
    "rssi_db",
    "sat_el_deg",
    "sat_lat_deg",
    "sat_lon_deg",
    "slant_range_km",
    "snr_db",
    "tracked_norad",
    "tle_pass_aos_unix_s",
    "tle_pass_culm_unix_s",
    "tle_pass_duration_s",
    "tle_pass_los_unix_s",
    "tle_pass_max_el_deg"
  ])
  |> sort(columns: ["_time"], desc: false)
""".strip()


def iter_influx_raw_rows(source_cfg: SourceConfig, start_utc: str, stop_utc: str) -> Iterator[Dict[str, str]]:
    flux = build_flux_query(source_cfg.influx_bucket, start_utc=start_utc, stop_utc=stop_utc)
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


def to_local_timestamp(received_utc: str, tz: ZoneInfo) -> str:
    return datetime.fromisoformat(received_utc.replace("Z", "+00:00")).astimezone(tz).isoformat()


def transform_row(raw_row: Dict[str, str], source_cfg: SourceConfig, local_tz: ZoneInfo) -> Dict[str, str]:
    received_utc = raw_row.get("_time", "")
    row = {field: "" for field in CSV_FIELDNAMES}
    row["source"] = source_cfg.source
    row["bucket"] = source_cfg.influx_bucket
    row["station"] = raw_row.get("station", source_cfg.station)
    row["received_utc"] = received_utc
    row["received_local"] = to_local_timestamp(received_utc, local_tz) if received_utc else ""

    for raw_key, value in raw_row.items():
        if raw_key in ("", "result", "table"):
            continue
        csv_key = RAW_TO_CSV.get(raw_key, raw_key)
        if csv_key in row and value != "":
            row[csv_key] = value

    return row


def iter_source_rows(
    source_cfg: SourceConfig,
    start_utc: str,
    stop_utc: str,
    local_tz: ZoneInfo,
    per_source_writer: csv.DictWriter,
) -> Iterator[Dict[str, str]]:
    for raw_row in iter_influx_raw_rows(source_cfg, start_utc=start_utc, stop_utc=stop_utc):
        row = transform_row(raw_row, source_cfg=source_cfg, local_tz=local_tz)
        per_source_writer.writerow(row)
        yield row


def merge_sorted_iterators(iterators: Iterable[Iterator[Dict[str, str]]]) -> Iterator[Dict[str, str]]:
    heap: List[tuple[str, int, Dict[str, str], Iterator[Dict[str, str]]]] = []
    for index, iterator in enumerate(iterators):
        try:
            first_row = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (first_row["received_utc"], index, first_row, iterator))

    while heap:
        _, index, row, iterator = heapq.heappop(heap)
        yield row
        try:
            next_row = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (next_row["received_utc"], index, next_row, iterator))


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
    if not requested:
        return list(ENV_FILES)
    if requested == ["all"]:
        return list(ENV_FILES)

    unknown = [source for source in requested if source not in ENV_FILES]
    if unknown:
        joined = ", ".join(sorted(unknown))
        valid = ", ".join(sorted(["all", *ENV_FILES.keys()]))
        raise ValueError(f"Unknown source(s): {joined}. Valid values: {valid}")

    return requested


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export TinyGS InfluxDB frame data into CSV.")
    parser.add_argument("--start", required=True, help="Start timestamp, e.g. '2026-04-16 11:00'")
    parser.add_argument("--stop", required=True, help="Stop timestamp, e.g. '2026-04-18 12:00'")
    parser.add_argument("--timezone", default="Europe/Prague", help="Timezone for naive timestamps and local CSV column.")
    parser.add_argument(
        "--sources",
        default="all",
        help="Comma-separated source list: active,passive1,passive2 or 'all'",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where CSV files will be written. Defaults to synscan_tinygs_tracker/exports/...",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    local_tz = ZoneInfo(args.timezone)
    start_local = parse_datetime(args.start, local_tz)
    stop_local = parse_datetime(args.stop, local_tz)
    if stop_local <= start_local:
        raise SystemExit("--stop must be later than --start")

    selected_sources = parse_sources(args.sources)
    source_cfgs = [load_source_config(source) for source in selected_sources]

    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    stop_utc = stop_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")

    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(BASE_DIR, start_local, stop_local)
    output_dir.mkdir(parents=True, exist_ok=True)

    combined_path = output_dir / "tinygs_all_sources.csv"
    counts: Dict[str, int] = {cfg.source: 0 for cfg in source_cfgs}

    per_source_files = {}
    per_source_writers = {}
    iterators: List[Iterator[Dict[str, str]]] = []
    try:
        for cfg in source_cfgs:
            path = output_dir / f"tinygs_{cfg.source}.csv"
            handle = path.open("w", encoding="utf-8", newline="")
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            per_source_files[cfg.source] = handle
            per_source_writers[cfg.source] = writer
            iterators.append(
                iter_source_rows(
                    source_cfg=cfg,
                    start_utc=start_utc,
                    stop_utc=stop_utc,
                    local_tz=local_tz,
                    per_source_writer=writer,
                )
            )

        with combined_path.open("w", encoding="utf-8", newline="") as combined_handle:
            combined_writer = csv.DictWriter(combined_handle, fieldnames=CSV_FIELDNAMES)
            combined_writer.writeheader()

            for row in merge_sorted_iterators(iterators):
                combined_writer.writerow(row)
                counts[row["source"]] += 1
    finally:
        for handle in per_source_files.values():
            handle.close()

    total_rows = sum(counts.values())
    print(f"Output directory: {output_dir}")
    print(f"Combined CSV: {combined_path}")
    print(f"Time range local: {start_local.isoformat()} -> {stop_local.isoformat()}")
    print(f"Time range UTC:   {start_utc} -> {stop_utc}")
    for cfg in source_cfgs:
        print(f"{cfg.source}: {counts[cfg.source]} rows")
    print(f"total: {total_rows} rows")


if __name__ == "__main__":
    main()
