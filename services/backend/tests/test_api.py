import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient

from listen.api.main import create_app
from listen.storage import DocPaths

AUTH = {"Remote-User": "doctor", "X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def client(settings, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>app</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    app = create_app(dataclasses.replace(settings, static_dir=static))
    with TestClient(app) as c:
        yield c


def test_auth_required(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"Remote-User": "doctor"}).status_code == 401
    assert client.get("/api/documents", headers={**AUTH, "Remote-User": "mallory"}).status_code == 401
    assert client.get("/api/documents", headers={**AUTH, "X-Listen-Proxy": "guess"}).status_code == 401
    assert client.get("/", headers={"Remote-User": "doctor"}).status_code == 401  # SPA is protected too
    assert client.get("/api/me", headers=AUTH).json()["user"] == "doctor"


def test_state_changes_need_csrf_header(client):
    headers = {k: v for k, v in AUTH.items() if k != "X-Requested-With"}
    r = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=headers)
    assert r.status_code == 403


def test_upload_pdf_creates_job(client, settings):
    r = client.post("/api/documents", files={"file": ("My_Great_Paper.pdf", PDF, "application/pdf")}, headers=AUTH)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["title"] == "My Great Paper" and doc["status"] == "queued" and doc["job"]["state"] == "queued"
    assert DocPaths(settings.docs_dir, doc["id"]).source_pdf.read_bytes() == PDF
    listed = client.get("/api/documents", headers=AUTH).json()
    assert [d["id"] for d in listed] == [doc["id"]]


def test_upload_rejects_non_pdf_and_large_files(client, settings):
    r = client.post("/api/documents", files={"file": ("x.pdf", b"<html>", "application/pdf")}, headers=AUTH)
    assert r.status_code == 415
    big = PDF + b"0" * (3 * 1024 * 1024)
    r = client.post("/api/documents", files={"file": ("big.pdf", big, "application/pdf")}, headers=AUTH)
    assert r.status_code == 413
    assert client.get("/api/documents", headers=AUTH).json() == []
    assert not any(settings.docs_dir.iterdir())


def test_add_url_search_patch_delete(client, settings):
    r = client.post("/api/documents/url", json={"url": "https://arxiv.org/abs/1706.03762"}, headers=AUTH)
    assert r.status_code == 201
    doc_id = r.json()["id"]
    assert client.post("/api/documents/url", json={"url": "javascript:alert(1)"}, headers=AUTH).status_code == 422

    client.patch(f"/api/documents/{doc_id}", json={"title": "Attention Is All You Need"}, headers=AUTH)
    assert [d["title"] for d in client.get("/api/documents?q=attention", headers=AUTH).json()] == [
        "Attention Is All You Need"]
    assert client.get("/api/documents?q=nothing", headers=AUTH).json() == []

    root = DocPaths(settings.docs_dir, doc_id).root
    assert root.exists()
    assert client.delete(f"/api/documents/{doc_id}", headers=AUTH).status_code == 204
    assert not root.exists()
    assert client.get(f"/api/documents/{doc_id}", headers=AUTH).status_code == 404


def test_rerun_conflicts_while_open(client, settings):
    doc_id = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH).json()["id"]
    assert client.post(f"/api/documents/{doc_id}/rerun", json={"stage": "chapters"}, headers=AUTH).status_code == 409
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE jobs SET state = 'done'")
    r = client.post(f"/api/documents/{doc_id}/rerun", json={"stage": "chapters"}, headers=AUTH)
    assert r.status_code == 202 and r.json()["job"]["state"] == "queued"
    assert client.post(f"/api/documents/{doc_id}/rerun", json={"stage": "bogus"}, headers=AUTH).status_code == 422


def test_keywords_backfill_queues_ready_documents_without_keywords(client, settings):
    doc_id = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH).json()["id"]
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc_id,))
        conn.execute("UPDATE jobs SET state = 'done'")
    assert client.post("/api/library/keywords/backfill", headers=AUTH).json() == {"queued": 1}
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        options = conn.execute("SELECT options FROM jobs WHERE state = 'queued'").fetchone()[0]
        assert options == {"from": "enrich"}
        conn.execute("INSERT INTO document_keywords (document_id, kind, term, source) VALUES (%s, 'topic', 'sleep', 'llm')",
                     (doc_id,))
    # already has keywords and an open job: nothing more to queue
    assert client.post("/api/library/keywords/backfill", headers=AUTH).json() == {"queued": 0}
    detail = client.get(f"/api/documents/{doc_id}", headers=AUTH).json()
    assert detail["keywords"]["topic"] == ["sleep"] and detail["keywords"]["task"] == []


