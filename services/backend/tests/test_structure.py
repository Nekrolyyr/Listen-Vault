from listen.worker import article, chapters, grobid
from listen.worker.docmodel import Document, Paragraph, Section

from .conftest import FIXTURES, make_pdf


def test_parse_tei():
    doc = grobid.parse_tei((FIXTURES / "sample.tei.xml").read_bytes())
    assert doc.title == "Sparse Attention Heads Carry Most of the Signal"
    assert doc.authors == ["Ada Lovelace", "Alan M Turing"]
    assert doc.published == "2024-05-02"
    assert [p.text for p in doc.abstract] == [
        "We study how attention heads specialise during pre-training. "
        "A small subset of heads accounts for most downstream gains."
    ]
    titles = [s.title for s in doc.sections]
    assert titles == ["Introduction", "Methods: Data", "Results", "Acknowledgements"]
    intro = doc.sections[0].paragraphs[0]
    # citations and footnotes removed, page from coordinates
    assert intro.text == ("Transformers dominate sequence modelling. "
                          "Prior work pruned heads by weight magnitude.")
    assert intro.page == 1
    data = doc.sections[1]
    assert [p.kind for p in data.paragraphs] == ["text", "equation", "text"]
    assert data.level == 2
    # the body-level figure is read after the last paragraph on or before page 3
    results = doc.sections[2].paragraphs
    assert results[-1].kind == "figure"
    assert results[-1].text == "Fig. 1: Accuracy as heads are removed."
    assert results[-1].page == 3


def test_chapter_plan_from_tei():
    doc = grobid.parse_tei((FIXTURES / "sample.tei.xml").read_bytes())
    plans = chapters.plan(doc)
    assert [p.kind for p in plans] == ["overview", "chapter", "chapter"]
    assert plans[1].title == "Abstract"
    # tiny sections are merged into one chapter; acknowledgements are skipped
    body = plans[2]
    assert body.title == "Introduction"
    assert [p.text for p in body.paragraphs if p.kind == "heading"] == ["Methods: Data", "Results"]
    assert "We thank the reviewers." not in body.text
    assert body.start_page == 1


def _section(title, words, level=1, page=1):
    return Section(title=title, level=level, paragraphs=[Paragraph(text="word " * words, page=page)])


def test_chapter_plan_merges_and_splits():
    doc = Document(sections=[
        _section("1 Introduction", 400),
        _section("1.1 Background", 60, level=2),
        Section(title="2 Method", paragraphs=[Paragraph("word " * 100, page=3) for _ in range(30)]),
        _section("References", 500),
        _section(None, 200, page=9),
    ])
    plans = chapters.plan(doc)
    assert [p.title for p in plans] == ["Overview", "Introduction", "Method, part 1", "Method, part 2", ""]
    assert plans[1].word_count == 461  # Background merged in, plus its heading word
    assert plans[2].start_page == 3


def test_accept_rejects_bad_answers():
    assert chapters.accept("Summary: the model works well on all tasks", 3, 40) == "The model works well on all tasks."
    assert chapters.accept("yes", 3, 40) is None
    assert chapters.accept("the the the the the the the the the the", 3, 40) is None
    assert chapters.accept("Summarize the text above in two sentences", 3, 40) is None
    long = ("Attention heads specialise early in training. Pruning them hurts accuracy a lot. "
            "Random pruning has almost no effect at all. The authors propose a new criterion.")
    assert chapters.accept(long, 3, 15) == ("Attention heads specialise early in training. "
                                            "Pruning them hurts accuracy a lot.")


class FakeLlm:
    """Stands in for the llama.cpp server; 1 token per word."""

    def __init__(self, answers=None):
        self.prompts = []
        self.answers = answers

    def count(self, text):
        return len(text.split())

    def truncate(self, text, n):
        return " ".join(text.split()[:n])

    def complete(self, prompt, n_predict=160, temperature=0.2):
        self.prompts.append(prompt)
        if self.answers is not None:
            return self.answers.pop(0) if self.answers else ""
        if "title" in prompt:
            return "Attention Pruning"
        return "The section explains how attention heads are pruned. It reports the effect on accuracy."


