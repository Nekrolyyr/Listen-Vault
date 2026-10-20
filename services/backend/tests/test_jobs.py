import psycopg
from psycopg.rows import dict_row

from listen import jobs


def _doc(conn):
    return conn.execute("INSERT INTO documents (source_type) VALUES ('pdf') RETURNING id").fetchone()["id"]


def _conn(url):
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def test_lifecycle(clean_db):
    with _conn(clean_db) as conn:
        doc = _doc(conn)
        job_id = jobs.enqueue(conn, doc)
        assert jobs.has_open_job(conn, doc)
        job = jobs.claim(conn, "w1")
        assert job["id"] == job_id and job["attempts"] == 1
        assert conn.execute("SELECT status FROM documents").fetchone()["status"] == "processing"
        assert jobs.claim(conn, "w2") is None  # running and fresh: not claimable
        assert jobs.heartbeat(conn, job_id, "synthesis", 2, 5)
        jobs.finish(conn, job_id, doc)
        row = conn.execute("SELECT * FROM jobs").fetchone()
        assert (row["state"], row["stage"], row["progress_done"], row["progress_total"]) == ("done", "synthesis", 2, 5)
        assert conn.execute("SELECT status FROM documents").fetchone()["status"] == "ready"
        assert not jobs.has_open_job(conn, doc)


def test_skip_locked_gives_each_worker_a_different_job(clean_db):
    with _conn(clean_db) as a, _conn(clean_db) as b:
        jobs.enqueue(a, _doc(a))
        jobs.enqueue(a, _doc(a))
        first, second = jobs.claim(a, "w1"), jobs.claim(b, "w2")
        assert first["id"] != second["id"]
        assert jobs.claim(a, "w1") is None


def test_stale_job_is_reclaimed(clean_db):
    with _conn(clean_db) as conn:
        jobs.enqueue(conn, _doc(conn))
        job = jobs.claim(conn, "crashed")
        conn.execute("UPDATE jobs SET heartbeat_at = now() - interval '11 minutes'")
        again = jobs.claim(conn, "w2")
        assert again["id"] == job["id"] and again["worker"] == "w2" and again["attempts"] == 2


def test_retry_with_backoff_then_fail(clean_db):
    with _conn(clean_db) as conn:
        doc = _doc(conn)
        jobs.enqueue(conn, doc)
        job = jobs.claim(conn, "w")
        assert jobs.fail(conn, job, "GROBID is busy", retryable=True)
        row = conn.execute("SELECT state, run_after > now() AS later FROM jobs").fetchone()
        assert row == {"state": "queued", "later": True}
        assert jobs.claim(conn, "w") is None  # backoff not over yet
        for attempt in (2, 3):
            conn.execute("UPDATE jobs SET run_after = now()")
            job = jobs.claim(conn, "w")
            assert job["attempts"] == attempt
            retried = jobs.fail(conn, job, "still busy", retryable=True)
        assert not retried
        doc_row = conn.execute("SELECT status, error FROM documents").fetchone()
        assert doc_row == {"status": "failed", "error": "still busy"}


def test_heartbeat_reports_deleted_document(clean_db):
    with _conn(clean_db) as conn:
        doc = _doc(conn)
        jobs.enqueue(conn, doc)
        job = jobs.claim(conn, "w")
        conn.execute("DELETE FROM documents WHERE id = %s", (doc,))
        assert not jobs.heartbeat(conn, job["id"], "synthesis")


def test_claim_gives_the_user_with_fewest_running_jobs_the_next_turn(clean_db):
    with _conn(clean_db) as conn:
        ann = conn.execute("INSERT INTO users (email) VALUES ('ann@example.org') RETURNING id").fetchone()["id"]
        bea = conn.execute("INSERT INTO users (email) VALUES ('bea@example.org') RETURNING id").fetchone()["id"]
        for _ in range(3):  # Ann queues three papers first
            jobs.enqueue(conn, _doc(conn), user_id=ann)
        jobs.enqueue(conn, _doc(conn), user_id=bea)  # Bea's paper is the newest
        first = jobs.claim(conn, "w1")
        second = jobs.claim(conn, "w2")
        assert first["user_id"] == ann
        assert second["user_id"] == bea  # Ann already has a job running, so Bea goes next
        assert jobs.open_for_user(conn, ann) == 3
