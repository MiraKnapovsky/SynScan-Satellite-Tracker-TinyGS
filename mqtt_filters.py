#!/usr/bin/env python3
"""MQTT frame filters and satellite name normalization utilities."""

import re
from typing import Any, Dict, Optional


RSSI_SNR_RE = re.compile(r"RSSI/SNR:\s*([+-]?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)dB", re.IGNORECASE)
FREQ_ERROR_RE = re.compile(r"Freq error:\s*([+-]?\d+(?:\.\d+)?)Hz", re.IGNORECASE)
NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def extract_frame_satellite(frame_obj: Any) -> Optional[str]:
    if not isinstance(frame_obj, list):
        return None
    for row in frame_obj:
        if not isinstance(row, list) or len(row) < 5:
            continue
        if row[0] == 1 and row[1] == 0 and row[2] == 0 and row[3] == 0:
            name = str(row[4]).strip()
            if name and name.upper() != "UNKNOWN":
                return name
            return None
    return None


def extract_frame_metrics(frame_obj: Any) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "rssi_db": None,
        "snr_db": None,
        "freq_error_hz": None,
        "decode_status": "unknown",
        "confirmed": False,
        "crc_error": False,
    }
    if not isinstance(frame_obj, list):
        return metrics

    for row in frame_obj:
        if not isinstance(row, list) or len(row) < 5:
            continue
        text = str(row[4]).strip()

        m_rssi = RSSI_SNR_RE.search(text)
        if m_rssi:
            metrics["rssi_db"] = float(m_rssi.group(1))
            metrics["snr_db"] = float(m_rssi.group(2))
            continue

        m_freq = FREQ_ERROR_RE.search(text)
        if m_freq:
            metrics["freq_error_hz"] = float(m_freq.group(1))
            continue

        upper = text.upper()
        if "CRC ERROR" in upper:
            metrics["decode_status"] = "crc_error"
            metrics["crc_error"] = True
        elif "CONFIRMED" in upper:
            metrics["decode_status"] = "confirmed"
            metrics["confirmed"] = True

    return metrics


def normalize_sat_name(name: str) -> str:
    text = (name or "").strip().lower().replace("_", "-")
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-").replace("\u2013", "-")
    text = text.replace("\u2014", "-").replace("\u2015", "-")
    text = NAME_NORMALIZE_RE.sub("-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def sat_dedupe_key(name: str) -> str:
    normalized = normalize_sat_name(name)
    if normalized:
        return normalized
    return name.strip().casefold()
