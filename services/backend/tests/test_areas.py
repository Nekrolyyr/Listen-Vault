import psycopg

from listen import areas, db
from listen.worker import classify, topics
from listen.worker.llm import LlmError

from .test_api import AUTH, _library_doc, client  # noqa: F401  (client is a fixture)


class FakeLlm:
    """Places a paper that mentions Twitter as social sciences (and computer science), anything else as other."""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def wait_ready(self, timeout_s=0):
        return True

    def truncate(self, text, max_tokens):
        return text

    def complete_json(self, prompt, schema, n_predict=300, *, cache_prompt=False):
        self.calls += 1
        if self.fail:
            raise LlmError("model is down")
        if "Twitter" in prompt:
            return {"primary": "Social Sciences", "also": ["Computer Science"], "confidence": "high"}
        return {"primary": "Other", "also": [], "confidence": "low"}

    def chat(self, user, *, system=None, n_predict=160, temperature=0.2, stop=None, cache_prompt=False, feature=""):
        return self.complete(user, n_predict, temperature)

    def chat_json(self, user, schema, *, system=None, n_predict=300, cache_prompt=False, feature=""):
        return self.complete_json(user, schema, n_predict)

    def complete(self, prompt, n_predict=160, temperature=0.2, **kw):
        if prompt.startswith("Keywords from scientific papers"):
            return "Pruning methods."
        return "none"


def test_rules_place_a_paper_by_its_cue_words():
    placed = areas.rules("twitter twitter tweet election")
    assert (placed.primary, placed.also, placed.source) == ("social", [], "rules")
    placed = areas.rules("election election election neural network neural network")
    assert placed.primary == "social" and placed.also == ["computer_science"]
    assert areas.rules("nothing relevant here").primary == areas.OTHER


def test_clean_keeps_valid_unique_areas_and_at_most_two_others():
    assert areas.clean("social", ["social", "psychology", "nope", "maths", "arts"]) == \
        ("social", ["psychology", "maths"])
    assert areas.clean("nope", None) == (areas.OTHER, [])


def test_llm_names_are_mapped_to_area_ids():
    placed = classify.place("Twitter and elections", None, [], [], FakeLlm())
    assert (placed.primary, placed.also, placed.confidence, placed.source) == \
        ("social", ["computer_science"], "high", "llm")


def test_when_the_llm_fails_the_rules_decide():
    placed = classify.place("Twitter and elections", "tweets", [], [], FakeLlm(fail=True))
    assert placed.source == "rules" and placed.primary == "social"


def test_the_schema_only_allows_the_listed_areas():
    allowed = classify.schema()["properties"]["primary"]["enum"]
    assert sorted(allowed) == sorted(areas.NAMES.values()) and "Other" in allowed


def test_topic_membership_needs_a_keyword_or_two_terms_in_the_text():
    assert topics.in_topic(["sleep consolidation"], {"sleep consolidation"}, "")
    assert topics.in_topic(["head pruning", "sparsity"], set(), "head pruning and sparsity in transformers")
    assert not topics.in_topic(["head pruning", "sparsity"], set(), "head pruning only")
    assert not topics.in_topic(["sleep"], set(), "sleepers")  # whole words only


def test_a_topic_takes_the_most_common_primary_area():
    assert topics.topic_area(["a", "b", "c"], {"a": "news", "b": "news", "c": "politics"}) == "news"
    assert topics.topic_area(["a"], {"a": areas.OTHER}) == areas.OTHER
    assert topics.topic_area([], {}) == areas.OTHER


def _insert(conn, title, abstract=None, status="ready"):
    return str(conn.execute("INSERT INTO documents (source_type, title, abstract, status) VALUES "
                            "('pdf', %s, %s, %s) RETURNING id", (title, abstract, status)).fetchone()[0])


def test_the_job_places_every_paper_once_and_keeps_manual_placements(clean_db, pool, settings):
    with psycopg.connect(clean_db, autocommit=True) as conn:
        twitter = _insert(conn, "Study", "We analyse Twitter posts.")
        plain = _insert(conn, "Geology", "Rock layers.")
        pinned = _insert(conn, "Hand placed", "Twitter again.")
        _insert(conn, "Broken", "Twitter.", status="failed")
        conn.execute("INSERT INTO document_areas (document_id, primary_area, confidence, source, pinned) "
                     "VALUES (%s, 'arts', 'high', 'manual', true)", (pinned,))

    llm = FakeLlm()
    assert classify.run(pool, settings.docs_dir, llm, 0, {}) == 2
    with pool.connection() as conn:
        rows = {str(r["document_id"]): r for r in conn.execute("SELECT * FROM document_areas")}
    assert rows[twitter]["primary_area"] == "social" and rows[twitter]["also_areas"] == ["computer_science"]
    assert rows[plain]["primary_area"] == areas.OTHER and rows[plain]["source"] == "llm"
    assert rows[pinned]["primary_area"] == "arts" and rows[pinned]["source"] == "manual"  # not overwritten
    assert len(rows) == 3  # the failed paper is not placed

    assert classify.run(pool, settings.docs_dir, llm, 0, {}) == 0  # nothing left to place


def test_the_job_retries_rules_placements_once_the_llm_is_back(clean_db, pool, settings):
    with psycopg.connect(clean_db, autocommit=True) as conn:
        doc = _insert(conn, "Twitter study", "Twitter posts.")
    assert classify.run(pool, settings.docs_dir, FakeLlm(fail=True), 0, {}) == 1
    with pool.connection() as conn:
        assert conn.execute("SELECT source FROM document_areas WHERE document_id = %s", (doc,)).fetchone()["source"] == "rules"
    assert classify.run(pool, settings.docs_dir, FakeLlm(), 0, {}) == 1
    with pool.connection() as conn:
        assert conn.execute("SELECT source FROM document_areas WHERE document_id = %s", (doc,)).fetchone()["source"] == "llm"


