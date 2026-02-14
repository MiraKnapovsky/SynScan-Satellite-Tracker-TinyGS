# SynScan Satellite Tracker (TinyGS + SynScan)

This project tracks satellites with a SynScan mount, using TinyGS MQTT state updates and local TLE data.

## Components

- `synscan_follow_sat.py`: main tracker loop (select target, compute az/el, send mount commands).
- `synscan_runner.py`: validates `synscan_config.json` and starts tracker with safe arguments.
- `synscan_web.py`: Flask UI/API for config, service control, status, and manual goto/stop.
- `mqtt_tinygs_listen.py`: subscribes to TinyGS MQTT topics and writes `state.json` + optional JSONL logs.
- `import_requests.py`: downloads supported TinyGS satellites and writes `satellites.tle`.
- `synscan_manual.py`: interactive manual mount control.
- `synscan_common.py`: shared serial protocol + az/el conversion utilities.

## Runtime Files

- `synscan_config.json`: tracker configuration used by `synscan_runner.py`.
- `state.json`: latest TinyGS state (updated by MQTT listener).
- `synscan_status.json`: live tracker status (updated by `synscan_follow_sat.py`).
- `satellites.tle`: TLE dataset used for tracking.
- `all_rx.jsonl`: optional frame packet archive.

## Requirements

- Python 3.10+ (tested with Python 3.11).
- Access to serial port device (for real mount mode).
- Linux with `systemd` (if using service controls in web UI).

Install Python dependencies:

```bash
python3 -m pip install flask paho-mqtt pyserial requests skyfield influxdb-client
```

## Quick Start

1. Update TLE file:

```bash
python3 import_requests.py
```

2. Run MQTT listener (writes `state.json`):

```bash
export TINYGS_USER=YOUR_USER_ID
export TINYGS_STATION=YOUR_STATION
export TINYGS_PASS=YOUR_PASSWORD
python3 mqtt_tinygs_listen.py \
  --user "$TINYGS_USER" \
  --station "$TINYGS_STATION" \
  --password "$TINYGS_PASS" \
  --out /home/student/01diplomka/state.json \
  --rx-out /home/student/01diplomka/all_rx.jsonl
```

3. Configure `synscan_config.json`.

4. Start tracker:

```bash
python3 synscan_runner.py
```

5. Optional web UI:

```bash
export SYNSCAN_WEB_PASSWORD=change-me
python3 synscan_web.py
```

Open `http://<host>:8080/config`.

## InfluxDB + Grafana

`mqtt_tinygs_listen.py` can write directly to InfluxDB v2 (optional):

- Measurement `tinygs_state`: data from topic `cmnd/begine` (mode, freq, bw, sf, cr, NORAD, ...).
- Measurement `tinygs_frame`: data from topic `cmnd/frame/0` (satellite, RSSI, SNR, freq error, confirmed/crc_error).

Configuration is via env vars (already loaded by `mqtt_tinygs_listen.service`):

```bash
# /home/student/01diplomka/mqtt_tinygs_listen.env
INFLUXDB_URL=http://127.0.0.1:8086
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=tinygs
INFLUXDB_TOKEN=your-token
INFLUXDB_MEAS_FRAME=tinygs_frame
INFLUXDB_MEAS_STATE=tinygs_state
# Optional custom CA bundle for MQTT TLS (when unset, system trust store is used)
TINYGS_CAFILE=/path/to/ca-bundle.pem
```

Then restart listener:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mqtt_tinygs_listen.service
sudo systemctl status mqtt_tinygs_listen.service
```

Grafana setup:

1. Add data source `InfluxDB` (Flux or InfluxQL according to your Influx setup).
2. Select bucket `tinygs` (or your custom bucket).
3. Build panels from:
   - `tinygs_frame`: `rssi_db`, `snr_db`, `freq_error_hz`, `confirmed`, `crc_error` by `satellite`.
   - `tinygs_state`: `freq`, `bw`, `sf`, `cr` by `satellite`.

Detailed dashboard documentation:

- `README_GRAFANA.md`

## `synscan_config.json` Keys

- `dummy`: `true` to log commands only (no serial writes).
- `port`: serial port path (for real mode), e.g. `/dev/ttyUSB0`.
- `lat`, `lon`, `alt`: observer location.
- `tle`: path to TLE file.
- `mode`: `state`, `max`, or `name`.
- `state`: path to state file (used in `state` mode).
- `min_el`: minimum elevation threshold.
- `interval`: control loop period in seconds.
- `lead`: prediction lead time in seconds.
- `wrap_limit`, `wrap_margin`, `plan_horizon`, `plan_step`, `az_home`: cable-wrap planning controls.
- `status_file`, `status_every`: tracker status JSON path and write interval.

## Web Auth

`synscan_web.py` uses HTTP Basic Auth:

- `SYNSCAN_WEB_PASSWORD`: password (default: `student`).
- `SYNSCAN_WEB_USER`: optional username. If empty, only password is checked.

## Services

`synscan_web.py` controls service name `synscan-follow-sat.service` via `systemctl`.
If you use service mode, ensure this unit exists and runs `python3 synscan_runner.py`.

`mqtt_tinygs_listen.service` is already present in this directory for the TinyGS listener.
