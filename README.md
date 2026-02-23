# SynScan TinyGS Tracker

This project tracks satellites with a SynScan mount, using TinyGS MQTT state updates and local TLE data.

## Platform Notes

- Tested on Raspberry Pi 5 (4 GB RAM).
- Tested on Debian GNU/Linux with `systemd`.
- This project is currently Debian-focused. Service files and setup steps in this README are prepared for Debian.

## Components

- `synscan_follow_sat.py`: main tracker loop (select target, compute az/el, send mount commands).
- `synscan_runner.py`: validates `synscan_config.json` and starts tracker with safe arguments.
- `synscan_web.py`: Flask UI/API for config, service control, status, and manual goto/stop.
- `mqtt_tinygs_listen.py`: main TinyGS MQTT listener flow (callbacks, geo, Influx integration).
- `mqtt_ingest.py`: MQTT ingest helpers (client creation, topic/env parsing).
- `mqtt_filters.py`: frame parsing/filtering and satellite-name normalization.
- `mqtt_storage.py`: file/catalog/state serialization helpers used by the listener.
- `mqtt_geo.py`: TLE-based satellite lookup and geometry calculations.
- `import_requests.py`: downloads supported TinyGS satellites and writes `satellites.tle`.
- `synscan_manual.py`: interactive manual mount control.
- `synscan_common.py`: shared serial protocol + az/el conversion utilities.

## Runtime Files

- `synscan_config.json`: tracker configuration used by `synscan_runner.py`.
- `state.json`: latest TinyGS state (updated by MQTT listener).
- `synscan_status.json`: live tracker status (updated by `synscan_follow_sat.py`).
- `satellites.tle`: TLE dataset used for tracking.
- `all_rx.jsonl`: optional frame packet archive (only when listener is started with `--rx-out`).

## Clone Repository

```bash
git clone https://github.com/MiraKnapovsky/SynScan-Satellite-Tracker-TinyGS.git /home/<user>/synscan_tinygs_tracker
cd /home/<user>/synscan_tinygs_tracker
```

## Requirements

- Python 3.10+ (tested with Python 3.11).
- Access to serial port device (for real mount mode).
- Debian GNU/Linux with `systemd` (required for the provided service setup).

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

## Ultimate Beginner Guide (From Zero to Running System)

Use this as the single end-to-end path on a new Debian machine.

1. Clone and enter the project (skip this if you already used the `Clone Repository` section above):

```bash
git clone https://github.com/MiraKnapovsky/SynScan-Satellite-Tracker-TinyGS.git /home/<user>/synscan_tinygs_tracker
cd /home/<user>/synscan_tinygs_tracker
```

2. Create virtual environment and install dependencies:

```bash
python3 -m venv tinygs_mqtt/env
source tinygs_mqtt/env/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

3. Create local TinyGS credential file:

```bash
cp mqtt_tinygs_listen.env.example mqtt_tinygs_listen.env
```

Edit `mqtt_tinygs_listen.env` and fill in:
- `TINYGS_USER`
- `TINYGS_STATION`
- `TINYGS_PASS`

4. Download current TLE data:

```bash
python3 import_requests.py
```

5. Configure tracker in `synscan_config.json`:
- First safe setup: keep `dummy: true`.
- Real movement later: set `dummy: false` and confirm `port`, `lat`, `lon`, `alt`.
- Target selection reads `NORAD` from `state.json`.
- When `NORAD` is missing in `state.json`, tracker switches to surveillance/neutral position.
- Keep `state` and `status_file` paths pointing to this project directory.

6. Load listener credentials into current shell:

```bash
set -a
source mqtt_tinygs_listen.env
set +a
```

7. Start MQTT listener manually (first validation run):

```bash
python3 mqtt_tinygs_listen.py \
  --user "$TINYGS_USER" \
  --station "$TINYGS_STATION" \
  --password "$TINYGS_PASS" \
  --out /home/<user>/synscan_tinygs_tracker/state.json \
  --frame-topic tinygs/${TINYGS_USER}/${TINYGS_STATION}/cmnd/frame/0
