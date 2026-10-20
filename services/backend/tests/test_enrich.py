import json

import httpx
import pytest

from listen.worker import enrich
from listen.worker.docmodel import Document, Paragraph, Section
from listen.worker.llm import LlmClient, LlmError


class FakeLlm:
    """Stands in for the llama.cpp server. `answers` maps a question's first words to the text it returns."""

    def __init__(self, answers=None, keywords=None, fail=False):
        self.answers = answers or {}
        self.keywords = keywords or {"topics": [], "keywords": []}
        self.fail = fail
        self.prompts = []

    def truncate(self, text, n):
        return " ".join(text.split()[:n])

    def chat(self, user, *, system=None, n_predict=160, temperature=0.2, stop=None, cache_prompt=False, feature=""):
        return self.complete(user, n_predict, temperature)

    def chat_json(self, user, schema, *, system=None, n_predict=300, cache_prompt=False, feature=""):
        return self.complete_json(user, schema, n_predict)

    def complete(self, prompt, n_predict=160, temperature=0.2, *, stop=None, cache_prompt=False):
        self.prompts.append(prompt)
        if self.fail:
            raise LlmError("boom")
        assert cache_prompt, "the excerpt is shared by the field questions, so the cache must be used"
        for key, text in self.answers.items():
            if key in prompt.rsplit("Question:", 1)[-1]:
                return text
        return ""

    def complete_json(self, prompt, schema, n_predict=300, *, cache_prompt=False):
        self.prompts.append(prompt)
        if self.fail:
            raise LlmError("boom")
        return self.keywords


def _doc():
    return Document(
        title="Attention heads and translation",
        abstract=[Paragraph("We prune attention heads in transformers to measure accuracy on translation.")],
        sections=[Section(title="Methods", paragraphs=[
            Paragraph("Pruning removes heads. Pruning is applied to every layer. " * 3),
        ])],
    )


def test_clean_terms_normalizes_and_limits():
    raw = ["  Attention Heads. ", "attention heads", 42, "x", "Pruning", "", "Layer Norm"]
    assert enrich.clean_terms(raw, 3) == ["attention heads", "pruning", "layer norm"]


def test_frequency_terms_skip_generic_words():
    terms = enrich.frequency_terms("The results show that pruning heads helps. Pruning heads again, pruning.", 3)
    assert terms[0] == "pruning" and "results" not in terms and "that" not in terms


def test_grounded_needs_the_phrase_or_all_its_words():
    text = "We run a controlled experiment on pruned heads."
    assert enrich.grounded("controlled experiment", text)
    assert enrich.grounded("experiment on heads", text)  # every content word is in the text
    assert not enrich.grounded("present the main theories", text)


def test_instructions_generic_words_and_long_answers_are_rejected():
    paper = "Attention heads for translation."
    body = paper + " We present the main theories of network science."
    assert not enrich.usable("present the main theories", "method", paper, body)
    assert not enrich.usable("data", "dataset", paper, body)
    assert not enrich.usable("the paper uses heads", "method", paper, body)
    assert not enrich.usable("a b c d e f g", "method", paper, body)
    assert enrich.usable("attention heads", "method", paper, body)


def test_task_may_be_paraphrased_but_must_share_words_with_the_abstract():
    paper = "Transformers prune attention heads to measure translation accuracy."
    assert enrich.usable("pruning attention heads for translation", "task", paper, paper)
    assert not enrich.usable("economic policy in europe", "task", paper, paper)


def test_llm_terms_are_grounded_and_each_field_is_asked():
    llm = FakeLlm(answers={
        "research question": "Machine translation",
        "research methods": "attention head pruning, present the main theories, controlled experiment",
        "dataset, sample or corpus": "translation corpus, none",
    }, keywords={"topics": ["Transformers", "invented topic"], "keywords": ["pruning", "heads"]})
    terms = enrich.extract(_doc(), llm)
    found = {(t.kind, t.term) for t in terms if t.source == "llm"}
    assert ("task", "machine translation") in found
    assert ("method", "attention head pruning") not in found  # "head pruning" is grounded only word by word
    assert ("method", "controlled experiment") not in found  # not in this paper's text
    assert ("dataset", "translation corpus") not in found  # not in this paper's text
    assert ("topic", "transformers") in found and ("topic", "invented topic") not in found
    assert ("keyword", "pruning") in found
    assert len(llm.prompts) == 4  # task, methods, datasets, keywords


def test_each_term_belongs_to_one_field():
    terms = enrich.dedupe([
        enrich.Term("dataset", "twitter", "rule", 1.0),
        enrich.Term("method", "twitter", "llm", 1.0),
        enrich.Term("keyword", "pruning", "frequency", 0.5),
    ])
    assert [(t.kind, t.term) for t in terms] == [("method", "twitter"), ("keyword", "pruning")]


