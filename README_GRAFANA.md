# Grafana Dashboard Guide (TinyGS / Helix Rotator 433MHz)

This document explains what is in the Grafana dashboard and how each chart is calculated.

Dashboard file:
- `dashboards/tinygs-overview.json`

Provisioned Grafana target:
- `/var/lib/grafana/dashboards/tinygs-overview.json`

Current dashboard metadata:
- Title: `Helix Rotator 433MHz`
- UID: `tinygs-overview`
- Default time range: `now-12h` to `now`
- Refresh: every `30s`

## 1) Data Model

The dashboard reads from one InfluxDB bucket (usually `tinygs`) and uses these measurements:

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

These three are pass-based, not packet-based.

## 4) Pass Logic (Used by pass-based panels)

Pass ID is derived per satellite from ordered `tinygs_state` timeline:

- First state point starts pass `0`.
- A new pass starts when gap between consecutive state points is `> 900s` (15 min).

Then each pass gets:

- A status:
  - `CONFIRMED` if at least one confirmed frame exists in that pass.
  - `NOT_CONFIRMED` otherwise.

- A max elevation bin:
  - Base from `tinygs_state.sat_el_deg`.
  - Frame-preferred elevation enhancement:
    - Maximum of state max elevation and frame max elevation for that pass.

- A frequency band (for frequency success table):
  - Taken from pass state frequency and grouped into 10 MHz bands (`400-409 MHz`, etc.).

Satellite catalog scope used in these panels:
- Catalog is all-time unique satellites with confirmed history.
- Built from `tinygs_frame` confirmed records using `range(start: 0, stop: now())`.

## 5) How NOT_CONFIRMED is Defined

`NOT_CONFIRMED` is not "CRC count".

For pass-based panels it means:
- A pass exists in state timeline for a catalog satellite,
- but no confirmed frame is present in that same pass.

So:
- `NOT_CONFIRMED` comes from pass tracking (`begine`/state), not from CRC errors.

## 6) Probability Formula

`Confirmed Reception Probability by Max Pass Elevation` computes per elevation bin:

- `probability = CONFIRMED / (CONFIRMED + NOT_CONFIRMED) * 100`

It uses the same pass set and same `CONFIRMED/NOT_CONFIRMED` definition as:
- `Passes by max elevation bin (begine, frame-preferred elevation)`

## 7) Why Some Counts Can Differ

Counts can differ between packet panels and pass panels because:

- Packet panels count frames.
- Pass panels count passes.
- Unique satellite stats count satellites.
- Time range affects state and frame events, while catalog may be all-time.
- Ingest filters (dedupe, no-satellite CRC drop, max slant range) remove records before storage.

## 8) Editing and Deploying Dashboard

Recommended workflow:

1. Edit:
   - `synscan_tinygs_tracker/dashboards/tinygs-overview.json`
2. Validate JSON:
   - `python3 -m json.tool synscan_tinygs_tracker/dashboards/tinygs-overview.json > /dev/null`
3. Increment `version` in dashboard JSON.
4. Deploy to Grafana provisioning path:
   - `sudo cp synscan_tinygs_tracker/dashboards/tinygs-overview.json /var/lib/grafana/dashboards/tinygs-overview.json`
5. Refresh Grafana dashboard page.

## 9) Quick Troubleshooting

- `unique satellites CONFIRMED (all-time)` is empty:
  - Check listener writes `tinygs_meta`.
  - Check `confirmed_satellites.json` persistence and service permissions.

- Pass charts look too low/high:
  - Verify selected time range contains enough `tinygs_state` points.
  - Confirm dedupe and max slant range settings are expected.

- Frequency in packet table looks unexpected:
  - It is joined from latest known state frequency for that satellite (not necessarily exact frequency at that packet timestamp).
