# Grafana Dashboard Guide (TinyGS / Helix Rotator 433MHz)

This document explains what is in the Grafana dashboard and how each chart is calculated.

Core dashboard files:
- `dashboards/tinygs-overview.json`
- `dashboards/tinygs-qfh-overview.json`
- `dashboards/tinygs-linear-overview.json`

Filtered dashboard variants are also included for `no-tianqi`, `only-tianqi`, and `common-no-tianqi` views of each station.

Provisioned Grafana target:
- `/var/lib/grafana/dashboards/tinygs-overview.json`
- `/var/lib/grafana/dashboards/tinygs-qfh-overview.json`
- `/var/lib/grafana/dashboards/tinygs-linear-overview.json`

Current dashboard metadata:
- Title: `Helix Rotátor 433MHz`
- UID: `tinygs-overview`
- Version: `111`
- Default time range: `now-12h` to `now`
- Refresh: every `30s`

QFH dashboard metadata:
- Title: `QFH 433MHz`
- UID: `tinygs-qfh-overview`
- Version: `96`
- Bucket: `tinygs_kna0047qfh`

Linear dashboard metadata:
- Title: `Linear 433MHz`
- UID: `tinygs-linear-overview`
- Version: `6`
- Bucket: `tinygs_kna0047linear`

## 1) Data Model

The Helix dashboard reads from bucket `tinygs`.
The QFH dashboard reads from bucket `tinygs_kna0047qfh`.
The Linear dashboard reads from bucket `tinygs_kna0047linear`.

All dashboards use these measurements:

- `tinygs_state`
  - Source: TinyGS `cmnd/begine` messages.
  - Typical fields: `freq`, `bw`, `sf`, `cr`, `sat_el_deg`, `slant_range_km`, `state_seen`.
  - Used for pass tracking and radio configuration context.

- `tinygs_frame`
  - Source: TinyGS `cmnd/frame/0` messages.
  - Typical fields: `confirmed`, `crc_error`, `snr_db`, `rssi_db`, `freq_error_hz`, `sat_el_deg`, `slant_range_km`.
  - Tag `decode_status` is typically `confirmed` or `crc_error`.

- `tinygs_meta`
  - Source: listener-maintained metadata.
  - Field: `unique_confirmed_all`.
  - Used by the all-time unique satellites stat.

## 2) Ingest and Quality Rules (Important for Interpretation)

These rules are applied before data reaches charts:

- CRC frame without satellite name is dropped.
- Duplicate frames from the same satellite within `5s` are dropped (configurable).
- Frames with computed `slant_range_km > 5000` are dropped (configurable).
- A persistent catalog of unique confirmed satellites is maintained in:
  - `confirmed_satellites.json`
- The all-time count is written to `tinygs_meta.unique_confirmed_all`.

This means dashboard values are already filtered and deduplicated at ingest.

## 3) Panel Inventory and Meaning

### A) Health and high-level counters

- `Station status`
  - Shows online if any `tinygs_state` record exists in last 5 minutes.

- `Last CONFIRMED message`
  - Time since newest confirmed frame.

- `unique satellites CONFIRMED (time frame)`
  - Unique satellites with at least one confirmed frame in selected dashboard time range.

- `unique satellites CONFIRMED (all-time)`
  - Latest `tinygs_meta.unique_confirmed_all` value (not limited by selected time range).

- `crc_error messages`
  - Count of CRC error frames in selected time range.

- `Satellites by CONFIRMED messages`
  - Pie chart of confirmed frame share by satellite.

### B) Packet-level inspection

- `Received packets detail`
  - Last 200 frame rows with:
    - receive time, satellite, frequency, frequency error, SNR, RSSI, distance, elevation, status
  - `Frequency` is joined from last known `tinygs_state.freq` for that satellite (last 30 days).

- `RX map (satellite position at receive time)`
  - Geomap of confirmed receive points using satellite lat/lon at reception time.

### C) Distributions

- `SNR histogram - CONFIRMED`
- `SNR histogram - CRC ERROR`
- `RSSI histogram - CONFIRMED`
- `RSSI histogram - CRC ERROR`
- `Freq error histogram - CONFIRMED`
- `Freq error histogram - CRC ERROR`

These show value distributions by decode status in the selected time range.

### D) Quality summary table

- `RX quality summary (avg/median)`
  - Rows: `ALL`, `CONFIRMED`, `CRC ERROR`
  - Metrics:
    - `Packets`
    - `SNR avg/median [dB]`
    - `RSSI avg/median [dB]`
    - `|Freq error| avg/median [Hz]`
    - `|Freq error| avg/median [% BW]`
    - `BW median [kHz]`
  - Key details:
    - Frequency error is absolute (`abs(freq_error_hz)`), so sign is ignored.
    - `% BW` formula:
      - `abs_freq_error_hz / (bw_khz * 1000) * 100`
    - Bandwidth join priority:
      1. Exact satellite match from recent `tinygs_state` (`-30d`)
      2. Satellite family fallback (prefix before `-`)
      3. Default fallback `125 kHz`

### E) Elevation and distance relationships (CONFIRMED only)

- `SNR vs Elevation (CONFIRMED, median, 5deg bins)`
- `CONFIRMED packets by elevation (5deg bins)`
- `SNR vs Distance (CONFIRMED, 100 km bins)`
- `CONFIRMED packets by distance (100 km bins)`
- `RSSI vs Elevation (CONFIRMED, median, 5deg bins)`
- `RSSI vs Distance (CONFIRMED, 100 km bins)`