def test_enricher_map_reduce_and_titles():
    doc = Document(abstract=[Paragraph("We prune attention heads and measure accuracy.")], sections=[
        Section(title=None, paragraphs=[Paragraph("This is a sentence about heads. " * 400)]),
    ])
    plans = chapters.plan(doc)
    llm = FakeLlm()
    chapters.Enricher(llm).run(doc, plans)
    body = plans[-1]
    assert body.title == "Attention Pruning"
    assert body.intro.startswith("The section explains")
    # 2400 words > 1200-token budget: two map calls, one reduce call, plus title and overview
    assert sum("in 2 sentences" in p for p in llm.prompts) == 3
    assert plans[0].intro_source == "llm"


def test_enricher_falls_back_to_extractive():
    doc = Document(abstract=[Paragraph("First point. Second point. Third point. Fourth point. Fifth.")],
                   sections=[Section(title="Intro", paragraphs=[Paragraph("Words here. " * 100)])])
    plans = chapters.plan(doc)
    chapters.Enricher(FakeLlm(answers=[])).run(doc, plans)
    assert plans[0].intro == "First point. Second point. Third point. Fourth point."
    assert plans[0].intro_source == "extractive"
    assert plans[1].intro is None


def test_pdf_fallback(tmp_path):
    from listen.worker import pdf_fallback

    pdf = make_pdf(tmp_path / "paper.pdf", [
        [(20, "A Study of Things"), (10, "Abstract"), (10, "We study things carefully."),
         (14, "1 Introduction"), (10, "Things matter a great deal."), (8, "Journal of Things 12")],
        [(10, "More about things on page two."), (14, "2 Results"), (10, "Things improved."),
         (8, "Journal of Things 13")],
        [(10, "Even more results."), (14, "References"), (10, "[1] A. Person. Things. 2020."),
         (8, "Journal of Things 14")],
    ])
    doc = pdf_fallback.parse_pdf(pdf)
    assert doc.title == "A Study of Things"
    assert [p.text for p in doc.abstract] == ["We study things carefully."]
    assert [s.title for s in doc.sections] == ["1 Introduction", "2 Results"]
    assert doc.sections[0].paragraphs[-1].page == 2
    all_text = " ".join(p.text for s in doc.sections for p in s.paragraphs)
    assert "Journal of Things" not in all_text and "Person" not in all_text


ARTICLE = """<html><head><title>How sleep consolidates memory</title>
<meta name="author" content="Jane Doe"><meta property="article:published_time" content="2024-02-01"></head>
<body><nav>Home | News</nav><article><h1>How sleep consolidates memory</h1>
<p>Sleep is not a passive state. During deep sleep the brain replays the experiences of the day,
strengthening some memories while letting others fade away completely over time.</p>
<h2>The replay hypothesis</h2>
<p>Researchers recorded neurons in the hippocampus of rats and saw the same firing sequences
during sleep that appeared when the animals explored a maze earlier in the day.</p>
<p>Blocking this replay impaired learning the next day, which suggests replay is necessary
for consolidation and not just a side effect of neural activity during rest.</p>
<h2>What it means for people</h2>
<p>For students, the practical lesson is simple: a good night of sleep after studying helps
the material stick much better than staying up late to cram for the exam.</p>
</article><footer>Copyright 2024</footer></body></html>"""


def test_parse_article():
    doc = article.parse_html(ARTICLE, "https://example.org/sleep")
    assert doc.title == "How sleep consolidates memory"
    assert doc.authors == ["Jane Doe"]
    assert [s.title for s in doc.sections] == [None, "The replay hypothesis", "What it means for people"]
    assert doc.sections[1].paragraphs[0].text.startswith("Researchers recorded neurons")
    assert "Copyright" not in " ".join(p.text for s in doc.sections for p in s.paragraphs)
