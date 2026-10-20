"""Library-wide maintenance: the daily topic rebuild and the admin's manual tasks.

The daily rebuild runs once a day, after the time in the admin setting `topics_rebuild_at` (local time, `TZ`), and
only when a paper finished processing since the last run. A run is recorded when it is queued, so a failing rebuild
is not queued again every minute.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import Connection

from . import jobs

DEFAULT_REBUILD_AT = "11:00"
TASKS = {
    "areas": "Place papers in areas",
    "areas_all": "Place all papers in areas again",
    "keywords": "Extract keywords for the library",
    "topics": "Rebuild topics",
}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_time(value: str) -> time:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError("The time must look like 11:00 (24-hour clock)")
    return time.fromisoformat(value)


def topics_due(now_local: datetime, rebuild_at: time, last_run: datetime | None, papers_finished: bool) -> bool:
    """Whether the daily topic rebuild should be queued now. `now_local` is an aware datetime in the server's zone,
    `last_run` an aware datetime (UTC or local), `papers_finished` whether a paper finished since `last_run`."""
    today_at = now_local.replace(hour=rebuild_at.hour, minute=rebuild_at.minute, second=0, microsecond=0)
    if now_local < today_at or not papers_finished:
        return False
    return last_run is None or last_run < today_at


def next_run(now_local: datetime, rebuild_at: time) -> datetime:
    today_at = now_local.replace(hour=rebuild_at.hour, minute=rebuild_at.minute, second=0, microsecond=0)
    return today_at if now_local < today_at else today_at + timedelta(days=1)


def get_value(conn: Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return default if row is None else row["value"]


def set_value(conn: Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s::jsonb) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, json.dumps(value)))


def rebuild_time(conn: Connection) -> time:
    return parse_time(get_value(conn, "topics_rebuild_at", DEFAULT_REBUILD_AT))


def last_run(conn: Connection, task: str) -> datetime | None:
    value = get_value(conn, f"maintenance_last:{task}")
    return None if value is None else datetime.fromisoformat(value)


def record_run(conn: Connection, task: str) -> None:
    set_value(conn, f"maintenance_last:{task}", datetime.now(timezone.utc).isoformat())


def papers_finished_since(conn: Connection, since: datetime | None) -> bool:
    if since is None:
        return conn.execute("SELECT 1 FROM jobs WHERE kind = 'document' AND state = 'done' LIMIT 1").fetchone() is not None
    return conn.execute("SELECT 1 FROM jobs WHERE kind = 'document' AND state = 'done' AND finished_at > %s LIMIT 1",
                        (since,)).fetchone() is not None


def queue_topics(conn: Connection, user_id: int | None = None) -> bool:
    """Queue a topic rebuild and record the run. False when one is already waiting or running."""
    job = jobs.enqueue_library(conn, "topics", user_id=user_id)
    if job is not None:
        record_run(conn, "topics")
    return job is not None


def schedule_topics(conn: Connection, zone: str, now: datetime | None = None) -> bool:
    """The daily check, called by the worker about once a minute. Returns True when a rebuild was queued."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    run_at = last_run(conn, "topics")
    if not topics_due(local_now, rebuild_time(conn), run_at, papers_finished_since(conn, run_at)):
        return False
    return queue_topics(conn)


def status(conn: Connection, zone: str, now: datetime | None = None) -> dict[str, Any]:
    local_now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))
    at = rebuild_time(conn)
    run_at = last_run(conn, "topics")
    return {
        "rebuild_at": at.strftime("%H:%M"),
        "timezone": zone,
        "next_run": next_run(local_now, at).isoformat(),
        "last_run": run_at.isoformat() if run_at else None,
        "papers_waiting": papers_finished_since(conn, run_at),
        "tasks": [{"id": task, "label": label, "last_run": (r.isoformat() if (r := last_run(conn, task)) else None)}
                  for task, label in TASKS.items()],
    }
