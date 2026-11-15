# runs the test set through the AI and scores every answer
#   python assistant/evaluate.py                 English, all questions
#   python assistant/evaluate.py --lang mr       only the questions written in Marathi
#   python assistant/evaluate.py --only oee-shop --only parts-v61
#
# Every run calls the Claude API and costs money: about 2 to 4 rounds per question.
# Results are saved in output/evals/ so runs can be compared.

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ask import AskError, ask, make_client, numbers_in  # noqa: E402
from fixture import build  # noqa: E402

HERE = Path(__file__).resolve().parent
TESTSET = HERE / "testset.json"
OUT = HERE.parent / "output" / "evals"

# rough price of the default model, US dollars per million tokens
PRICE_IN, PRICE_OUT = 4.00, 20.00


def load(path=TESTSET):
    return json.loads(Path(path).read_text(encoding="utf-8"))["questions"]


def contains(answer, expected):
    # numbers are compared as values, so "73" matches "73.0" and Marathi digits count.
    # Words and codes must appear as written.
    pool = answer.text + " " + " ".join(f["value"] for f in answer.facts)
    if numbers_in(expected) and not any(c.isalpha() for c in expected):
        return numbers_in(expected) <= numbers_in(pool)
    return expected.lower() in pool.lower()


def grade(item, answer):
    # a pass needs every check to hold. The reasons say what failed.
    reasons = []
    if item["answerable"]:
        if answer.could_not_answer:
            reasons.append("said it could not answer")
        missing = [e for e in item["expect"] if not contains(answer, e)]
        if missing:
            reasons.append("missing " + ", ".join(missing))
    elif not answer.could_not_answer:
        reasons.append("answered a question the data cannot answer")
    if answer.unverified_numbers:
        reasons.append("numbers not in the data: " + ", ".join(f"{n:g}" for n in answer.unverified_numbers))
    return {"passed": not reasons, "reasons": reasons}


def evaluate(items, lang="en", client=None, db=None):
    if db is None:
        db = build(Path(tempfile.mkdtemp()) / "fixture.db")
    client = client or make_client()

    results = []
    for item in items:
        question = item["question"].get(lang)
        if question is None:
            continue
        began = time.time()
        try:
            answer = ask(question, lang, db, client=client)
            graded = grade(item, answer)
            row = {
                "id": item["id"], "lang": lang, "question": question, **graded,
                "answer": answer.text, "facts": answer.facts, "could_not_answer": answer.could_not_answer,
                "unverified_numbers": answer.unverified_numbers, "lookups": answer.lookups,
                "model": answer.model, "input_tokens": answer.input_tokens, "output_tokens": answer.output_tokens,
            }
        except AskError as e:
            row = {"id": item["id"], "lang": lang, "question": question, "passed": False,
                   "reasons": [f"error: {e}"], "input_tokens": 0, "output_tokens": 0}
        row["seconds"] = round(time.time() - began, 1)
        results.append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'}  {item['id']:<28} {'; '.join(row['reasons'])}", flush=True)
    return results


def summary(results):
    passed = sum(r["passed"] for r in results)
    tokens_in = sum(r["input_tokens"] for r in results)
    tokens_out = sum(r["output_tokens"] for r in results)
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    return {"passed": passed, "total": len(results), "tokens_in": tokens_in, "tokens_out": tokens_out,
            "approx_cost_usd": round(cost, 4)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--lang", choices=["en", "mr", "hi"], default="en")
    p.add_argument("--only", action="append", default=[], help="question id, can be given more than once")
    a = p.parse_args()

    items = load()
    if a.only:
        items = [i for i in items if i["id"] in a.only]

    try:
        results = evaluate(items, a.lang)
    except AskError as e:
        raise SystemExit(f"cannot run the test set: {e}")

    s = summary(results)
    print()
    print(f"{s['passed']} of {s['total']} passed. Tokens in {s['tokens_in']}, out {s['tokens_out']}, "
          f"about ${s['approx_cost_usd']}")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.lang}.json"
    path.write_text(json.dumps({"summary": s, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved:", path)