```

8. Verify that `state.json` is updating (open second terminal):

```bash
cat /home/<user>/synscan_tinygs_tracker/state.json
```

9. Start tracker loop (second terminal):

```bash
cd /home/<user>/synscan_tinygs_tracker
python3 synscan_runner.py
```

10. Start web UI (third terminal):

```bash
cd /home/<user>/synscan_tinygs_tracker
export SYNSCAN_WEB_PASSWORD=change-me
# optional:
# export SYNSCAN_WEB_USER=admin
# export SYNSCAN_WEB_HOST=158.196.240.175
# export SYNSCAN_WEB_PORT=8080
python3 synscan_web.py
```

Open `http://158.196.240.175:8080/config` (or your custom host/port).

11. Optional: run listener as systemd service after manual test passes:

```bash
sudo cp /home/<user>/synscan_tinygs_tracker/mqtt_tinygs_listen@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mqtt_tinygs_listen@$(whoami).service
sudo systemctl status mqtt_tinygs_listen@$(whoami).service
```

12. Optional: if you already installed `synscan-follow-sat.service` and `synscan-web.service`, control them with:

```bash
sudo systemctl restart synscan-follow-sat.service
sudo systemctl restart synscan-web.service
sudo systemctl status synscan-follow-sat.service synscan-web.service
```

## InfluxDB + Grafana

`mqtt_tinygs_listen.py` can write directly to InfluxDB v2 (optional):

- Measurement `tinygs_state`: data from topic `cmnd/begine` (mode, freq, bw, sf, cr, NORAD, ...).
- Measurement `tinygs_frame`: data from topic `cmnd/frame/0` (satellite, RSSI, SNR, freq error, confirmed/crc_error).

Configuration is via env vars (already loaded by `mqtt_tinygs_listen.service`):

```bash
# /home/student/synscan_tinygs_tracker/mqtt_tinygs_listen.env
INFLUXDB_URL=http://127.0.0.1:8086
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=tinygs
INFLUXDB_TOKEN=your-token
INFLUXDB_MEAS_FRAME=tinygs_frame
INFLUXDB_MEAS_STATE=tinygs_state
INFLUXDB_MEAS_META=tinygs_meta
# Optional custom CA bundle for MQTT TLS (when unset, system trust store is used)
TINYGS_CAFILE=/path/to/ca-bundle.pem
# Optional: tracker status source used to stamp tracked_norad into Influx points
TINYGS_TRACKER_STATUS_FILE=/home/<user>/synscan_tinygs_tracker/synscan_status.json
TINYGS_TRACKER_STATUS_MAX_AGE_S=10
```

Then restart listener:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mqtt_tinygs_listen.service
sudo systemctl status mqtt_tinygs_listen.service
```

Portable multi-user variant (recommended for new deployments):

```bash
# install template unit
sudo cp /home/<user>/synscan_tinygs_tracker/mqtt_tinygs_listen@.service /etc/systemd/system/
sudo systemctl daemon-reload

# start for target linux user account (example: alice)
sudo systemctl enable --now mqtt_tinygs_listen@alice.service
sudo systemctl status mqtt_tinygs_listen@alice.service
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
- `mode`: tracking mode key in config.
- `state`: path to TinyGS state file (`state.json`) used for NORAD target selection.
- `min_el`: minimum elevation threshold.
- `interval`: control loop period in seconds.
- `lead`: prediction lead time in seconds.
- `wrap_limit`, `wrap_margin`, `plan_horizon`, `plan_step`, `az_home`: cable-wrap planning controls.
- `status_file`, `status_every`: tracker status JSON path and write interval.

Web UI note:
- Some advanced/path fields are intentionally hidden in `/config` (`tle`, `state`, `status_file`, `interval`, `status_every`).
- Hidden fields are preserved from existing `synscan_config.json` when saving.

## Web Auth

`synscan_web.py` uses HTTP Basic Auth:

- `SYNSCAN_WEB_PASSWORD`: required password (no insecure default).
- `SYNSCAN_WEB_USER`: optional username. If empty, only password is checked.
- `SYNSCAN_WEB_HOST`: optional bind host (default: `158.196.240.175`).
- `SYNSCAN_WEB_PORT`: optional bind port (default: `8080`).

POST protection:
- All web `POST` actions use CSRF protection.
- Browser form actions work normally.
- Custom API clients must send `X-CSRF-Token` matching the `synscan_csrf` cookie.

## Services

`synscan_web.py` controls service name `synscan-follow-sat.service` via `systemctl`.
If you use service mode, ensure this unit exists and runs `python3 synscan_runner.py`.

`mqtt_tinygs_listen.service` is already present in this directory for the TinyGS listener.
For user-specific deployments without hardcoded `student` paths, use `mqtt_tinygs_listen@.service`.
