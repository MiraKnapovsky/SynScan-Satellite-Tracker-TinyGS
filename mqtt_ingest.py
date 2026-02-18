#!/usr/bin/env python3
"""MQTT ingest helpers: client creation, topic parsing, env parsing."""

import os
from typing import Dict, Optional

import paho.mqtt.client as mqtt


def make_client(client_id: str) -> mqtt.Client:
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)


def parse_topic_parts(topic: str) -> Dict[str, Optional[str]]:
    parts = topic.split("/")
    if len(parts) < 4 or parts[0] != "tinygs":
        return {"user": None, "station": None, "channel": None, "cmd": None, "subcmd": None}
    return {
        "user": parts[1] if len(parts) > 1 else None,
        "station": parts[2] if len(parts) > 2 else None,
        "channel": parts[3] if len(parts) > 3 else None,
        "cmd": parts[4] if len(parts) > 4 else None,
        "subcmd": parts[5] if len(parts) > 5 else None,
    }


def parse_env_float(name: str) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[WARN] Ignoring invalid float in env {name}: {raw!r}")
        return None
