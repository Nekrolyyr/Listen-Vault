import psycopg
from psycopg.rows import dict_row

from listen.worker import topics


class NamingLlm:
    """Names every topic 'Pruning Methods' and rejects 'sleep' from any topic it is asked about."""

    def __init__(self):
        self.prompts = []

    def wait_ready(self, timeout_s=0):
        return True

    def complete(self, prompt, n_predict=160, temperature=0.2):
        self.prompts.append(prompt)
        if prompt.startswith("Keywords from scientific papers"):
            return "Pruning methods."
        if "do not belong" in prompt:
            return "sleep, none" if "sleep" in prompt else "none"
        return ""


DOC_TERMS = {
    "d1": {"head pruning", "weight pruning", "sparsity", "sleep"},
    "d2": {"head pruning", "weight pruning", "sparsity"},
    "d3": {"weight pruning", "sparsity", "head pruning", "memory"},
    "d4": {"sleep consolidation", "memory"},
    "d5": {"sleep consolidation", "memory", "sleep"},
}


def test_terms_in_one_paper_do_not_define_a_topic():
    vecs = topics.term_vectors({"d1": {"solo"}, "d2": {"other"}})
    assert vecs == {}


def test_topics_are_named_and_checked_by_the_llm():
    llm = NamingLlm()
    built = topics.build(DOC_TERMS, llm)
    names = [name for name, _ in built]
    assert "Pruning methods" in names
    pruning = next(terms for name, terms in built if name == "Pruning methods")
    assert {"head pruning", "weight pruning", "sparsity"} <= set(pruning)
    # the membership check was asked for each cluster
    assert any("do not belong" in p for p in llm.prompts)


def test_topics_without_llm_are_named_after_their_common_term():
    built = topics.build(DOC_TERMS, None)
    assert built and all(name[0].isupper() for name, _ in built)
    assert all(len(terms) >= topics.MIN_TOPIC_TERMS for _, terms in built)


def test_clean_name_rejects_sentences_and_echoes():
    assert topics.clean_name("sparse attention heads.") == "Sparse attention heads"
    assert topics.clean_name("This is a very long answer that is not a short name at all") is None
    assert topics.clean_name("keywords") is None


def test_rebuild_writes_topics_for_the_api(settings, pool):
    with pool.connection() as conn, conn.transaction():
        for doc, terms in DOC_TERMS.items():
            doc_id = conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('url', %s, 'ready') "
                                  "RETURNING id", (doc,)).fetchone()["id"]
            for term in terms:
                conn.execute("INSERT INTO document_keywords (document_id, kind, term, source) "
                             "VALUES (%s, 'keyword', %s, 'llm')", (doc_id, term))
    count = topics.rebuild(pool, NamingLlm())
    assert count >= 1
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM topics").fetchall()]
    assert "Pruning methods" in names
