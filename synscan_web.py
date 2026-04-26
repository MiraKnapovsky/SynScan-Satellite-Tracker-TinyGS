#!/usr/bin/env python3
"""Flask UI/API for SynScan config, service control, status and manual moves."""

import json
import math
import os
import secrets
import subprocess
from html import escape
from pathlib import Path
from typing import Any, Dict
import hmac
from flask import Flask, request, redirect, url_for, jsonify, render_template_string, Response, g
from synscan_common import clamp_el, goto_azel, open_port, send_cmd

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG = BASE_DIR / "synscan_config.json"
STATUS = BASE_DIR / "synscan_status.json"
STATE  = BASE_DIR / "state.json"

SERVICE = os.getenv("SYNSCAN_FOLLOW_SERVICE", "synscan-follow-sat.service").strip() or "synscan-follow-sat.service"
CSRF_COOKIE = "synscan_csrf"
CSRF_FIELD = "csrf_token"
# If SYNSCAN_WEB_USER is empty, only password is checked (backward compatible).
WEB_USER = os.getenv("SYNSCAN_WEB_USER", "").strip()
WEB_PASSWORD = os.getenv("SYNSCAN_WEB_PASSWORD", "").strip()
if not WEB_PASSWORD:
    raise SystemExit("Set SYNSCAN_WEB_PASSWORD before starting synscan_web.py")

def _normalize_web_host(value: str) -> str:
    """Accept host or URL input and return plain host/IP for Flask bind."""
    host = value.strip()
    if host.startswith("http://"):
        host = host[len("http://") :]
    elif host.startswith("https://"):
        host = host[len("https://") :]
    return host.strip("/")


WEB_HOST = _normalize_web_host(
    os.getenv("SYNSCAN_WEB_HOST", "127.0.0.1")
) or "127.0.0.1"
try:
    WEB_PORT = int(os.getenv("SYNSCAN_WEB_PORT", "8080"))
except ValueError as exc:
    raise SystemExit("SYNSCAN_WEB_PORT must be an integer") from exc

TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>SynScan</title>
  <style>
    body { font-family: sans-serif; max-width: 980px; margin: 40px auto; }
    .card { padding: 14px; border: 1px solid #ddd; border-radius: 12px; margin: 14px 0; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; }
    .btn { display:inline-block; padding:10px 14px; border:1px solid #ccc; border-radius:10px; text-decoration:none; color:#111; background:#f8f8f8; }
    .btn:hover { background:#f0f0f0; }
    .danger { border-color:#d88; background:#fff3f3; }
    code { background:#f5f5f5; padding:2px 6px; border-radius:6px; }
    input, select { padding: 8px; border-radius: 8px; border: 1px solid #ccc; }
    label { display:block; margin-top: 10px; font-weight: 600; }
    .small { color:#666; font-size: 0.92em; }
    .kv { display:grid; grid-template-columns: 180px 1fr; gap: 6px 12px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
  </style>
</head>
<body>
  <h1>SynScan follow sat</h1>

  <div class="card">
    <h2>Status (tracker)</h2>
    <div class="small" id="summary">?</div>
    <div class="kv" style="margin-top:10px;">
      <div><b>Služba</b></div><div><span id="svc">?</span> / <span id="en">?</span></div>
      <div><b>Režim</b></div><div id="ph">?</div>
      <div><b>Cíl</b></div><div id="tgt">?</div>
      <div><b>Az / El </b></div><div><span id="az">?</span>° / <span id="el">?</span>°</div>
      <div><b>Poslední update</b></div><div id="ts">?</div>
      <div><b>Poslední příkaz</b></div><div><span id="cmd" class="mono">?</span> <span id="cmdts" class="small"></span></div>
    </div>

    <div class="row" style="margin-top:12px;">
      <form method="post" action="/svc/start" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button class="btn" type="submit">Start</button>
      </form>
      <form method="post" action="/svc/stop" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button class="btn danger" type="submit">Stop</button>
      </form>
      <form method="post" action="/svc/restart" style="display:inline;">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <button class="btn" type="submit">Restart</button>
      </form>
      <a class="btn" href="/logs">Logs</a>
    </div>

    <div class="small" style="margin-top:8px;">
      Tracker zapisuje <code>{{ base_dir }}/synscan_status.json</code>.
    </div>
  </div>

  <div class="card">
    <h2>State (TinyGS/MQTT)</h2>
    <div class="small" id="state_summary">?</div>
    <div class="kv" style="margin-top:10px;">
      <div><b>Satelit</b></div><div id="st_sat">?</div>
      <div><b>NORAD</b></div><div id="st_norad">?</div>
      <div><b>Poslední update</b></div><div id="st_last">?</div>
      <div><b>Frekvence / režim</b></div><div><span id="st_freq">?</span> MHz / <span id="st_mode">?</span></div>
      <div><b>BW / SF / CR</b></div><div><span id="st_bw">?</span> kHz / <span id="st_sf">?</span> / <span id="st_cr">?</span></div>
      <div><b>PL / PWR / gain</b></div><div><span id="st_pl">?</span> / <span id="st_pwr">?</span> / <span id="st_gain">?</span></div>
      <div><b>CRC / iIQ / fldro</b></div><div><span id="st_crc">?</span> / <span id="st_iIQ">?</span> / <span id="st_fldro">?</span></div>
      <div><b>CL / SW</b></div><div><span id="st_cl">?</span> / <span id="st_sw">?</span></div>
    </div>

    <div class="small" style="margin-top:8px;">
      Čte <code>{{ base_dir }}/state.json</code>.
    </div>
  </div>

  <div class="card">
    <h2>Konfigurace (synscan_config.json)</h2>
    <form method="post">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <label>dummy (test bez montáže)</label>
      <input type="checkbox" name="dummy" {% if c.get('dummy') %}checked{% endif %}>

      <label>port (RS-232 zařízení, např. /dev/ttyUSB0)</label>
      <input name="port" style="width:420px" value="{{ c.get('port','/dev/ttyUSB0') }}">

      <label>lat (zeměpisná šířka)</label>
      <input name="lat" value="{{ c.get('lat',49.83) }}">

      <label>lon (zeměpisná délka)</label>
      <input name="lon" value="{{ c.get('lon',18.17) }}">

      <label>alt (nadmořská výška v m)</label>
      <input name="alt" value="{{ c.get('alt',240) }}">

      <label>min_el (min. elevace pro tracking)</label>
      <input name="min_el" value="{{ c.get('min_el',10) }}">

      <label>lead (predikční náskok v s)</label>
      <input name="lead" value="{{ c.get('lead',0.8) }}">

      <label>max_az_step (max. azimut na jeden krok; 0 vypne segmentaci)</label>
      <input name="max_az_step" value="{{ c.get('max_az_step', 0.0) }}">

      <label>max_el_step (max. elevace na jeden krok; 0 vypne segmentaci)</label>
      <input name="max_el_step" value="{{ c.get('max_el_step', 0.0) }}">

      <label>wrap_limit (kabelový limit azimut +/- °)</label>
      <input name="wrap_limit" value="{{ c.get('wrap_limit',270.0) }}">

      <label>wrap_margin (rezerva k wrap_limit v °)</label>
      <input name="wrap_margin" value="{{ c.get('wrap_margin',10.0) }}">

      <label>plan_horizon (horizont predikce přeletu v s)</label>
      <input name="plan_horizon" value="{{ c.get('plan_horizon',2400.0) }}">

      <label>plan_step (krok predikce v s)</label>
      <input name="plan_step" value="{{ c.get('plan_step',2.0) }}">

      <label>az_home (bezpečný azimut pro odmotání)</label>
      <input name="az_home" value="{{ c.get('az_home',0.0) }}">

      <label>invert_elevation (obrátit elevaci do montáže)</label>
      <input type="checkbox" name="invert_elevation" {% if c.get('invert_elevation') %}checked{% endif %}>

      <label>elevation_offset_deg (korekce při 0°, lineárně mizí do 90°)</label>
      <input name="elevation_offset_deg" value="{{ c.get('elevation_offset_deg', 0.0) }}">

      <div class="row" style="margin-top:14px;">
        <button class="btn" type="submit">Uložit + restart služby</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h2>Manuální ovládání rotátoru</h2>
    <div class="small">Doporučeno: nejdřív zastavit službu, aby si nepřepisovala příkazy.</div>
    <div class="kv" style="margin-top:10px;">
      <div><b>Aktuální Az / El</b></div>
      <div><span id="cur_az">?</span>° / <span id="cur_el">?</span>°</div>
      <div><b>Az</b></div>
      <div><input id="man_az" type="number" step="0.1" placeholder="0–360" disabled></div>
      <div><b>El</b></div>
      <div><input id="man_el" type="number" step="0.1" placeholder="0–90" disabled></div>
      <div><b>Výsledek</b></div>
      <div id="man_out" class="mono">-</div>
    </div>
    <div class="row" style="margin-top:12px;">
      <button class="btn" type="button" onclick="toggleManual()">Odemknout zadávání</button>
      <button class="btn" type="button" onclick="manualSend()">Poslat na montáž</button>
      <button class="btn danger" type="button" onclick="manualStop()">Stop</button>
    </div>
  </div>

  <script>
    const csrfToken = "{{ csrf_token }}";

    function setText(id, v){
      document.getElementById(id).textContent = (v === undefined || v === null || v === "") ? "-" : v;
    }

    function setNum(id, v, decimals){
      if(v === undefined || v === null || v === ""){
        setText(id, v);
        return;
      }
      const n = Number(v);
      if(Number.isNaN(n)){
        setText(id, v);
        return;
      }
      setText(id, n.toFixed(decimals));
    }

    function statusSummary(svc, s){
      if(!svc || svc.active !== 'active'){
        return 'Služba neběží. Tracker neposílá příkazy na montáž.';
      }
      if(!s){
        return 'Služba běží, ale zatím není vytvořený synscan_status.json.';
      }
      const isCenter = (s.phase === 'center') || (s.do_center === true);
      if(isCenter){
        return 'Surveillance režim: držím neutrální polohu.';
      }
      if(s.tracked_name){
        return 'Sleduji satelit: ' + s.tracked_name + '.';
      }
      return 'Služba běží, čekám na cíl.';
    }

    function parseLastUpdateTime(t){
      if(!t || typeof t !== 'string') return null;
      const m = t.match(/^(\d{1,2}):(\d{2}):(\d{2})$/);
      if(!m) return null;
      const now = new Date();
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), Number(m[1]), Number(m[2]), Number(m[3]));
      if(d.getTime() - now.getTime() > 5 * 60 * 1000){
        d.setDate(d.getDate() - 1);
      }
      return d;
    }

    function stateSummary(s){
      if(!s){
        return 'state.json nenalezen nebo prázdný.';
      }
      const sat = s.sat ? s.sat : '(bez satelitu)';
      const freq = (s.freq !== undefined && s.freq !== null) ? (s.freq + ' MHz') : '-';
      const mode = s.mode ? s.mode : '-';
      const last = s.last_update ? s.last_update : '-';
      let extra = '';
      const t = parseLastUpdateTime(s.last_update);
      if(t){
        const ageSec = Math.round((Date.now() - t.getTime()) / 1000);
        if(ageSec >= 120){
          extra = ' POZOR: update stare (' + ageSec + ' s).';
        }
      }
      return 'TinyGS: ' + sat + ', ' + freq + ' / ' + mode + ', update: ' + last + '.' + extra;
    }

    function toggleManual(){
      const az = document.getElementById('man_az');
      const el = document.getElementById('man_el');
      const nowLocked = !az.disabled;
      az.disabled = nowLocked;
      el.disabled = nowLocked;
    }

    async function manualSend(){
      const az = document.getElementById('man_az').value;
      const el = document.getElementById('man_el').value;
      try{
        const r = await fetch('/api/manual/goto', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
          body: JSON.stringify({az: az, el: el}),
        });
        const j = await r.json();
        if(!r.ok || !j.ok){
          setText('man_out', j.error || 'Chyba.');
          return;
        }
        const out = 'Odesláno: Az ' + j.az_deg + '°, El ' + j.el_deg + '°';
        setText('man_out', out);
      }catch(e){
        setText('man_out', 'Chyba při odeslání.');
      }
    }

    async function manualStop(){
      try{
        const r = await fetch('/api/manual/stop', {
          method: 'POST',
          headers: {'X-CSRF-Token': csrfToken},
        });
        const j = await r.json();
        if(!r.ok || !j.ok){
          setText('man_out', j.error || 'Chyba.');
          return;
        }
        setText('man_out', 'Stop odeslán.');
      }catch(e){
        setText('man_out', 'Chyba při odeslání stop.');
      }
    }

    async function refreshStatus(){
      try{
        const r = await fetch('/api/status', {cache:'no-store'});
        const j = await r.json();

        const svc = j.service || {};
        setText('svc', svc.active || '?');
        setText('en',  svc.enabled || '?');

        const s = j.status;
        if(!s){
          setText('ph','-'); setText('tgt','(zatím žádný synscan_status.json)');
          setText('az','-'); setText('el','-');
          setText('ts','-');
          setText('cmd','-'); setText('cmdts','');
          setText('summary', statusSummary(svc, null));
          return;
        }

        setText('ph', s.phase ?? '-');
        const isCenter = (s.phase === 'center') || (s.do_center === true);

        const tgt = (s.phase === 'no_target') ? '(nic)' : (isCenter ? '[SURVEILLANCE]' : (s.tracked_name ? s.tracked_name : '(nic)'));
        setText('tgt', tgt);

        setNum('az',  s.az_deg, 1);
        setNum('el',  s.el_deg, 1);
        setNum('cur_az', s.az_deg, 1);
        setNum('cur_el', s.el_deg, 1);
        setText('ts',  s.ts);

        setText('cmd', s.last_cmd);
        setText('cmdts', s.last_cmd_ts ? ('(' + s.last_cmd_ts + ')') : '');
        setText('summary', statusSummary(svc, s));
      }catch(e){
        setText('svc','ERROR');
        setText('summary', 'Chyba při načítání statusu.');
      }
    }

    async function refreshState(){
      try{
        const r = await fetch('/api/state', {cache:'no-store'});
        const j = await r.json();
        const s = j.state;

        if(!s){
          setText('st_sat','(nenalezeno state.json)');
          setText('st_norad','-');
          setText('st_last','-');
          setText('st_freq','-');
          setText('st_mode','-');
          setText('st_bw','-');
          setText('st_sf','-');
          setText('st_cr','-');
          setText('st_pl','-');
          setText('st_pwr','-');
          setText('st_gain','-');
          setText('st_crc','-');
          setText('st_iIQ','-');
          setText('st_fldro','-');
          setText('st_cl','-');
          setText('st_sw','-');
          setText('state_summary', stateSummary(null));
          return;
        }

        setText('st_sat', s.sat);
        setText('st_norad', s.NORAD);
        setText('st_last', s.last_update);

        setText('st_freq', s.freq);
        setText('st_mode', s.mode);

        setText('st_bw', s.bw);
        setText('st_sf', s.sf);
        setText('st_cr', s.cr);

        setText('st_pl', s.pl);
        setText('st_pwr', s.pwr);
        setText('st_gain', s.gain);

        setText('st_crc', s.crc);
        setText('st_iIQ', s.iIQ);
        setText('st_fldro', s.fldro);

        setText('st_cl', s.cl);
        setText('st_sw', s.sw);
        setText('state_summary', stateSummary(s));
      }catch(e){
        setText('st_sat','ERROR');
        setText('state_summary', 'Chyba při načítání state.json.');
      }
    }

    refreshStatus();
    refreshState();
    setInterval(refreshStatus, 1000);
    setInterval(refreshState, 1000);
  </script>
</body>
</html>
"""

def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True)

def sh_checked(cmd: list[str]) -> None:
    result = sh(cmd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"Command failed: {' '.join(cmd)}")

def _unauthorized() -> Response:
    return Response(
        "Unauthorized",
        401,
        {"WWW-Authenticate": 'Basic realm="SynScan"'},
    )

def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)

def _validate_csrf() -> bool:
    cookie_token = getattr(g, "csrf_token", None) or ""
    if not cookie_token:
        return False

    provided = request.headers.get("X-CSRF-Token")
    if not provided:
        provided = request.form.get(CSRF_FIELD)

    # Optional JSON fallback for non-browser API clients.
    if not provided and request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            raw = payload.get(CSRF_FIELD)
            if raw is not None:
                provided = str(raw)

    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(provided, cookie_token)

@app.before_request
def require_auth():
    auth = request.authorization
    if not auth:
        return _unauthorized()
    if WEB_USER and auth.username != WEB_USER:
        return _unauthorized()
    if auth.password != WEB_PASSWORD:
        return _unauthorized()

    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = _new_csrf_token()
    g.csrf_token = token

    if request.method == "POST" and not _validate_csrf():
        return Response("CSRF validation failed", 403)

@app.after_request
def set_csrf_cookie(resp: Response) -> Response:
    token = getattr(g, "csrf_token", None)
    if token and request.cookies.get(CSRF_COOKIE) != token:
        resp.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="Strict")
    return resp

def service_state() -> dict:
    a = sh(["systemctl", "is-active", SERVICE]).stdout.strip()
    e = sh(["systemctl", "is-enabled", SERVICE]).stdout.strip()
    return {"active": a, "enabled": e}

def privileged_systemctl(*args: str) -> list[str]:
    cmd = ["systemctl", *args]
    if os.geteuid() == 0:
        return cmd
    return ["sudo", "-n", *cmd]

def service_start():
    sh_checked(privileged_systemctl("start", SERVICE))

def service_stop():
    sh_checked(privileged_systemctl("stop", SERVICE))

def service_restart():
    sh_checked(privileged_systemctl("restart", SERVICE))

def atomic_write_json(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def load_cfg():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}

def load_status():
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return None

def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None

def _get_port() -> str:
    cfg = load_cfg()
    return cfg.get("port") or "/dev/ttyUSB0"

def _invert_elevation_enabled() -> bool:
    cfg = load_cfg()
    return bool(cfg.get("invert_elevation", False))

def _elevation_offset_deg() -> float:
    cfg = load_cfg()
    try:
        return float(cfg.get("elevation_offset_deg", 0.0))
    except (TypeError, ValueError):
        return 0.0

@app.get("/api/status")
def api_status():
    return jsonify({"service": service_state(), "status": load_status()})

@app.get("/api/state")
def api_state():
    return jsonify({"state": load_state()})

@app.post("/api/manual/goto")
def api_manual_goto():
    data = request.get_json(silent=True) or {}
    try:
        az_user = float(data.get("az", ""))
        el_user = float(data.get("el", ""))
    except Exception:
        return jsonify({"ok": False, "error": "Neplatné Az/El."}), 400

    if not math.isfinite(az_user) or not math.isfinite(el_user):
        return jsonify({"ok": False, "error": "Az/El musí být konečné číslo."}), 400
    if not (0.0 <= az_user <= 360.0):
        return jsonify({"ok": False, "error": "Az musí být v rozsahu 0..360°."}), 400
    if not (0.0 <= el_user <= 90.0):
        return jsonify({"ok": False, "error": "El musí být v rozsahu 0..90°."}), 400

    try:
        el_send = clamp_el(el_user)
        with open_port(_get_port()) as ser:
            cmd, ok = goto_azel(
                ser,
                az_user,
                el_send,
                invert_elevation=_invert_elevation_enabled(),
                elevation_offset_deg=_elevation_offset_deg(),
            )
        if not ok:
            return jsonify({"ok": False, "error": "Montáž neodpověděla."}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({
        "ok": True,
        "az_deg": round(az_user, 2),
        "el_deg": round(el_send, 2),
        "cmd": cmd,
    })

@app.post("/api/manual/stop")
def api_manual_stop():
    try:
        with open_port(_get_port()) as ser:
            send_cmd(ser, "M")
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})

@app.get("/")
def home():
    return redirect(url_for("config_get"))

@app.get("/config")
def config_get():
    return render_template_string(
        TEMPLATE,
        c=load_cfg(),
        base_dir=str(BASE_DIR),
        csrf_token=g.csrf_token,
    )

@app.post("/config")
def config_post():
    def f(name, default=None):
        v = request.form.get(name, "")
        return v if v != "" else default

    def parse_float_field(name: str, default: float) -> float:
        raw = f(name, default)
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Neplatná hodnota pole '{name}': {raw!r}") from exc

    current_cfg = load_cfg()
    tle_fixed = str(current_cfg.get("tle") or (BASE_DIR / "satellites.tle"))
    state_fixed = str(current_cfg.get("state") or (BASE_DIR / "state.json"))
    status_file_fixed = str(current_cfg.get("status_file") or (BASE_DIR / "synscan_status.json"))
    interval_fixed = float(current_cfg.get("interval", 0.5))
    max_az_step_fixed = float(current_cfg.get("max_az_step", 0.0))
    max_el_step_fixed = float(current_cfg.get("max_el_step", 0.0))
    status_every_fixed = float(current_cfg.get("status_every", 1.0))

    try:
        cfg: Dict[str, Any] = {
            "dummy": (request.form.get("dummy") == "on"),
            "port": f("port", "/dev/ttyUSB0"),
            "lat": parse_float_field("lat", 0.0),
            "lon": parse_float_field("lon", 0.0),
            "alt": parse_float_field("alt", 0.0),
            "tle": tle_fixed,
            "state": state_fixed,
            "min_el": parse_float_field("min_el", 10.0),
            "interval": interval_fixed,
            "lead": parse_float_field("lead", 0.8),
            "max_az_step": parse_float_field("max_az_step", max_az_step_fixed),
            "max_el_step": parse_float_field("max_el_step", max_el_step_fixed),
            "wrap_limit": parse_float_field("wrap_limit", 270.0),
            "wrap_margin": parse_float_field("wrap_margin", 10.0),
            "plan_horizon": parse_float_field("plan_horizon", 2400.0),
            "plan_step": parse_float_field("plan_step", 2.0),
            "az_home": parse_float_field("az_home", 0.0),
            "invert_elevation": (request.form.get("invert_elevation") == "on"),
            "elevation_offset_deg": parse_float_field("elevation_offset_deg", 0.0),
            "status_file": status_file_fixed,
            "status_every": status_every_fixed,
        }
    except ValueError as exc:
        return str(exc), 400

    atomic_write_json(CONFIG, cfg)
    try:
        service_restart()
    except RuntimeError as exc:
        return f"Konfigurace uložena, ale restart služby selhal: {exc}", 500
    return redirect(url_for("config_get"))

@app.post("/svc/start")
def svc_start():
    try:
        service_start()
    except RuntimeError as exc:
        return f"Service start failed: {exc}", 500
    return redirect(url_for("config_get"))

@app.post("/svc/stop")
def svc_stop():
    try:
        service_stop()
    except RuntimeError as exc:
        return f"Service stop failed: {exc}", 500
    return redirect(url_for("config_get"))

@app.post("/svc/restart")
def svc_restart():
    try:
        service_restart()
    except RuntimeError as exc:
        return f"Service restart failed: {exc}", 500
    return redirect(url_for("config_get"))

@app.get("/logs")
def logs():
    r = sh(["journalctl", "-u", SERVICE, "-n", "200", "--no-pager"])
    text = r.stdout if r.stdout else "(no logs)"
    return f"<pre>{escape(text)}</pre>"

if __name__ == "__main__":
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
