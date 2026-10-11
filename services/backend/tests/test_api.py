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
    assert status["configured"] is False
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


def _library_doc(settings, title, published, status, terms, listened=False, source="pdf"):
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        doc_id = conn.execute(
            "INSERT INTO documents (source_type, title, published, status, listened, duration_s) "
            "VALUES (%s, %s, %s, %s, %s, 600) RETURNING id",
            (source, title, published, status, listened)).fetchone()[0]
        for kind, term in terms:
            conn.execute("INSERT INTO document_keywords (document_id, kind, term, source) "
                         "VALUES (%s, %s, %s, 'llm')", (doc_id, kind, term))
    return str(doc_id)


def test_library_filters_facets_groups_and_graph(client, settings):
    a = _library_doc(settings, "Head pruning", "2024-05-02", "ready",
                     [("method", "head pruning"), ("task", "machine translation")], listened=True)
    b = _library_doc(settings, "Pruning again", "2024-07-01", "ready",
                     [("method", "head pruning"), ("task", "machine translation")], source="url")
    _library_doc(settings, "Sleep", "2019", "ready", [("task", "sleep consolidation")])

    titles = lambda q: sorted(d["title"] for d in client.get(q, headers=AUTH).json())  # noqa: E731
    assert titles("/api/documents?term=head%20pruning") == ["Head pruning", "Pruning again"]
    assert titles("/api/documents?term=head%20pruning&term=machine%20translation&listened=true") == ["Head pruning"]
    assert titles("/api/documents?year=2024") == ["Head pruning", "Pruning again"]
    assert titles("/api/documents?source=url") == ["Pruning again"]
    assert titles("/api/documents?status=queued") == []
    assert client.get("/api/documents?year=24", headers=AUTH).status_code == 422

    facets = client.get("/api/library/facets", headers=AUTH).json()
    assert facets["total"] == 3 and facets["audio_s"] == 1800 and facets["listened_audio_s"] == 600
    assert facets["source"] == {"pdf": 2, "url": 1, "zotero": 0}
    assert facets["years"] == [{"year": 2019, "count": 1}, {"year": 2024, "count": 2}]
    assert facets["terms"]["method"] == [{"term": "head pruning", "count": 2}]
    narrowed = client.get("/api/library/facets?term=head%20pruning", headers=AUTH).json()
    assert narrowed["total"] == 2 and narrowed["listened"] == {"true": 1, "false": 1}

    groups = client.get("/api/library/groups", headers=AUTH).json()
    assert [sorted(d["id"] for d in g["documents"]) for g in groups["groups"]] == [sorted([a, b])]
    assert "head pruning" in groups["groups"][0]["label"] and [d["title"] for d in groups["ungrouped"]] == ["Sleep"]

    graph = client.get("/api/library/graph", headers=AUTH).json()
    group = {n["title"]: n["group"] for n in graph["nodes"]}
    assert len(graph["nodes"]) == 3 and group["Head pruning"] == group["Pruning again"] != group["Sleep"]
    assert [(link["source"], link["target"]) for link in graph["links"]] == [tuple(sorted((a, b)))]


def test_timeline_and_topics(client, settings):
    a = _library_doc(settings, "Old paper", "2019", "ready", [("keyword", "sleep")])
    _library_doc(settings, "New paper", "2024-03-05", "ready", [("keyword", "sleep")], source="url")
    _library_doc(settings, "Undated", None, "ready", [("keyword", "sleep")])
    _library_doc(settings, "Other", "2020", "ready", [("keyword", "memory")])

    timeline = client.get("/api/library/timeline?term=sleep", headers=AUTH).json()
    assert timeline["label"] == "sleep"
    assert [i["title"] for i in timeline["items"]] == ["Old paper", "New paper", "Undated"]  # undated last
    assert timeline["items"][0] == {"id": a, "title": "Old paper", "date": "2019-01-01", "precision": "year",
                                    "source": "pdf", "status": "ready", "listened": False}
    assert timeline["items"][1]["date"] == "2024-03-05" and timeline["items"][1]["precision"] == "day"
    assert timeline["items"][2]["date"] is None

    assert client.get("/api/library/timeline", headers=AUTH).status_code == 422
    assert client.get("/api/library/timeline?topic=999", headers=AUTH).status_code == 404

    assert client.post("/api/library/topics/rebuild", headers=AUTH).json() == {"queued": True}
    assert client.post("/api/library/topics/rebuild", headers=AUTH).json() == {"queued": False}
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        topic_id = conn.execute("INSERT INTO topics (name, terms) VALUES ('Sleep and memory', '{sleep,memory}') "
                                "RETURNING id").fetchone()[0]
    listed = client.get("/api/library/topics", headers=AUTH).json()
    assert listed == [{"id": topic_id, "name": "Sleep and memory", "terms": ["sleep", "memory"], "documents": 4}]
    by_topic = client.get(f"/api/library/timeline?topic={topic_id}", headers=AUTH).json()
    assert by_topic["label"] == "Sleep and memory" and len(by_topic["items"]) == 4


def test_queue_all_catalogued_is_manual_and_skips_unlinked_items(client, settings):
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('pdf', 'With PDF', 'catalogued')")
        conn.execute("INSERT INTO documents (source_type, source_url, title, status) "
                     "VALUES ('url', 'https://doi.org/10.1/x', 'With link', 'catalogued')")
        conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('url', 'Nothing', 'catalogued')")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0  # importing queued nothing
    assert client.post("/api/zotero/queue-all", headers=AUTH).json() == {"queued": 2}
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        statuses = dict(conn.execute("SELECT title, status FROM documents").fetchall())
    assert statuses == {"With PDF": "queued", "With link": "queued", "Nothing": "catalogued"}
