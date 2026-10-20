import psycopg
from psycopg.rows import dict_row

from listen import jobs
from listen.storage import DocPaths
from listen.worker import qa
from listen.worker.docmodel import Document, Paragraph, Section
from listen.worker.main import Worker
from listen.worker.pipeline import Pipeline


class QaLlm:
    """Stands in for the llama.cpp server with a fixed answer."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def wait_ready(self, timeout_s=0):
        return True

    def count(self, text):
        return len(text.split())

    def truncate(self, text, n):
        return " ".join(text.split()[:n])

    def chat(self, user, *, system=None, n_predict=160, temperature=0.2, stop=None, cache_prompt=False, feature=""):
        return self.complete(user, n_predict, temperature)

    def chat_json(self, user, schema, *, system=None, n_predict=300, cache_prompt=False, feature=""):
        return self.complete_json(user, schema, n_predict)

    def complete(self, prompt, n_predict=160, temperature=0.2):
        self.prompts.append(prompt)
        return self.answer


def _owner(conn) -> int:
    return conn.execute("INSERT INTO users (email) VALUES ('qa@example.org') RETURNING id").fetchone()["id"]


def _paper():
    return Document(
        abstract=[Paragraph("We study how attention heads specialise during pre-training.", page=1)],
        sections=[
            Section(title="Methods", paragraphs=[
                Paragraph("Head pruning removes attention heads ranked by their importance on the validation set. "
                          "Pruning is repeated for every layer of the network.", page=2),
                Paragraph("The optimiser uses a learning rate schedule with warm-up steps.", page=2),
            ]),
            Section(title="Results", paragraphs=[
                Paragraph("Accuracy drops only slightly after pruning half of the heads in the model.", page=3),
            ]),
        ],
    )


def test_bm25_puts_the_matching_passage_first():
    items = qa.passages(_paper())
    best = qa.rank("How are attention heads pruned?", items)
    assert best and "pruning" in best[0].text.lower() and best[0].page == 2
    assert qa.rank("zebra", items) == []  # no shared words: nothing to cite


def test_clean_keeps_valid_citations_and_drops_invented_ones():
    text, cited = qa.clean("Answer: Heads are pruned by importance [1]. Layers repeat it [9].", 2)
    assert text == "Heads are pruned by importance [1]. Layers repeat it."
    assert cited == [1]
    assert qa.clean("yes", 2) is None                                   # too short
    assert qa.clean("the the the the the the the the the the the the the the", 2) is None  # loop
    assert qa.clean("Use the passages above to answer the question.", 2) is None           # echoed prompt


def test_answer_cites_the_passage_it_used():
    llm = QaLlm("Head pruning ranks heads by importance on validation data [1].")
    text, citations = qa.answer(llm, _paper(), "How are attention heads pruned?")
    assert text.startswith("Head pruning")
    assert citations[0]["page"] == 2 and citations[0]["n"] == 1
    assert "Passages from a scientific paper" in llm.prompts[0] and "Question: How are" in llm.prompts[0]


def test_answer_without_citation_points_at_the_best_passage():
    text, citations = qa.answer(QaLlm("Heads with the least importance are removed first."),
                                _paper(), "Which heads are pruned?")
    assert citations and citations[0]["page"] == 2


def test_question_is_answered_by_a_qa_job_and_the_document_stays_ready(settings, pool):
    with pool.connection() as conn:
        doc_id = conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('pdf', 'P', 'ready') "
                              "RETURNING id").fetchone()["id"]
        q = conn.execute("INSERT INTO questions (document_id, question) VALUES (%s, 'How are heads pruned?') "
                         "RETURNING id", (doc_id,)).fetchone()["id"]
        jobs.enqueue_question(conn, doc_id, q, _owner(conn))
    paths = DocPaths(settings.docs_dir, doc_id).ensure()
    _paper().save(paths.structure)

    llm = QaLlm("Head pruning ranks heads by importance [1].")
    worker = Worker(settings, pool, Pipeline(settings, pool, llm=llm))
    assert worker.run_once()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM questions").fetchone()
        assert row["status"] == "done" and row["answer"].startswith("Head pruning")
        assert row["citations"][0]["page"] == 2
        doc = conn.execute("SELECT status FROM documents").fetchone()
        assert doc["status"] == "ready"  # a question never changes the processing status
        assert conn.execute("SELECT state FROM jobs").fetchone()["state"] == "done"


def test_question_without_model_is_marked_failed(settings, pool):
    with pool.connection() as conn:
        doc_id = conn.execute("INSERT INTO documents (source_type, title, status) VALUES ('pdf', 'P', 'ready') "
                              "RETURNING id").fetchone()["id"]
        q = conn.execute("INSERT INTO questions (document_id, question) VALUES (%s, 'How?') RETURNING id",
                         (doc_id,)).fetchone()["id"]
        jobs.enqueue_question(conn, doc_id, q, _owner(conn))
    DocPaths(settings.docs_dir, doc_id).ensure()
    _paper().save(DocPaths(settings.docs_dir, doc_id).structure)

    class Down(QaLlm):
        def wait_ready(self, timeout_s=0):
            return False

    worker = Worker(settings, pool, Pipeline(settings, pool, llm=Down("x")))
    assert worker.run_once()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        row = conn.execute("SELECT status, error FROM questions").fetchone()
        assert row["status"] == "failed" and "not running" in row["error"]