These charts bin confirmed frames and then show either packet count or median quality in each bin.

### F) Pass-based analytics

- `Success by frequency band`
- `Passes by max elevation bin (begine, frame-preferred elevation)`
- `Confirmed Reception Probability by Max Pass Elevation`
- `TEMP: NOT_CONFIRMED passes detail`

These panels are pass-based, not packet-based.

## 4) Pass Logic (Used by pass-based panels)

For max-elevation pass panels (`Passes by max elevation bin`, `Confirmed Reception Probability`, and `TEMP: ... detail`), pass identity is built from TLE pass window fields, not from timeline gaps.

Pass candidate source:
- `tinygs_state` with fields:
  - `tle_pass_max_el_deg`
  - `tle_pass_aos_unix_s`
  - `tle_pass_los_unix_s`
- Requires `tracked_norad`.

Pass key:
- `pass_key = NORAD + "|" + int(tle_pass_aos_unix_s) + "|" + int(tle_pass_los_unix_s)`
- Integer seconds are truncation (`int(...)` in Flux).

Per-pass values:
- `max_el_deg` comes from `tinygs_state.tle_pass_max_el_deg`.
- `AOS/LOS` come from `tinygs_state.tle_pass_aos_unix_s` / `tle_pass_los_unix_s` and are shown as UTC time.

Pass status:
- `CONFIRMED` if at least one `tinygs_frame` row with `decode_status == confirmed` maps to the same pass key (`NORAD|AOS|LOS`) using frame `tle_pass_aos_unix_s` and `tle_pass_los_unix_s`.
- `NOT_CONFIRMED` otherwise.

Notes:
- The panel title `...frame-preferred elevation` is legacy naming; current logic uses TLE pass max elevation from state.
- `Success by frequency band` is still pass-based, but uses its own frequency-band pass logic.

## 5) How NOT_CONFIRMED is Defined

`NOT_CONFIRMED` is not "CRC count".

For pass-based panels it means:
- A TLE-defined pass exists in state data for a tracked NORAD in selected time range,
- but no confirmed frame maps to the same TLE pass key.

So:
- `NOT_CONFIRMED` comes from TLE pass tracking in state data, not from CRC errors.

## 6) Probability Formula

`Confirmed Reception Probability by Max Pass Elevation` computes per elevation bin:

- `probability = CONFIRMED / (CONFIRMED + NOT_CONFIRMED) * 100`

It uses the same pass set and same `CONFIRMED/NOT_CONFIRMED` definition as:
- `Passes by max elevation bin (begine, frame-preferred elevation)`
- `TEMP: NOT_CONFIRMED passes detail`

## 7) Why Some Counts Can Differ

Counts can differ between packet panels and pass panels because:

- Packet panels count frames.
- Pass panels count passes.
- Unique satellite stats count satellites.
- Time range affects both state pass candidates and confirmed frame matches.
- Ingest filters (dedupe, no-satellite CRC drop, max slant range) remove records before storage.
- TLE AOS/LOS values can drift slightly; near integer-second boundaries this can occasionally split one physical flyby into two adjacent pass keys.

## 8) Editing and Deploying Dashboard

Recommended workflow:

1. Edit:
   - `synscan_tinygs_tracker/dashboards/tinygs-overview.json`
   - `synscan_tinygs_tracker/dashboards/tinygs-qfh-overview.json`
   - `synscan_tinygs_tracker/dashboards/tinygs-linear-overview.json`
2. Validate JSON:
   - `python3 -m json.tool synscan_tinygs_tracker/dashboards/tinygs-overview.json > /dev/null`
   - `python3 -m json.tool synscan_tinygs_tracker/dashboards/tinygs-qfh-overview.json > /dev/null`
   - `python3 -m json.tool synscan_tinygs_tracker/dashboards/tinygs-linear-overview.json > /dev/null`
3. Increment `version` in dashboard JSON.
4. Deploy to Grafana provisioning path:
   - `sudo cp synscan_tinygs_tracker/dashboards/tinygs-overview.json /var/lib/grafana/dashboards/tinygs-overview.json`
   - `sudo cp synscan_tinygs_tracker/dashboards/tinygs-qfh-overview.json /var/lib/grafana/dashboards/tinygs-qfh-overview.json`
   - `sudo cp synscan_tinygs_tracker/dashboards/tinygs-linear-overview.json /var/lib/grafana/dashboards/tinygs-linear-overview.json`
5. Refresh Grafana dashboard page.

## 9) Quick Troubleshooting

- `unique satellites CONFIRMED (all-time)` is empty:
  - Check listener writes `tinygs_meta`.
  - Check `confirmed_satellites.json` persistence and service permissions.

- Pass charts look too low/high:
  - Verify selected time range contains enough `tinygs_state` points with `tle_pass_*` fields.
  - Verify confirmed frames include `tracked_norad`, `tle_pass_aos_unix_s`, and `tle_pass_los_unix_s` for pass matching.
  - Confirm dedupe and max slant range settings are expected.

- Frequency in packet table looks unexpected:
  - It is joined from latest known state frequency for that satellite (not necessarily exact frequency at that packet timestamp).
