"""The signed-in user's settings (audio and reading), stored per account in user_settings, with defaults."""
from __future__ import annotations

import json
from typing import Any

from psycopg import Connection

# Audio settings are the account's own: the audio made for a paper uses the settings of the user who added it.
DEFAULTS: dict[str, Any] = {
    "engine": "kokoro",          # kokoro | piper
    "voice": "af_heart",         # Kokoro voice id; Piper uses its single installed voice
    "speech_speed": 1.0,         # synthesis speed (playback speed is set in the player)
    "citations": "remove",       # remove | speak  — "(Smith et al., 2020)"
    "reading_wpm": 300,          # speed-read target, words per minute (150–800)
}
AUDIO_KEYS = ("engine", "voice", "speech_speed", "citations")

INT_RANGES: dict[str, tuple[int, int]] = {
    "reading_wpm": (150, 800),
}

CHOICES: dict[str, tuple] = {
    "engine": ("kokoro", "piper"),
    "citations": ("remove", "speak"),
}


def get_all(conn: Connection, user_id: int) -> dict[str, Any]:
    values = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM user_settings WHERE user_id = %s", (user_id,)):
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
        elif isinstance(default, int):
            low, high = INT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{key} must be a whole number between {low} and {high}")
            value = int(value)
        elif not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError(f"{key} must be a short string")
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key} must be one of {', '.join(CHOICES[key])}")
        clean[key] = value
    return clean


def update(conn: Connection, user_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    for key, value in validate(changes).items():
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value",
            (user_id, key, json.dumps(value)),
        )
    return get_all(conn, user_id)
