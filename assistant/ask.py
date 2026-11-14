# ask the shop a question in your own words
#   python assistant/ask.py "why was V61 slow today?" --lang mr
#
# Claude only looks things up with the fixed lookups in tools.py and writes the answer.
# Every number in the answer is then checked against what the lookups returned.

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tools import RESULTS_DB, TOOLS, Shop, run_tool  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
MAX_ROUNDS = 8

# a declined request is retried on the model Anthropic recommends, inside the same call
FALLBACK_BETA = "server-side-fallback-2026-07-01"

LANGUAGES = {"en": "English", "mr": "Marathi", "hi": "Hindi"}

SYSTEM = """You answer questions from the owner and staff of a CNC and VMC machine shop in Pune, India, about how their machines did.

You can only know what the lookup tools return. Use them for every fact. If the tools do not have the data, say so plainly and set could_not_answer to true. Never estimate, guess or use general knowledge about other shops. Things the system does not track include energy use, operators' names, customer orders, delivery dates and costs.

Numbers:
- Copy numbers exactly as the tools give them. Do not round them again or work out new ones. If the question needs a number the tools do not give, say it is not available.
- Write digits as 0-9 in every language.
- Give units: minutes, hours, %, parts.

Codes in the data:
- Machine codes (V60) and reason codes (NO_MATERIAL) stay as they are. You may explain a code in the answer's language.
- UNKNOWN means the operator did not tap a reason. Do not guess one.
- quality_pct is 100 when no rejects were entered. That is not real quality data, so say quality was not measured.

Style: the answer is read on a phone, often on WhatsApp. Lead with the direct answer. Use short sentences and plain words. Use at most 6 lines. Do not use markdown, tables or headings.

Fill the reply fields:
- answer: the reply, in the language you are asked for.
- facts: every number, machine, program, time or reason code the answer relies on, with value copied exactly from the tools.
- could_not_answer: true if the data cannot answer the question, fully or in part.
- missing: what data is missing, in English. Empty if nothing is missing."""

ANSWER_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
            "could_not_answer": {"type": "boolean"},
            "missing": {"type": "string"},
        },
        "required": ["answer", "facts", "could_not_answer", "missing"],
        "additionalProperties": False,
    },
}


class AskError(Exception):
    pass


@dataclass
class Answer:
    text: str
    facts: list = field(default_factory=list)
    could_not_answer: bool = False
    missing: str = ""
    unverified_numbers: list = field(default_factory=list)
    lookups: list = field(default_factory=list)
    stop_reason: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def verified(self):
        return not self.unverified_numbers


# ---- checking numbers

DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text):
    # every number in a text, as a value, whatever digits it is written in
    return {float(n) for n in NUMBER.findall(str(text).translate(DEVANAGARI))}


def unverified(answer_text, facts, sources):
    # numbers the answer uses that no lookup and not the question gave
    known = set()
    for s in sources:
        known |= numbers_in(s)
    used = numbers_in(answer_text)
    for f in facts:
        used |= numbers_in(f.get("value", ""))
    return sorted(n for n in used if n not in known)


# ---- the conversation

def make_client():
    try:
        import anthropic
    except ImportError:
        raise AskError("the anthropic package is not installed. Run: pip install -r requirements.txt")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise AskError("no Anthropic API key. Set ANTHROPIC_API_KEY first.")
    return anthropic.Anthropic()


def blocks_of(response, kind):
    return [b for b in response.content if getattr(b, "type", None) == kind]


def final_reply(response):
    text = "".join(b.text for b in blocks_of(response, "text"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise AskError(f"the answer was not in the expected form: {text[:200]}")


def ask(question, lang="en", db=RESULTS_DB, client=None, model=MODEL, max_rounds=MAX_ROUNDS):

    if lang not in LANGUAGES:
        raise AskError(f"language must be one of: {', '.join(LANGUAGES)}")

    shop = Shop(db)
    client = client or make_client()

    messages = [{
        "role": "user",
        "content": f"Answer in {LANGUAGES[lang]}.\n\nQuestion: {question}",
    }]
    sources = [question]
    lookups = []
    totals = {"in": 0, "out": 0}

    for _ in range(max_rounds):
        response = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT, "format": ANSWER_FORMAT},
            # the system prompt and tools never change, so they are cached between rounds
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            totals["in"] += getattr(usage, "input_tokens", 0) or 0
            totals["out"] += getattr(usage, "output_tokens", 0) or 0

        def finish(**kwargs):
            return Answer(lookups=lookups, stop_reason=response.stop_reason,
                          model=getattr(response, "model", model),
                          input_tokens=totals["in"], output_tokens=totals["out"], **kwargs)

        if response.stop_reason == "refusal":
            # every model in the fallback chain declined
            return finish(text="Sorry, this question could not be answered.", could_not_answer=True,
                          missing="the request was declined")

        if response.stop_reason == "max_tokens":
            raise AskError("the answer was cut off before it finished")

        # the whole reply goes back unchanged, so the conversation stays append-only
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            continue

        calls = blocks_of(response, "tool_use")
        if response.stop_reason == "tool_use" and calls:
            results = []
            for call in calls:
                text, is_error = run_tool(shop, call.name, call.input)
                lookups.append({"tool": call.name, "input": call.input, "error": is_error})
                sources.append(text)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": is_error})
            # all results of one turn go back together
            messages.append({"role": "user", "content": results})
            continue

        reply = final_reply(response)
        return finish(
            text=reply["answer"],
            facts=reply["facts"],
            could_not_answer=reply["could_not_answer"],
            missing=reply["missing"],
            unverified_numbers=unverified(reply["answer"], reply["facts"], sources),
        )

    raise AskError(f"no answer after {max_rounds} rounds of lookups")


def show(a):
    print(a.text)
    if a.unverified_numbers:
        print()
        print("WARNING: these numbers were not found in the shop data, check them:",
              ", ".join(f"{n:g}" for n in a.unverified_numbers))
    if a.could_not_answer and a.missing:
        print(f"(missing: {a.missing})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--lang", choices=list(LANGUAGES), default="en")
    p.add_argument("--db", default=RESULTS_DB)
    p.add_argument("--details", action="store_true", help="also print the lookups and token use")
    a = p.parse_args()

    try:
        answer = ask(a.question, a.lang, a.db)
    except (AskError, FileNotFoundError) as e:
        raise SystemExit(f"cannot answer: {e}")

    show(answer)
    if a.details:
        print()
        print("lookups:", json.dumps(answer.lookups, ensure_ascii=False))
        print(f"model {answer.model}, tokens in {answer.input_tokens}, out {answer.output_tokens}")