def test_rules_fill_gaps_left_by_the_model():
    body = ("We collected data from Twitter in 2017. The sample used the German federal election dataset. "
            "We ran a controlled experiment and a controlled experiment again. We fit a regression.")
    assert "twitter" in enrich.dataset_rules(body)
    assert "german federal election" in enrich.dataset_rules(body)
    methods = enrich.method_rules(body)
    assert "controlled experiment" in methods and "experiment" not in methods  # the longer name wins
    assert "regression" not in methods  # mentioned once only


def test_extract_survives_llm_failure_with_rules_and_frequency():
    doc = _doc()
    doc.sections[0].paragraphs.append(Paragraph("Data were collected from Reddit. We ran a survey and a survey."))
    terms = enrich.extract(doc, FakeLlm(fail=True))
    assert {"rule", "frequency"} == {t.source for t in terms}
    assert ("dataset", "reddit") in {(t.kind, t.term) for t in terms}


def test_extract_without_llm_uses_text_only():
    assert {t.source for t in enrich.extract(_doc(), None)} <= {"frequency", "rule"}


def test_excerpt_reads_the_method_sections():
    doc = Document(
        abstract=[Paragraph("A study of heads.")],
        sections=[
            Section(title="Introduction", paragraphs=[Paragraph("Heads matter. More is said here. And here.")]),
            Section(title="Data", paragraphs=[Paragraph("Intro line. We collected posts from Reddit. Unrelated tail.")]),
        ],
    )
    text = enrich.excerpt(doc, FakeLlm())
    assert "We collected posts from Reddit." in text
    assert "And here." not in text  # introduction: only the first two sentences


def test_complete_json_parses_or_raises():
    def handler(request: httpx.Request):
        payload = json.loads(request.read())
        assert payload["json_schema"] == enrich.KEYWORD_SCHEMA  # the grammar is sent with the prompt
        content = '{"topics": ["x"]}' if payload["prompt"] == "ok" else "not json"
        return httpx.Response(200, json={"content": content})

    client = LlmClient("http://llm")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.complete_json("ok", enrich.KEYWORD_SCHEMA) == {"topics": ["x"]}
    with pytest.raises(LlmError):
        client.complete_json("bad", enrich.KEYWORD_SCHEMA)


def _elements_doc():
    return Document(sections=[Section(title="Results", paragraphs=[
        Paragraph("Accuracy drops once more heads are removed. Figure 2 shows the drop in detail. "
                  "Table 1 lists the exact numbers for each setting.", page=2),
        Paragraph("Table 1: Accuracy per setting.", page=3, kind="table",
                  rows="setting | accuracy\nbase | 91.2\npruned | 88.0"),
        Paragraph("Figure 2: Accuracy as heads are removed.", page=3, kind="figure"),
        Paragraph("Figure 9: Unmentioned figure.", page=4, kind="figure"),
    ])])


def test_element_summaries_use_rows_or_citing_text():
    doc = _elements_doc()
    llm = FakeLlm()
    llm.complete = lambda prompt, n_predict=160, temperature=0.2: (
        "The table compares accuracy for the base and the pruned model across settings.")
    llm.truncate = lambda text, n: text
    assert enrich.element_summaries(doc, llm) == 2
    table, figure, unmentioned = doc.sections[0].paragraphs[1:]
    assert table.summary.startswith("The table compares")
    assert figure.summary.startswith("The table compares")  # the LLM is fake; what matters is the input below
    assert unmentioned.summary is None  # nothing in the text refers to Figure 9
    assert enrich._citations(doc, "Figure 2") == ["Figure 2 shows the drop in detail."]


def test_element_summary_prompts_contain_rows_and_citations():
    doc = _elements_doc()
    prompts = []
    llm = FakeLlm()
    llm.complete = lambda prompt, n_predict=160, temperature=0.2: (prompts.append(prompt) or
                                                                   "A clear and specific summary of the element.")
    llm.truncate = lambda text, n: text
    enrich.element_summaries(doc, llm)
    assert any("base | 91.2" in p for p in prompts)
    assert any("Figure 2 shows the drop" in p and "Figure 2" in p.splitlines()[-1] for p in prompts)


def test_table_rows_are_read_from_tei():
    from lxml import etree

    from listen.worker import grobid

    fig = etree.fromstring(
        '<figure xmlns="http://www.tei-c.org/ns/1.0" type="table" coords="3,50,80,300,40">'
        '<head>Table 1</head><figDesc>Accuracy per setting.</figDesc>'
        '<table><row><cell>setting</cell><cell>accuracy</cell></row>'
        '<row><cell>base</cell><cell>91.2</cell></row><row><cell></cell></row></table></figure>')
    para = grobid._figure(fig)
    assert para.kind == "table" and para.page == 3
    assert para.rows == "setting | accuracy\nbase | 91.2"
