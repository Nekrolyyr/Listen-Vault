"""The daily topic rebuild (11:00, only when a paper finished since the last run) and the admin's maintenance page."""
import dataclasses
from datetime import datetime, time, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from listen import maintenance
from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE

from .conftest import make_user

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}
ELEVEN = time(11, 0)
UTC = timezone.utc


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC)


def test_due_only_after_eleven_and_only_with_new_papers():
    assert not maintenance.topics_due(at(20, 10, 59), ELEVEN, None, True)  # too early
    assert maintenance.topics_due(at(20, 11, 0), ELEVEN, None, True)
    assert not maintenance.topics_due(at(20, 12, 0), ELEVEN, None, False)  # nothing new
    assert not maintenance.topics_due(at(20, 12, 0), ELEVEN, at(20, 11, 0), True)  # already ran today
    assert maintenance.topics_due(at(21, 11, 5), ELEVEN, at(20, 11, 0), True)  # a new day


def test_a_missed_day_runs_at_the_next_check_once():
    # the worker was down from 11:00 to 15:00: the rebuild runs at 15:00, then not again that day
    assert maintenance.topics_due(at(20, 15, 0), ELEVEN, at(19, 11, 0), True)
    assert not maintenance.topics_due(at(20, 15, 1), ELEVEN, at(20, 15, 0), True)


def test_next_run_is_today_or_tomorrow():
    assert maintenance.next_run(at(20, 10, 0), ELEVEN) == at(20, 11, 0)
    assert maintenance.next_run(at(20, 11, 0), ELEVEN) == at(21, 11, 0)


@pytest.fixture
def db(clean_db):
    with psycopg.connect(clean_db, autocommit=True, row_factory=dict_row) as conn:
        yield conn


def _finished_paper(conn, when):
    doc = conn.execute("INSERT INTO documents (source_type, status) VALUES ('url', 'ready') RETURNING id").fetchone()["id"]
    conn.execute("INSERT INTO jobs (document_id, state, finished_at) VALUES (%s, 'done', %s)", (doc, when))


def test_scheduler_queues_one_rebuild_when_a_paper_finished(db):
    _finished_paper(db, datetime.now(UTC) - timedelta(hours=1))
    with db.transaction():
        assert maintenance.schedule_topics(db, "UTC", now=at(20, 11, 30))
    job = db.execute("SELECT * FROM jobs WHERE kind = 'topics'").fetchone()
    assert job["state"] == "queued"
    # a second check while it waits does not queue another
    with db.transaction():
        assert not maintenance.schedule_topics(db, "UTC", now=at(20, 11, 31))
    assert db.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'topics'").fetchone()["n"] == 1


def test_no_rebuild_when_nothing_finished_since_the_last_run(db):
    # the last rebuild ran two days ago, and the only paper finished before it
    last = datetime.now(UTC) - timedelta(days=2)
    _finished_paper(db, datetime.now(UTC) - timedelta(days=3))
    db.execute("INSERT INTO settings (key, value) VALUES ('maintenance_last:topics', %s::jsonb)",
               (f'"{last.isoformat()}"',))
    with db.transaction():
        assert not maintenance.schedule_topics(db, "UTC", now=at(20, 11, 30))
    # a paper finishes today: the rebuild is queued once, and a failure is not queued again on the next check
    _finished_paper(db, datetime.now(UTC) - timedelta(hours=2))
    with db.transaction():
        assert maintenance.schedule_topics(db, "UTC", now=at(20, 11, 30))
    db.execute("UPDATE jobs SET state = 'failed', finished_at = now() WHERE kind = 'topics'")
    with db.transaction():
        assert not maintenance.schedule_topics(db, "UTC", now=at(20, 11, 35))


def test_the_time_is_read_in_the_server_zone(db):
    _finished_paper(db, datetime.now(UTC) - timedelta(hours=1))
    # 10:30 UTC is 12:30 in Zurich (CEST): due there; not due at 10:30 UTC in UTC
    with db.transaction():
        assert maintenance.schedule_topics(db, "Europe/Zurich", now=at(20, 10, 30))


@pytest.fixture
def admin_client(settings, tmp_path):
    app = create_app(dataclasses.replace(settings))
    with TestClient(app) as c:
        _, token = make_user(settings.database_url, "root@example.org", admin=True)
        c.cookies.set(SESSION_COOKIE, token)
        yield c


def test_admin_maintenance_status_runs_and_time_setting(admin_client, settings):
    status = admin_client.get("/api/admin/maintenance", headers=AUTH).json()
    assert status["rebuild_at"] == "11:00" and [t["id"] for t in status["tasks"]] == [
        "areas", "areas_all", "keywords", "topics"]
    assert status["papers_waiting"] is False

    assert admin_client.post("/api/admin/maintenance/topics", headers=AUTH).json() == {"queued": True}
    assert admin_client.post("/api/admin/maintenance/topics", headers=AUTH).json() == {"queued": False}
    status = admin_client.get("/api/admin/maintenance", headers=AUTH).json()
    assert next(t for t in status["tasks"] if t["id"] == "topics")["last_run"] is not None

    assert admin_client.post("/api/admin/maintenance/nope", headers=AUTH).status_code == 404
    assert admin_client.put("/api/admin/maintenance/rebuild-time", json={"time": "25:00"}, headers=AUTH).status_code == 422
    r = admin_client.put("/api/admin/maintenance/rebuild-time", json={"time": "06:30"}, headers=AUTH)
    assert r.json() == {"rebuild_at": "06:30"}
    assert admin_client.get("/api/admin/maintenance", headers=AUTH).json()["rebuild_at"] == "06:30"
    assert admin_client.get("/api/admin/audit", headers=AUTH).json()[0]["action"] == "maintenance.rebuild_time"