def test_map_area_topic_and_classification_endpoints(client, settings):
    # two papers share two keywords, so they form a topic; two more form another one
    a = _library_doc(settings, "Head pruning", "2024", "ready", [("method", "head pruning"), ("task", "sparsity")],
                     listened=True)
    _library_doc(settings, "Pruning again", "2025", "ready", [("method", "head pruning"), ("task", "sparsity")])
    _library_doc(settings, "Sleep", "2019", "ready", [("task", "sleep consolidation"), ("method", "memory")])
    _library_doc(settings, "Sleep again", "2020", "ready", [("task", "sleep consolidation"), ("method", "memory")])

    pool = db.make_pool(settings.database_url)
    pool.wait()
    try:
        topics.rebuild(pool, None)
    finally:
        pool.close()
    topic_id = next(t["id"] for t in client.get("/api/library/topics", headers=AUTH).json() if t["name"] == "Head pruning")
    titles = lambda q: sorted(d["title"] for d in client.get(q, headers=AUTH).json())  # noqa: E731

    # hand placement of one paper: its area, its other area, and one of its topics taken off
    placed = client.patch(f"/api/documents/{a}/classification", headers=AUTH,
                          json={"primary": "social", "also": ["psychology"], "topics": {"Head pruning": False}}).json()
    assert placed["areas"] == {"primary": "social", "also": ["psychology"], "confidence": "high", "source": "manual",
                               "pinned": True}
    assert [t["name"] for t in placed["topics"]] == []
    assert client.patch(f"/api/documents/{a}/classification", headers=AUTH,
                        json={"primary": "nope"}).status_code == 422
    assert client.patch(f"/api/documents/{a}/classification", headers=AUTH,
                        json={"primary": "psychology", "also": ["nope"]}).status_code == 422

    overview = client.get("/api/library/overview", headers=AUTH).json()
    assert overview["papers"] == 4 and overview["unread"] == 3
    by_id = {x["id"]: x for x in overview["areas"]}
    assert len(overview["catalog"]) == len(areas.IDS)
    assert set(by_id) == {"social", "psychology"}  # only the areas that have papers are on the map
    assert by_id["social"]["papers"] == 1 and by_id["social"]["listened"] == 1
    assert by_id["psychology"]["papers"] == 1 and by_id["social"]["progress"] == 1.0
    assert overview["unplaced"] == 3  # the three papers the job has not placed
    assert {x["reason"] for x in overview["attention"]} == {"no area yet"}

    area_page = client.get("/api/library/areas/social", headers=AUTH).json()
    assert area_page["papers"] == 1 and area_page["name"] == "Social Sciences"
    assert client.get("/api/library/areas/nope", headers=AUTH).status_code == 404

    topic = client.get(f"/api/library/topics/{topic_id}", headers=AUTH).json()
    assert topic["papers"] == 1 and topic["unread"] == 1  # Head pruning without the paper taken off
    assert {"prev", "next", "related"} <= set(topic)
    assert topic["prev"] is None or set(topic["prev"]) == {"id", "name"}
    assert client.get("/api/library/topics/999", headers=AUTH).status_code == 404

    assert titles("/api/documents?area=social") == ["Head pruning"]
    assert titles("/api/documents?area=psychology") == ["Head pruning"]
    assert titles(f"/api/documents?topic={topic_id}") == ["Pruning again"]
    assert titles("/api/documents?q=social") == ["Head pruning"]  # search reaches area names
    assert titles("/api/documents?q=sleep") == ["Sleep", "Sleep again"]  # and keywords
    assert titles("/api/documents?q=head%20pruning") == ["Head pruning", "Pruning again"]

    # the hand choice survives a rebuild
    pool = db.make_pool(settings.database_url)
    pool.wait()
    try:
        topics.rebuild(pool, None)
    finally:
        pool.close()
    assert titles(f"/api/documents?topic={topic_id}") == ["Pruning again"]  # same id after the rebuild

    # a topic can be put back on a paper by hand
    client.patch(f"/api/documents/{a}/classification", headers=AUTH,
                 json={"primary": "social", "also": [], "topics": {"Head pruning": True}})
    assert titles(f"/api/documents?topic={topic_id}") == ["Head pruning", "Pruning again"]

    assert client.post("/api/admin/maintenance/areas", headers=AUTH).json() == {"queued": True}
    assert client.post("/api/admin/maintenance/areas", headers=AUTH).json() == {"queued": False}
    assert client.post("/api/admin/maintenance/areas_all", headers=AUTH).json() == {"queued": False}


def test_queued_papers_are_progress_not_attention(client, settings, clean_db):
    _library_doc(settings, "Waiting", "2026", "queued", [])
    _library_doc(settings, "Ready", "2026", "ready", [])
    overview = client.get("/api/library/overview", headers=AUTH).json()
    assert (overview["processing"], overview["placing"], overview["unplaced"]) == (1, 0, 1)
    assert [x["title"] for x in overview["attention"]] == ["Ready"]  # the queued paper is not asked for an area

    with psycopg.connect(clean_db, autocommit=True) as conn:
        conn.execute("INSERT INTO jobs (kind, state) VALUES ('areas', 'queued')")  # the library job is on its way
    overview = client.get("/api/library/overview", headers=AUTH).json()
    assert (overview["processing"], overview["placing"], overview["unplaced"]) == (1, 1, 0)
    assert overview["attention"] == []
