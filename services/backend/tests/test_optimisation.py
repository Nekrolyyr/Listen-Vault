"""Tests for the optimisation phases (docs/optimisation-plan.md): the chat format, typed keywords, the tagger, vectors,
paper clustering, exemplar placement, hybrid retrieval, related papers and OpenAlex. The model tests run only when the
model files are installed (MODELS_DIR, default /models)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from listen import areas, config, embeddings, vectors
from listen.worker import classify, enrich, llm as llm_mod, openalex, qa, tagger, topics
from listen.worker.docmodel import Document, Paragraph, Section
from listen.worker.llm import LlmClient, LlmError


# ---------------------------------------------------------------- the chat format
def test_chatml_prompt_turns_thinking_off():
    text = llm_mod.chatml("Which area?", system="Be brief.")
    assert text == ("<|im_start|>system\nBe brief.<|im_end|>\n<|im_start|>user\nWhich area?<|im_end|>\n"
                    "<|im_start|>assistant\n<think>\n\n</think>\n\n")


def test_strip_thinking_removes_closed_and_cut_off_blocks():
    assert llm_mod.strip_thinking("<think>plan</think>\n\n{\"a\": 1}") == '{"a": 1}'
    assert llm_mod.strip_thinking("<think>still planning") == ""
    assert llm_mod.strip_thinking("Answer") == "Answer"


def test_budgets_follow_the_format_and_clients_without_one_are_raw():
    class Bare:  # the test fakes have no chat_format
        pass
    assert llm_mod.budget(Bare(), "excerpt") == 900
    chat = LlmClient("http://x")
    assert chat.chat_format == "chatml" and llm_mod.budget(chat, "excerpt") == 2500
    with pytest.raises(ValueError):
        LlmClient("http://x", chat_format="nope")


class RecordingLlm(LlmClient):
    """A client that records the requests instead of sending them."""

    def __init__(self, chat_format, answer=""):
        super().__init__("http://llm", chat_format=chat_format)
        self.sent: list[dict] = []
        self.answer = answer

    def _post(self, path, payload):
        self.sent.append({"path": path, **payload})
        return {"content": self.answer, "tokens_evaluated": 1}


def test_chat_sends_the_template_and_strips_thinking():
    client = RecordingLlm("chatml", answer="<think>x</think>Short answer")
    assert client.chat("Question?", system="Sys.", n_predict=20) == "Short answer"
    sent = client.sent[0]
    assert sent["prompt"].startswith("<|im_start|>system\nSys.<|im_end|>")
    assert sent["stop"] == llm_mod.CHATML_STOP and sent["n_predict"] == 20


def test_raw_format_sends_the_plain_text_and_the_bloomz_stops():
    client = RecordingLlm("raw", answer=" plain ")
    assert client.chat("Question?", system="Sys.") == "plain"
    assert client.sent[0]["prompt"] == "Sys.\n\nQuestion?"
    assert client.sent[0]["stop"] == llm_mod.RAW_STOP


def test_chat_json_passes_the_schema_and_rejects_non_objects():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    client = RecordingLlm("chatml", answer='{"a": "x"}')
    assert client.chat_json("Q", schema) == {"a": "x"}
    assert client.sent[0]["json_schema"] == schema
    with pytest.raises(LlmError):
        RecordingLlm("chatml", answer="[1, 2]").chat_json("Q", schema)
    with pytest.raises(LlmError):
        RecordingLlm("chatml", answer="not json").chat_json("Q", schema)


# ---------------------------------------------------------------- settings
def test_settings_read_the_flags(monkeypatch):
    for name in ("LLM_FORMAT", "KEYWORDS_MODE", "EMBEDDINGS", "KEYWORD_TAGGER", "TOPICS_MODE", "QA_RETRIEVAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    s = config.load()
    assert (s.llm_format, s.keywords_mode, s.embeddings, s.keyword_tagger) == ("chatml", "typed", True, False)
    assert (s.topics_mode, s.qa_retrieval, s.openalex_email) == ("embeddings", "bm25", "")
    assert s.areas_exemplars is False
    monkeypatch.setenv("KEYWORD_TAGGER", "on")
    monkeypatch.setenv("LLM_FORMAT", "raw")
    s = config.load()
    assert s.keyword_tagger and s.llm_format == "raw"


def test_settings_reject_unknown_choices(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("QA_RETRIEVAL", "magic")
    with pytest.raises(ValueError):
        config.load()


# ---------------------------------------------------------------- vectors
def test_cosine_nearest_and_centroid():
    assert vectors.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert vectors.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert vectors.cosine([0, 0], [1, 1]) == 0.0
    items = [("a", [1, 0]), ("b", [0.9, 0.1]), ("c", [0, 1])]
    assert [k for k, _ in vectors.nearest([1, 0], items, 2)] == ["a", "b"]
    assert [k for k, _ in vectors.nearest([1, 0], items, 5, exclude="a")] == ["b", "c"]
    assert vectors.centroid([[1, 0], [0, 1]]) == pytest.approx([0.7071, 0.7071], abs=1e-3)


def test_rrf_rewards_keys_ranked_high_in_several_lists():
    assert vectors.rrf([["a", "b", "c"], ["b", "a", "d"]])[:2] == ["a", "b"]  # equal scores: sorted by key
    assert vectors.rrf([["x", "y"], ["z"]])[0] == "x"  # equal top ranks: sorted by key
    assert vectors.rrf([["p"], ["q", "p"]])[0] == "p"  # p is in both lists


def test_average_linkage_merges_similar_items_only():
    sims = {frozenset(("a", "b")): 0.9, frozenset(("b", "c")): 0.8, frozenset(("a", "c")): 0.85,
            frozenset(("d", "e")): 0.7}
    def sim(x, y):
        return sims.get(frozenset((x, y)), 0.1)
    groups = sorted(sorted(g) for g in vectors.average_linkage(["a", "b", "c", "d", "e"], sim, 0.6))
    assert groups == [["a", "b", "c"], ["d", "e"]]
    with pytest.raises(ValueError):
        vectors.average_linkage([str(i) for i in range(vectors.MAX_CLUSTER_ITEMS + 1)], sim, 0.5)


# ---------------------------------------------------------------- typed keywords
def _doc(title="Pruning heads of transformers", abstract="We prune heads of transformers and study accuracy on a news corpus."):
    return Document(title=title, abstract=[Paragraph(text=abstract)],
                    sections=[Section(title="Methods", paragraphs=[
                        Paragraph(text="We collected the news corpus and evaluated accuracy.", page=2)])])


class TypedLlm:
    chat_format = "chatml"

    def __init__(self, answer):
        self.answer = answer

    def truncate(self, text, n):
        return text

    def chat_json(self, user, schema, **kw):
        assert schema["required"] == ["task", "methods", "datasets", "topics", "keywords"]
        return self.answer


def test_typed_terms_are_checked_like_the_legacy_answers():
    answer = {"task": "How can we prune heads?", "methods": ["prune heads", "analyze the platform's mechanisms"],
              "datasets": ["news corpus", "general"], "topics": ["transformers"], "keywords": ["accuracy", "data"]}
    terms = {(t.kind, t.term) for t in enrich.typed_terms(_doc(), TypedLlm(answer))}
    assert ("task", "prune heads") in terms
    assert ("method", "prune heads") in terms
    assert ("dataset", "news corpus") in terms
    assert not any(t == "analyze the platform's mechanisms" for _, t in terms)  # an instruction
    assert ("dataset", "general") not in terms  # generic
    assert ("keyword", "data") not in terms


def test_tagger_terms_are_grounded_and_weighted_below_the_llm():
    found = enrich.tagger_terms(_doc(), lambda body: ["pruning heads", "news corpus", "unseen phrase"])
    assert {t.term for t in found} == {"pruning heads", "news corpus"}  # "unseen phrase" is not in the text
    assert all(t.source == "tagger" and t.weight == enrich.TAGGER_WEIGHT for t in found)


def test_extract_keeps_the_llm_version_of_a_term_both_found():
    answer = {"task": "", "methods": [], "datasets": [], "topics": [], "keywords": ["news corpus"]}
    terms = enrich.extract(_doc(), TypedLlm(answer), typed=True, tagger=lambda body: ["news corpus"])
    kept = [t for t in terms if t.term == "news corpus"]
    assert len(kept) == 1 and kept[0].source == "llm"


def test_tagger_failures_do_not_stop_the_keywords():
    def broken(body):
        raise RuntimeError("model gone")
    assert enrich.tagger_terms(_doc(), broken) == []


def test_tagger_output_becomes_keyphrases():
    entities = [{"entity_group": "KEY", "word": "pruning heads"}, {"entity_group": "O", "word": "the"},
                {"entity_group": "KEY", "word": "Transformers"}]
    assert tagger.spans_to_terms(entities) == ["pruning heads", "transformers"]
    assert tagger.chunks("a b c d") == ["a b c d"]
    assert len(tagger.chunks(" ".join(["w"] * 400))) == 3


# ---------------------------------------------------------------- areas by exemplars
def _placement(primary, confidence="low", also=()):
    return areas.Placement(primary=primary, also=list(also), confidence=confidence, source="llm")


def test_exemplars_overrule_an_unsure_model_when_they_agree():
    near = [("social", 0.9), ("social", 0.88), ("social", 0.8), ("computer_science", 0.7), ("social", 0.6)]
    placed, agreement = classify.settle(_placement("computer_science"), near)
    assert placed.primary == "social" and placed.source == "exemplars" and placed.confidence == "medium"
    assert placed.also == ["computer_science"]
    assert agreement == 0.8  # four of the five exemplars are in the placed area


def test_a_confident_model_is_not_overruled_but_its_agreement_is_kept():
    near = [("social", 0.9)] * 4 + [("computer_science", 0.7)]
    placed, agreement = classify.settle(_placement("computer_science", confidence="high"), near)
    assert placed.primary == "computer_science" and placed.source == "llm"
    assert agreement == 0.2


def test_no_neighbours_means_no_agreement():
    placed, agreement = classify.settle(_placement("social"), None)
    assert placed.primary == "social" and agreement is None


def test_neighbour_areas_pick_the_nearest_exemplars_and_skip_the_paper_itself():
    pool = [("p1", "social", [1.0, 0.0]), ("p2", "arts", [0.0, 1.0]), ("self", "other", [1.0, 0.0])]
    near = classify.neighbour_areas([1.0, 0.0], pool, "self")
    assert [a for a, _ in near] == ["social", "arts"]
    assert classify.neighbour_areas(None, pool, "self") is None
    assert "Social Sciences (0.90)" in classify.evidence([("social", 0.9)])


def test_a_prompt_with_evidence_is_sent_to_the_model():
    seen = {}

    class Model:
        def truncate(self, text, n):
            return text

        def chat_json(self, user, schema, **kw):
            seen["user"] = user
            return {"primary": areas.NAMES["social"], "also": [], "confidence": "high"}

    placed = classify.llm_placement(Model(), "Title: x", hint="Papers the user filed as similar: a.")
    assert placed.primary == "social" and "Papers the user filed" in seen["user"]


# ---------------------------------------------------------------- topics from paper vectors
def test_build_papers_clusters_by_vector_and_names_from_titles():
    vec = {"a": [1.0, 0.0], "b": [0.98, 0.2], "c": [0.0, 1.0], "d": [0.05, 0.99]}
    keywords = {"a": {"echo", "instagram"}, "b": {"echo", "algorithm"}, "c": {"sampling"}, "d": {"sampling", "network"}}
    titles = {"a": "Echo on Instagram", "b": "Algorithms and echo chambers", "c": "Sampling networks",
              "d": "Survey sampling"}

    class Namer:
        def chat(self, user, **kw):
            assert "Echo on Instagram" in user or "Sampling networks" in user
            return "Echo chambers" if "Echo" in user else "Network sampling"

    built = topics.build_papers(vec, keywords, titles, Namer())
    names = {tuple(members): name for name, _, members in built}
    assert names[("a", "b")] == "Echo chambers"
    assert names[("c", "d")] == "Network sampling"
    terms_of = {tuple(members): terms for _, terms, members in built}
    assert "echo" in terms_of[("a", "b")]


def test_build_papers_without_a_model_names_by_the_first_term():
    vec = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
    built = topics.build_papers(vec, {"a": {"echo"}, "b": {"echo"}}, {"a": "x", "b": "y"}, None)
    assert built == [("Echo", ["echo"], ["a", "b"])]


# ---------------------------------------------------------------- QA retrieval
class QaEncoder:
    """Vectors by hand: every question is about the same topic as the 'sunset' passage, and no other passage is."""

    def query_vector(self, question):
        return [1.0, 0.0]

    def passage_vectors(self, texts):
        return [[1.0, 0.0] if "sunset" in t else [0.0, 1.0] for t in texts]


def test_hybrid_retrieval_brings_in_passages_that_share_no_words():
    doc = Document(title="x", abstract=[], sections=[Section(title="S", paragraphs=[
        Paragraph(text="The sunset colours were recorded every evening.", page=3),
        Paragraph(text="Participants filled in a questionnaire about trust.", page=4),
        Paragraph(text="Questionnaire responses were coded twice.", page=5)])])
    items = qa.passages(doc)
    question = "Which hue dominated dusk?"  # no word of the passage appears in it
    assert 3 not in [p.page for p in qa.retrieve(question, items)]  # keyword search alone misses it
    hybrid = [p.page for p in qa.retrieve(question, items, QaEncoder())]
    assert hybrid[0] == 3
    assert len(hybrid) <= qa.TOP_K_HYBRID


def test_answers_cite_only_passages_in_the_prompt():
    result = qa.clean("Trust was low [1] and the coding was double [9].", 2)
    text, cited = result
    assert cited == [1]
    assert "[9]" not in text


# ---------------------------------------------------------------- OpenAlex
def test_openalex_matches_the_exact_title_and_keeps_its_topics():
    results = [{"id": "W1", "display_name": "Something else", "topics": []},
               {"id": "W2", "display_name": "Breaking the bubble: A case study",
                "topics": [{"display_name": "Social Media Influence", "score": 0.9123,
                            "subfield": {"display_name": "Communication"}, "field": {"display_name": "Social Sciences"},
                            "domain": {"display_name": "Social Sciences"}}]}]
    work = openalex.pick(results, "Breaking the bubble: a case study")
    assert work["id"] == "W2"
    assert openalex.topics_of(work) == [{"name": "Social Media Influence", "subfield": "Communication",
                                         "field": "Social Sciences", "domain": "Social Sciences", "score": 0.912}]
    assert openalex.pick(results, "Unrelated title") is None
    assert openalex.filter_title("Echo, chambers | on Instagram") == "Echo  chambers   on Instagram"


def test_openalex_lookup_sends_the_title_and_the_contact(monkeypatch):
    import httpx

    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert openalex.lookup(client, "Echo chambers, revisited", "me@example.org") is None
    assert seen["params"]["mailto"] == "me@example.org"
    assert seen["params"]["filter"] == "title.search:Echo chambers  revisited"


# ---------------------------------------------------------------- database: vectors and related papers
def _paper(conn, title, abstract=""):
    return conn.execute("INSERT INTO documents (source_type, title, abstract) VALUES ('pdf', %s, %s) RETURNING id",
                        (title, abstract)).fetchone()["id"]


def test_related_papers_are_the_nearest_vectors(clean_db):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(clean_db, autocommit=True, row_factory=dict_row) as conn:
        a = _paper(conn, "Echo chambers on Instagram")
        b = _paper(conn, "Filter bubbles on Instagram")
        c = _paper(conn, "Sampling networks in the field")
        embeddings.store(conn, a, [1.0, 0.0, 0.0])
        embeddings.store(conn, b, [0.9, 0.1, 0.0])
        embeddings.store(conn, c, [0.0, 0.0, 1.0])
        related = embeddings.related(conn, str(a), k=2)
        assert [r["title"] for r in related] == ["Filter bubbles on Instagram", "Sampling networks in the field"]
        assert related[0]["similarity"] > related[1]["similarity"]
        missing = embeddings.missing(conn)
        assert {r["id"] for r in missing} == set()  # all three have a vector of the current model
        d = _paper(conn, "No vector yet")
        assert [r["id"] for r in embeddings.missing(conn)] == [d]


def test_a_paper_without_a_vector_has_no_related_papers(clean_db):
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(clean_db, autocommit=True, row_factory=dict_row) as conn:
        a = _paper(conn, "Only paper")
        assert embeddings.related(conn, str(a)) == []


# ---------------------------------------------------------------- the real models (skipped without the files)
MODELS = Path(os.environ.get("MODELS_DIR", "/models"))


def _has(name):
    return (MODELS / "embed" / name / "config.json").exists()


@pytest.mark.skipif(not _has("specter2_base") or not (MODELS / "embed" / "specter2_adapter").exists(),
                    reason="SPECTER2 is not installed")
def test_specter2_puts_papers_on_the_same_topic_closer():
    pytest.importorskip("torch")
    pytest.importorskip("adapters")
    from listen.worker import embed

    model = embed.specter(MODELS)
    vecs = model.paper_vectors([
        ("Breaking the bubble: A case study on the echo chamber effect in Instagram",
         "The echo chamber effect on Instagram reinforces similar views through algorithmic filtering."),
        ("QUANTIFICATION OF ECHO CHAMBERS in online social networks",
         "Echo chambers in online social networks polarise users; we quantify them with a framework."),
        ("Measuring Networks in the Field",
         "Surveys that elicit social networks need careful design; small samples mislead network inference."),
    ])
    assert len(vecs) == 3 and len(vecs[0]) == 768
    assert vectors.cosine(vecs[0], vecs[1]) > vectors.cosine(vecs[0], vecs[2])


@pytest.mark.skipif(not _has("bge-small"), reason="the passage model is not installed")
def test_passage_model_ranks_the_answering_passage_first():
    pytest.importorskip("torch")
    from listen.worker import embed

    enc = embed.passages(MODELS)
    passages = ["Participants were recruited from three universities in 2019.",
                "The framework quantifies echo chambers with a modularity score."]
    q = enc.query_vector("How are echo chambers measured?")
    scores = [vectors.cosine(q, v) for v in enc.passage_vectors(passages)]
    assert scores[1] > scores[0]
