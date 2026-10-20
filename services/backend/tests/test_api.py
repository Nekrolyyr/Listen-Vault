import dataclasses

import psycopg
import pytest
from fastapi.testclient import TestClient

from listen.api.main import create_app
from listen.api.security import SESSION_COOKIE
from listen.storage import DocPaths

from .conftest import make_user

AUTH = {"X-Listen-Proxy": "test-proxy-secret", "X-Requested-With": "listen-vault"}


@pytest.fixture(scope="session")
def PDF(tmp_path_factory) -> bytes:
    """A real two-page PDF (the upload counts its pages)."""
    from .conftest import make_pdf
    path = make_pdf(tmp_path_factory.mktemp("pdf") / "two.pdf", [[(14, "Head pruning")], [(12, "Methods")]])
    return path.read_bytes()


@pytest.fixture
def client(settings, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>app</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("do not serve")
    app = create_app(dataclasses.replace(settings, static_dir=static))
    with TestClient(app) as c:
        user_id, token = make_user(settings.database_url, admin=True)
        c.cookies.set(SESSION_COOKIE, token)
        c.user_id = user_id
        yield c


def test_auth_required(client, settings):
    assert client.get("/api/health", headers=AUTH).status_code == 200
    assert client.get("/api/documents", headers=AUTH).status_code == 200  # signed in by the fixture
    anonymous = TestClient(client.app)
    assert anonymous.get("/api/documents", headers=AUTH).status_code == 401  # no session cookie
    assert anonymous.get("/api/documents").status_code == 401  # and no proxy secret
    assert anonymous.get("/api/documents", headers={**AUTH, "X-Listen-Proxy": "guess"}).status_code == 401
    anonymous.cookies.set(SESSION_COOKIE, "forged")
    assert anonymous.get("/api/documents", headers=AUTH).status_code == 401
    assert anonymous.get("/api/terms", headers=AUTH).status_code == 200  # public
    assert anonymous.get("/", headers=AUTH).status_code == 200  # the app itself is public; its data is not
    assert client.get("/api/me", headers=AUTH).json()["email"] == "doctor@example.org"


def test_state_changes_need_csrf_header(client):
    headers = {k: v for k, v in AUTH.items() if k != "X-Requested-With"}
    r = client.post("/api/documents/url", json={"url": "https://example.org/a"}, headers=headers)
    assert r.status_code == 403


def test_upload_pdf_creates_job(client, settings, PDF):
    r = client.post("/api/documents", files={"file": ("My_Great_Paper.pdf", PDF, "application/pdf")}, headers=AUTH)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["title"] == "My Great Paper" and doc["status"] == "queued" and doc["job"]["state"] == "queued"
    assert DocPaths(settings.docs_dir, doc["id"]).source_pdf.read_bytes() == PDF
    listed = client.get("/api/documents", headers=AUTH).json()
    assert [d["id"] for d in listed] == [doc["id"]]


def test_upload_rejects_non_pdf_and_large_files(client, settings, PDF):
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
    assert client.post("/api/admin/maintenance/keywords", headers=AUTH).json() == {"queued": 1}
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        options = conn.execute("SELECT options FROM jobs WHERE state = 'queued'").fetchone()[0]
        assert options == {"from": "enrich"}
        conn.execute("INSERT INTO document_keywords (document_id, kind, term, source) VALUES (%s, 'topic', 'sleep', 'llm')",
                     (doc_id,))
    # already has keywords and an open job: nothing more to queue
    assert client.post("/api/admin/maintenance/keywords", headers=AUTH).json() == {"queued": 0}
    detail = client.get(f"/api/documents/{doc_id}", headers=AUTH).json()
    assert detail["keywords"]["topic"] == ["sleep"] and detail["keywords"]["task"] == []


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
    assert listed["playback"] == {"chapter_idx": 2, "position_s": 81.5, "chapter_title": None,
                                  "listened_s": 81.5, "progress": None}  # no chapters or duration here

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
    (paths.root / "narrations" / "1").mkdir(parents=True)
    (paths.root / "narrations" / "1" / "1.m4a").write_bytes(bytes(range(256)) * 40)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        narration = conn.execute("INSERT INTO narrations (document_id, user_id, state) "
                                 "SELECT %s, id, 'ready' FROM users WHERE email = 'doctor@example.org' RETURNING id",
                                 (doc_id,)).fetchone()[0]
        conn.execute("INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s) "
                         "VALUES (%s, 1, 'narrations/' || %s || '/1.m4a', 10)", (narration, narration))
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
    """A processed paper in the library of the doctor (the account the client fixture signs in)."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        doc_id = conn.execute(
            "INSERT INTO documents (source_type, title, published, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (source, title, published, status)).fetchone()[0]
        conn.execute("INSERT INTO library_entries (user_id, document_id, listened) "
                     "SELECT id, %s, %s FROM users WHERE email = 'doctor@example.org'", (doc_id, listened))
        conn.execute("INSERT INTO narrations (document_id, user_id, state, duration_s) "
                     "SELECT %s, id, 'ready', 600 FROM users WHERE email = 'doctor@example.org'", (doc_id,))
        for kind, term in terms:
            conn.execute("INSERT INTO document_keywords (document_id, kind, term, source) "
                         "VALUES (%s, %s, %s, 'llm')", (doc_id, kind, term))
    return str(doc_id)


def test_library_filters_facets_and_graph(client, settings):
    a = _library_doc(settings, "Head pruning", "2024-05-02", "ready",
                     [("method", "head pruning"), ("task", "machine translation")], listened=True)
    b = _library_doc(settings, "Pruning again", "2024-07-01", "ready",
                     [("method", "head pruning"), ("task", "machine translation")], source="url")
    _library_doc(settings, "Sleep", "2019", "ready", [("task", "sleep consolidation")])

    titles = lambda q: sorted(d["title"] for d in client.get(q, headers=AUTH).json())  # noqa: E731
    assert titles("/api/documents?term=head%20pruning") == ["Head pruning", "Pruning again"]
    assert titles("/api/documents?term=head%20pruning&term=machine%20translation&listened=true") == ["Head pruning"]
    assert titles("/api/documents?year_from=2024&year_to=2024") == ["Head pruning", "Pruning again"]
    assert titles("/api/documents?year_from=2020") == ["Head pruning", "Pruning again"]  # the 2019 paper is out
    assert titles("/api/documents?year_to=2019") == ["Sleep"]
    assert titles("/api/documents?year_from=2025") == []
    assert titles("/api/documents?source=url") == ["Pruning again"]
    assert titles("/api/documents?status=queued") == []
    assert client.get("/api/documents?year_from=24", headers=AUTH).status_code == 422

    facets = client.get("/api/library/facets", headers=AUTH).json()
    assert facets["total"] == 3 and facets["audio_s"] == 1800 and facets["listened_audio_s"] == 600
    assert facets["source"] == {"pdf": 2, "url": 1}
    assert facets["years"] == [{"year": 2019, "count": 1}, {"year": 2024, "count": 2}]
    assert facets["year_bounds"] == [2019, 2024]
    narrowed_years = client.get("/api/library/facets?year_from=2024", headers=AUTH).json()
    assert narrowed_years["years"] == [{"year": 2024, "count": 2}]
    assert narrowed_years["year_bounds"] == [2019, 2024]  # the whole library, not the filtered papers
    assert facets["terms"]["method"] == [{"term": "head pruning", "count": 2}]
    narrowed = client.get("/api/library/facets?term=head%20pruning", headers=AUTH).json()
    assert narrowed["total"] == 2 and narrowed["listened"] == {"true": 1, "false": 1}

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

    assert client.post("/api/admin/maintenance/topics", headers=AUTH).json() == {"queued": True}
    assert client.post("/api/admin/maintenance/topics", headers=AUTH).json() == {"queued": False}
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        topic_id = conn.execute("INSERT INTO topics (name, terms, paper_count) VALUES "
                                "('Sleep and memory', '{sleep,memory}', 4) RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO document_topics (document_id, topic_id) SELECT id, %s FROM documents", (topic_id,))
    listed = client.get("/api/library/topics", headers=AUTH).json()
    assert listed == [{"id": topic_id, "name": "Sleep and memory", "terms": ["sleep", "memory"], "area": None,
                       "documents": 4}]
    by_topic = client.get(f"/api/library/timeline?topic={topic_id}", headers=AUTH).json()
    assert by_topic["label"] == "Sleep and memory" and len(by_topic["items"]) == 4


def test_playback_shows_the_chapter_and_progress_through_the_paper(client, settings):
    doc = _library_doc(settings, "Long paper", "2024", "ready", [("task", "long task")])
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute("INSERT INTO chapters (document_id, idx, kind, title) VALUES "
                     "(%s, 0, 'overview', 'Overview'), (%s, 1, 'chapter', 'Methods'), "
                     "(%s, 2, 'chapter', 'Results')", (doc, doc, doc))
        conn.execute("UPDATE narrations SET duration_s = 1000 WHERE document_id = %s", (doc,))
        conn.execute("INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s) "
                     "SELECT n.id, c.idx, 'audio/' || c.idx || '.m4a', d FROM narrations n "
                     "JOIN (VALUES (0, 100.0), (1, 300.0), (2, 600.0)) AS v(idx, d) ON true "
                     "JOIN chapters c ON c.document_id = n.document_id AND c.idx = v.idx "
                     "WHERE n.document_id = %s", (doc,))
    client.put(f"/api/documents/{doc}/playback", json={"chapter_idx": 1, "position_s": 150, "rate": 1.0}, headers=AUTH)
    listed = client.get("/api/documents", headers=AUTH).json()[0]
    assert listed["playback"]["chapter_title"] == "Methods"
    assert listed["playback"]["listened_s"] == 250.0  # 100 s of the overview, then 150 s into Methods
    assert listed["playback"]["progress"] == 0.25
    assert listed["listened"] is False
