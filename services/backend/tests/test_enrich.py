import json

import httpx
import pytest

from listen.worker import enrich
from listen.worker.docmodel import Document, Paragraph, Section
from listen.worker.llm import LlmClient, LlmError


class JsonLlm:
    """Stands in for the llama.cpp server with a fixed JSON answer (or a failure)."""

    def __init__(self, answer=None, fail=False):
        self.answer = answer
        self.fail = fail
        self.prompts = []

    def truncate(self, text, n):
        return " ".join(text.split()[:n])

    def complete_json(self, prompt, schema, n_predict=300):
        self.prompts.append(prompt)
        if self.fail:
            raise LlmError("boom")
        return self.answer


def _doc():
    return Document(
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


def test_extract_combines_llm_and_frequency_terms():
    llm = JsonLlm({
        "task": "Machine translation",
        "methods": ["Head pruning"],
        "datasets": [],
        "topics": ["Transformers"],
        "keywords": ["pruning", "attention"],
    })
    terms = enrich.extract(_doc(), llm)
    llm_terms = {(t.kind, t.term) for t in terms if t.source == "llm"}
    assert llm_terms == {("task", "machine translation"), ("method", "head pruning"),
                         ("topic", "transformers"), ("keyword", "pruning"), ("keyword", "attention")}
    frequent = [t for t in terms if t.source == "frequency"]
    assert frequent and all(t.weight == enrich.FREQUENCY_WEIGHT for t in frequent)
    assert "pruning" not in {t.term for t in frequent}  # already known from the LLM
    assert len(llm.prompts) == 1


def test_extract_survives_llm_failure():
    terms = enrich.extract(_doc(), JsonLlm(fail=True))
    assert terms and {t.source for t in terms} == {"frequency"}


def test_extract_without_llm_uses_text_only():
    assert {t.source for t in enrich.extract(_doc(), None)} == {"frequency"}


def test_complete_json_parses_or_raises():
    def handler(request: httpx.Request):
        payload = json.loads(request.read())
        assert payload["json_schema"] == enrich.SCHEMA  # the grammar is sent with the prompt
        content = '{"task": "x"}' if payload["prompt"] == "ok" else "not json"
        return httpx.Response(200, json={"content": content})

    client = LlmClient("http://llm")
    client.http = httpx.Client(transport=httpx.MockTransport(handler))
    assert client.complete_json("ok", enrich.SCHEMA) == {"task": "x"}
    with pytest.raises(LlmError):
        client.complete_json("bad", enrich.SCHEMA)
