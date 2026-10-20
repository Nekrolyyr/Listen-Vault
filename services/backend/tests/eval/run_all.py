"""Score every LLM-backed feature on the labelled papers (docs/optimisation-plan.md, phase 0 and the gates after it).

    python tests/eval/run_all.py --corpus DIR --llm http://llm:8080 --label qwen-typed \
        --keywords typed --tagger off --qa hybrid --areas-exemplars on --topics embeddings \
        --models-dir /models --out-dir tests/eval/results

DIR holds the structure.json of each paper, as <id>/structure.json (the data volume's docs folder) or as plain
*.json files. Papers are matched to the gold files by title. The same paper is often in the library twice; it is
scored once. Nothing here reads or writes the library database.

Options default to the legacy behaviour the baseline was measured with (keywords legacy, qa bm25, areas without
exemplars, topics by keywords, chat format of the client). Gold files, all in this folder:
  extraction_gold.json  task, methods, datasets per paper           (scored as in eval_extraction.py)
  areas_gold.json       primary and other areas per paper           (proposed labels; reviewed: false)
  ask_gold.json         questions, each with the phrase its answer is in (its pages are looked up in the paper)
  topics_gold.json      groups of papers that belong together       (proposed; the topic score needs it)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # services/backend
sys.path.insert(0, str(HERE))

import eval_extraction as ev  # noqa: E402

from listen import vectors  # noqa: E402
from listen.worker import classify, enrich, qa, topics  # noqa: E402
from listen.worker.docmodel import Document  # noqa: E402
from listen.worker.llm import LlmClient  # noqa: E402


def unique_docs(corpus: Path) -> dict[str, Document]:
    """One document per title. The corpus holds each gold paper twice."""
    paths = sorted(corpus.glob("*/structure.json")) or sorted(corpus.glob("*.json"))
    docs: dict[str, Document] = {}
    for path in paths:
        doc = Document.load(path)
        docs.setdefault(doc.title, doc)
    return docs


def matched_papers(docs: dict[str, Document], gold: list[dict]) -> list[tuple[dict, Document]]:
    """(gold entry, document) for every gold paper that is in the corpus."""
    out = []
    for title, doc in docs.items():
        entry = ev.gold_for(title, gold)
        if entry is not None:
            out.append((entry, doc))
    return out


def ratio(a: int, b: int):
    return None if b == 0 else round(a / b, 2)


def abstract_of(doc: Document) -> str:
    return " ".join(p.text for p in doc.abstract)


def run_keywords(matched, llm, kwargs: dict) -> tuple[dict, dict[str, list]]:
    """Scores for the keyword fields, and the terms of each paper as (term, source, kind) (they feed the later steps)."""
    rows, totals = [], {f: dict(tp_pred=0, n_pred=0, tp_gold=0, n_gold=0, junk=0) for f in ev.FIELDS}
    terms_of: dict[str, list] = {}
    for entry, doc in matched:
        t0 = time.time()
        terms = enrich.extract(doc, llm, **kwargs)
        terms_of[doc.title] = [(t.term, t.source, t.kind, t.weight) for t in sorted(terms, key=lambda t: -t.weight)]
        preds = {f: [t.term for t in terms if t.kind == k] for f, k in ev.FIELDS.items()}
        sources = {f: sorted({t.source for t in terms if t.kind == k}) for f, k in ev.FIELDS.items()}
        res = ev.score(preds, entry)
        rows.append({"title": doc.title[:70], "seconds": round(time.time() - t0), "sources": sources, "fields": res})
        for f in ev.FIELDS:
            for key in ("tp_pred", "n_pred", "tp_gold", "n_gold"):
                totals[f][key] += res[f][key]
            totals[f]["junk"] += len(res[f]["junk"])
        print(f"keywords  {doc.title[:60]:60s} {round(time.time() - t0):4d}s", flush=True)
    summary = {f: {"precision": ratio(t["tp_pred"], t["n_pred"]), "recall": ratio(t["tp_gold"], t["n_gold"]),
                   "predicted": t["n_pred"], "gold": t["n_gold"], "junk": t["junk"]} for f, t in totals.items()}
    summary["seconds_per_paper"] = round(sum(r["seconds"] for r in rows) / max(1, len(rows)), 1)
    return {"summary": summary, "papers": rows}, terms_of


def gold_vectors(matched, models_dir: Path) -> dict[str, list[float]]:
    """SPECTER2 vector per paper title (the same model and input as the library)."""
    from listen.worker import embed
    papers = [(doc.title, abstract_of(doc)) for _, doc in matched]
    return dict(zip([doc.title for _, doc in matched], embed.specter(models_dir).paper_vectors(papers)))


def run_areas(matched, areas_gold: list[dict], llm, terms_of: dict[str, list], vec_of: dict | None) -> dict:
    """Area placements. With vec_of, the nearest other gold papers (leave one out, by their gold areas) are the
    exemplars: they are shown to the model and settle an unsure placement, as in the library."""
    gold_by_title = {g["title"]: g for g in areas_gold}
    primary_of = {g["title"]: g["primary"] for g in areas_gold}
    rows = []
    for entry, doc in matched:
        g = gold_by_title.get(doc.title)
        if g is None:
            continue
        keywords = [term for term, _, _, _ in terms_of.get(doc.title, [])]
        sections = [s.title for s in doc.sections if s.title]
        near = None
        if vec_of is not None and doc.title in vec_of:
            pool = [(t, vec_of[t]) for t in primary_of if t != doc.title and t in vec_of]
            ranked = vectors.nearest(vec_of[doc.title], pool, classify.EXEMPLARS)
            near = [(primary_of[t], s) for t, s in ranked] or None
        t0 = time.time()
        placement = classify.place(doc.title, abstract_of(doc), keywords, sections, llm, near)
        placement, agreement = classify.settle(placement, near)
        pred_also, gold_also = set(placement.also), set(g["also"])
        rows.append({
            "title": doc.title[:70], "seconds": round(time.time() - t0),
            "predicted": placement.primary, "gold": g["primary"], "source": placement.source,
            "agreement": agreement, "primary_ok": placement.primary == g["primary"],
            "also_predicted": sorted(pred_also), "also_gold": sorted(gold_also),
            "also_hits": len(pred_also & gold_also),
        })
        print(f"areas     {doc.title[:60]:60s} {placement.primary:14s} gold={g['primary']}", flush=True)
    if not rows:
        return {"summary": None, "papers": []}
    also_hits = sum(r["also_hits"] for r in rows)
    also_pred = sum(len(r["also_predicted"]) for r in rows)
    also_gold = sum(len(r["also_gold"]) for r in rows)
    summary = {"primary_accuracy": ratio(sum(r["primary_ok"] for r in rows), len(rows)), "papers": len(rows),
               "also_precision": ratio(also_hits, also_pred), "also_recall": ratio(also_hits, also_gold),
               "exemplars": vec_of is not None}
    return {"summary": summary, "papers": rows}


def expected_pages(doc: Document, phrase: str) -> set[int]:
    needle = phrase.lower()
    return {p.page for s in doc.sections for p in s.paragraphs if p.page and needle in (p.text or "").lower()}


def run_ask(matched, ask_gold: list[dict], llm, kwargs: dict) -> dict:
    by_title = {p["title"]: p for p in ask_gold}
    rows = []
    for entry, doc in matched:
        spec = by_title.get(doc.title)
        if spec is None:
            continue
        for item in spec["questions"]:
            pages = expected_pages(doc, item["evidence"])
            t0 = time.time()
            result = qa.answer(llm, doc, item["question"], **kwargs)
            seconds = round(time.time() - t0, 1)
            row = {"title": doc.title[:70], "question": item["question"], "expected_pages": sorted(pages),
                   "seconds": seconds, "answered": result is not None, "page_hit": None, "answer": None, "cited": []}
            if result is not None:
                text, cites = result
                cited = {c["page"] for c in cites if c.get("page")}
                row.update(answer=text, cited=sorted(cited))
                if pages:
                    row["page_hit"] = bool(cited & pages)
            rows.append(row)
            print(f"ask       {item['question'][:60]:60s} {seconds:5.1f}s hit={row['page_hit']}", flush=True)
    scored = [r for r in rows if r["page_hit"] is not None]
    summary = {
        "questions": len(rows),
        "answered": ratio(sum(r["answered"] for r in rows), len(rows)),
        "page_hit": ratio(sum(r["page_hit"] for r in scored), len(scored)),
        "scored": len(scored),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1) if rows else None,
    }
    return {"summary": summary, "questions": rows}


def ari(gold: dict[str, int], pred: dict[str, int]) -> float | None:
    """Adjusted Rand index over the papers present in both labelings."""
    papers = [p for p in gold if p in pred]
    if len(papers) < 2:
        return None

    def pairs(n: int) -> float:
        return n * (n - 1) / 2

    cont = Counter((gold[p], pred[p]) for p in papers)
    a = Counter(gold[p] for p in papers)
    b = Counter(pred[p] for p in papers)
    index = sum(pairs(n) for n in cont.values())
    sum_a = sum(pairs(n) for n in a.values())
    sum_b = sum(pairs(n) for n in b.values())
    expected = sum_a * sum_b / pairs(len(papers))
    best = (sum_a + sum_b) / 2
    return None if best == expected else round((index - expected) / (best - expected), 3)


def score_topics(matched, built: list[tuple[str, list[str], list[str]]], topics_gold: dict) -> dict:
    """ARI of the topics against the gold groups. Papers in no topic are each a group of their own."""
    titles = [doc.title for _, doc in matched]
    gold_label: dict[str, int] = {}
    for i, group in enumerate(topics_gold["groups"]):
        for title in group:
            gold_label.setdefault(title, i)
    pred_label: dict[str, int] = {}
    for j, (_, _, members) in enumerate(built):
        for title in members:
            pred_label.setdefault(title, j)
    for k, title in enumerate(titles):
        pred_label.setdefault(title, -1 - k)
    return {"ari": ari(gold_label, pred_label), "topics": len(built), "papers": len(gold_label),
            "list": [{"name": n, "terms": t, "papers": m} for n, t, m in built]}


def run_topics(matched, topics_gold: dict, llm, terms_of: dict[str, list], vec_of: dict | None) -> dict:
    """Keyword topics (vec_of None) or embedding topics, as the library builds them."""
    from listen.worker import topics as tp
    titles = {doc.title: doc.title for _, doc in matched}
    keywords_of = {t: {term for term, source, _, _ in terms if source != "frequency"} for t, terms in terms_of.items()}
    if vec_of is None:
        text = {doc.title: f"{doc.title} {abstract_of(doc)}".lower() for _, doc in matched}
        built_raw = tp.build({t: set(k) for t, k in keywords_of.items()}, llm)
        built = []
        for name, terms in built_raw:
            members = [t for t in text if tp.in_topic(terms, keywords_of.get(t, set()), text[t])]
            built.append((name, terms, members))
    else:
        built = tp.build_papers(vec_of, keywords_of, titles, llm)
    return score_topics(matched, built, topics_gold)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--llm", default="http://127.0.0.1:8080")
    ap.add_argument("--label", required=True)
    ap.add_argument("--keywords", choices=["legacy", "typed"], default="legacy")
    ap.add_argument("--tagger", choices=["off", "on"], default="off")
    ap.add_argument("--qa", choices=["bm25", "hybrid"], default="bm25")
    ap.add_argument("--areas-exemplars", choices=["off", "on"], default="off")
    ap.add_argument("--topics", choices=["keywords", "embeddings"], default="keywords")
    ap.add_argument("--models-dir", type=Path, default=Path("/models"))
    ap.add_argument("--chat-format", choices=["raw", "chatml"], help="prompt format (default: the client's)")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--skip", default="", help="comma list of features to leave out: keywords,areas,ask,topics")
    args = ap.parse_args()
    skip = {s for s in args.skip.split(",") if s}

    llm_kwargs = {"chat_format": args.chat_format} if args.chat_format else {}
    llm = LlmClient(args.llm, **llm_kwargs)
    matched = matched_papers(unique_docs(args.corpus), ev.load_gold(HERE / "extraction_gold.json"))
    print(f"{len(matched)} gold papers in the corpus", flush=True)

    kw: dict = {}
    if args.keywords == "typed":
        kw["typed"] = True
    if args.tagger == "on":
        from listen.worker.tagger import Tagger
        kw["tagger"] = Tagger(args.models_dir / "embed" / "kbir-inspec")
    encoder = None
    if args.qa == "hybrid":
        from listen.worker import embed
        encoder = embed.passage_encoder(args.models_dir)
        if encoder is None:
            raise SystemExit("hybrid retrieval needs the passage model in MODELS_DIR/embed/bge-small")
    needs_vectors = args.areas_exemplars == "on" or args.topics == "embeddings"
    vec_of = gold_vectors(matched, args.models_dir) if needs_vectors else None

    out: dict = {"label": args.label, "config": {
        "keywords": args.keywords, "tagger": args.tagger, "qa": args.qa, "areas_exemplars": args.areas_exemplars,
        "topics": args.topics, "chat_format": args.chat_format or "client default"}}
    terms_of: dict[str, list] = {}
    if "keywords" not in skip:
        out["keywords"], terms_of = run_keywords(matched, llm, kw)
    if "areas" not in skip and (HERE / "areas_gold.json").exists():
        areas_gold = json.loads((HERE / "areas_gold.json").read_text())["papers"]
        out["areas"] = run_areas(matched, areas_gold, llm, terms_of,
                                 vec_of if args.areas_exemplars == "on" else None)
    if "ask" not in skip and (HERE / "ask_gold.json").exists():
        ask_gold = json.loads((HERE / "ask_gold.json").read_text())["papers"]
        out["ask"] = run_ask(matched, ask_gold, llm, {"encoder": encoder} if encoder else {})
    topics_path = HERE / "topics_gold.json"
    if "topics" not in skip:
        if topics_path.exists():
            out["topics"] = run_topics(matched, json.loads(topics_path.read_text()), llm, terms_of,
                                       vec_of if args.topics == "embeddings" else None)
        else:
            out["topics"] = {"summary": None, "note": "no topics_gold.json: label groups of papers to score this"}

    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / f"{args.label}.json"
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps({k: v.get("summary") for k, v in out.items() if isinstance(v, dict) and "summary" in v},
                     ensure_ascii=False, indent=1))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
