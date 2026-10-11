"""User-adjustable settings stored in the settings table, with defaults."""
from __future__ import annotations

import json
from typing import Any

from psycopg import Connection

DEFAULTS: dict[str, Any] = {
    "engine": "kokoro",          # kokoro | piper
    "voice": "af_heart",         # Kokoro voice id; Piper uses its single installed voice
    "speech_speed": 1.0,         # synthesis speed (playback speed is set in the player)
    "citations": "remove",       # remove | speak  — "(Smith et al., 2020)"
}

CHOICES: dict[str, tuple] = {
    "engine": ("kokoro", "piper"),
    "citations": ("remove", "speak"),
}


def get_all(conn: Connection) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in DEFAULTS:
            values[row["key"]] = row["value"]
    return values


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"unknown setting {key!r}")
        default = DEFAULTS[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif isinstance(default, float):
            if not isinstance(value, (int, float)) or not 0.7 <= float(value) <= 1.4:
                raise ValueError(f"{key} must be between 0.7 and 1.4")
            value = float(value)
        elif not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError(f"{key} must be a short string")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
        clean[key] = value
    return clean


def update(conn: Connection, changes: dict[str, Any]) -> dict[str, Any]:
    for key, value in validate(changes).items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, json.dumps(value)),
        )
    return get_all(conn)
