"""Score the search-term extraction (task, methods, datasets) against hand labels.

Run from services/backend with the LLM reachable:
    python tests/eval/eval_extraction.py --corpus DIR --llm http://host:8080 --out result.json

DIR holds the normalized structure.json of each paper (DATA_DIR/docs/<id>/structure.json, copied out).
Papers are matched to the gold labels by title. Nothing here reads or writes the library database.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # services/backend

import importlib.util  # noqa: E402

from listen.worker.docmodel import Document  # noqa: E402
from listen.worker.llm import LlmClient  # noqa: E402

FIELDS = {"task": "task", "methods": "method", "datasets": "dataset"}
MATCH_TOKENS = {"of", "and", "the", "a", "an", "in", "on", "for", "to", "with", "from", "by", "at"}
INSTRUCTION_START = {"present", "introduce", "explore", "analyze", "analyse", "identify", "describe", "explain",
                     "discuss", "examine", "investigate", "review", "show", "study", "provide", "summarize",
                     "outline", "focus", "list", "state", "answer", "determine", "evaluate"}


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in MATCH_TOKENS}


def same(pred: str, gold: str) -> bool:
    tp, tg = tokens(pred), tokens(gold)
    if not tp or not tg:
        return False
    inter = len(tp & tg)
    return inter / len(tg) >= 0.6 and inter / len(tp) >= 0.5


def load_gold(path: Path) -> list[dict]:
    return json.loads(path.read_text())["papers"]


def gold_for(title: str, gold: list[dict]) -> dict | None:
    key = re.sub(r"\W+", " ", title.lower()).strip()[:40]
    for g in gold:
        if re.sub(r"\W+", " ", g["title"].lower()).strip()[:40] == key:
            return g
    return None


def score(predictions: dict[str, list[str]], gold: dict) -> dict:
    out = {}
    for field in FIELDS:
        preds = predictions.get(field, [])
        golds = gold[field]
        tp_pred = sum(any(same(p, g) for g in golds) for p in preds)
        tp_gold = sum(any(same(p, g) for p in preds) for g in golds)
        junk = [p for p in preds if p.split()[0] in INSTRUCTION_START]
        out[field] = {"preds": preds, "tp_pred": tp_pred, "n_pred": len(preds), "tp_gold": tp_gold,
                      "n_gold": len(golds), "junk": junk}
    return out


def load_enrich(path: Path | None):
    """The package's enrich module, or another enrich.py file (to score an earlier version)."""
    if path is None:
        from listen.worker import enrich
        return enrich
    spec = importlib.util.spec_from_file_location("enrich_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--llm", default="http://127.0.0.1:8080")
    ap.add_argument("--gold", type=Path, default=HERE / "extraction_gold.json")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-llm", action="store_true", help="text-only fallback, to see the floor")
    ap.add_argument("--module", type=Path, help="an enrich.py file to score instead of the package's")
    args = ap.parse_args()
    enrich = load_enrich(args.module)

    gold = load_gold(args.gold)
    llm = None if args.no_llm else LlmClient(args.llm)
    rows, totals = [], {f: dict(tp_pred=0, n_pred=0, tp_gold=0, n_gold=0, junk=0) for f in FIELDS}
    for path in sorted(args.corpus.glob("*.json")):
        doc = Document.load(path)
        g = gold_for(doc.title, gold)
        if g is None:
            continue
        t0 = time.time()
        terms = enrich.extract(doc, llm)
        preds = {f: [t.term for t in terms if t.kind == k] for f, k in FIELDS.items()}
        sources = {f: sorted({t.source for t in terms if t.kind == k}) for f, k in FIELDS.items()}
        res = score(preds, g)
        rows.append({"title": doc.title[:70], "seconds": round(time.time() - t0), "sources": sources, "fields": res})
        for f in FIELDS:
            for key in ("tp_pred", "n_pred", "tp_gold", "n_gold"):
                totals[f][key] += res[f][key]
            totals[f]["junk"] += len(res[f]["junk"])
        print(f"{doc.title[:60]:60s} {round(time.time() - t0):4d}s", flush=True)

    def ratio(a, b):
        return None if b == 0 else round(a / b, 2)

    summary = {f: {"precision": ratio(t["tp_pred"], t["n_pred"]), "recall": ratio(t["tp_gold"], t["n_gold"]),
                   "predicted": t["n_pred"], "gold": t["n_gold"], "junk": t["junk"]} for f, t in totals.items()}
    print(json.dumps(summary, indent=1))
    args.out.write_text(json.dumps({"summary": summary, "papers": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