def test_zotero_status_and_queue_of_catalogued_items(client, settings):
    status = client.get("/api/zotero", headers=AUTH).json()
    assert status["configured"] is False and status["queue_tag"] == "listen-queue"
    assert client.post("/api/zotero/sync", headers=AUTH).status_code == 503
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        with_link = conn.execute("INSERT INTO documents (source_type, source_url, title, status) "
                                 "VALUES ('url', 'https://doi.org/10.1/x', 'A', 'catalogued') RETURNING id").fetchone()[0]
        no_link = conn.execute("INSERT INTO documents (source_type, title, status) "
                               "VALUES ('url', 'B', 'catalogued') RETURNING id").fetchone()[0]
    assert client.post(f"/api/documents/{no_link}/queue", headers=AUTH).status_code == 422
    r = client.post(f"/api/documents/{with_link}/queue", headers=AUTH)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    assert client.post(f"/api/documents/{with_link}/queue", headers=AUTH).status_code == 409
    assert client.get("/api/zotero", headers=AUTH).json()["catalogued"] == 1


def test_questions_need_a_processed_paper(client, settings):
    doc_id = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH).json()["id"]
    body = {"question": "What is the main result?"}
    assert client.post(f"/api/documents/{doc_id}/questions", json=body, headers=AUTH).status_code == 409
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc_id,))
        conn.execute("UPDATE jobs SET state = 'done'")
    r = client.post(f"/api/documents/{doc_id}/questions", json=body, headers=AUTH)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    listed = client.get(f"/api/documents/{doc_id}/questions", headers=AUTH).json()
    assert [q["question"] for q in listed] == ["What is the main result?"]
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        assert conn.execute("SELECT kind FROM jobs WHERE state = 'queued'").fetchone()[0] == "qa"
        assert conn.execute("SELECT status FROM documents").fetchone()[0] == "ready"  # untouched by a question
    assert client.post(f"/api/documents/{doc_id}/questions", json={"question": "no"}, headers=AUTH).status_code == 422


def test_playback_and_settings(client):
    doc_id = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH).json()["id"]
    assert client.get(f"/api/documents/{doc_id}/playback", headers=AUTH).json()["position_s"] == 0
    r = client.put(f"/api/documents/{doc_id}/playback", json={"chapter_idx": 2, "position_s": 81.5, "rate": 1.25},
                   headers=AUTH)
    assert r.status_code == 200
    assert client.get(f"/api/documents/{doc_id}/playback", headers=AUTH).json() == {
        "chapter_idx": 2, "position_s": 81.5, "rate": 1.25}
    listed = client.get("/api/documents?sort=listened", headers=AUTH).json()[0]
    assert listed["playback"] == {"chapter_idx": 2, "position_s": 81.5}

    s = client.get("/api/settings", headers=AUTH).json()
    assert s["values"]["voice"] == "af_heart" and "bm_george" in s["voices"]
    r = client.put("/api/settings", json={"voice": "bm_george", "speech_speed": 1.1}, headers=AUTH)
    assert r.json()["values"]["voice"] == "bm_george"
    assert client.put("/api/settings", json={"voice": "nope"}, headers=AUTH).status_code == 422
    assert client.put("/api/settings", json={"speech_speed": 9}, headers=AUTH).status_code == 422
    assert client.put("/api/settings", json={"engine": "espeak"}, headers=AUTH).status_code == 422


def test_audio_supports_range_requests(client, settings):
    doc_id = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=AUTH).json()["id"]
    paths = DocPaths(settings.docs_dir, doc_id).ensure()
    paths.audio(1).write_bytes(bytes(range(256)) * 40)
    r = client.get(f"/api/documents/{doc_id}/chapters/1/audio", headers={**AUTH, "Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 100-199/10240"
    assert r.content == (bytes(range(256)) * 40)[100:200]
    assert client.get(f"/api/documents/{doc_id}/chapters/5/audio", headers=AUTH).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/pdf", headers=AUTH).status_code == 404


def test_spa_fallback_and_no_traversal(client):
    assert client.get("/library", headers=AUTH).text == "<html>app</html>"
    r = client.get("/assets/app.js", headers=AUTH)
    assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
    assert client.get("/../secret.txt", headers=AUTH).text == "<html>app</html>"
    assert client.get("/assets/%2e%2e/%2e%2e/secret.txt", headers=AUTH).text != "do not serve"
    assert client.get("/api/nope", headers=AUTH).status_code == 404
