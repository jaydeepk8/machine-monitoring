# the AI layer without the AI: a scripted stand-in plays the model, so the loop,
# the lookups, the number check and the grading are all tested for real

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assistant"))

import ask as ask_module  # noqa: E402
from ask import ANSWER_FORMAT, FALLBACK_BETA, MODEL, Answer, AskError, ask, numbers_in, unverified  # noqa: E402
from evaluate import evaluate, grade, load, summary  # noqa: E402
from fixture import build  # noqa: E402
from tools import TOOLS, Shop, run_tool  # noqa: E402


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    return str(build(tmp_path_factory.mktemp("shop") / "fixture.db"))


# ---- the stand-in model

def tool_call(name, args, call_id="call_1"):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def reply(answer, facts=(), could_not_answer=False, missing=""):
    body = {"answer": answer, "facts": [{"label": l, "value": v} for l, v in facts],
            "could_not_answer": could_not_answer, "missing": missing}
    return SimpleNamespace(type="text", text=json.dumps(body, ensure_ascii=False))


def turn(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), model=MODEL,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


class FakeClient:
    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request):
        # a copy, so later appends do not change what was sent
        self.requests.append(copy.deepcopy(request))
        return self.turns.pop(0)


def one_lookup_then(answer_block, name="get_downtime", args=None):
    return FakeClient(
        turn("tool_use", tool_call(name, args if args is not None else {"machine": "V60"})),
        turn("end_turn", answer_block),
    )


# ---- the loop

def test_a_lookup_then_an_answer(db):
    client = one_lookup_then(reply("V60 lost 6.2 minutes to NO_OPERATOR.", [("NO_OPERATOR minutes", "6.2")]))
    a = ask("How much time did V60 lose to NO_OPERATOR?", db=db, client=client)

    assert a.text == "V60 lost 6.2 minutes to NO_OPERATOR."
    assert a.verified
    assert a.lookups == [{"tool": "get_downtime", "input": {"machine": "V60"}, "error": False}]
    assert a.input_tokens == 2000 and a.output_tokens == 200

    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "call_1"
    assert result["is_error"] is False
    assert '"minutes": 6.2' in result["content"]


def test_the_request_uses_the_agreed_settings(db):
    client = one_lookup_then(reply("ok"))
    ask("How is the shop doing?", db=db, client=client)
    r = client.requests[0]

    assert r["model"] == "claude-opus-5-5"
    assert r["fallbacks"] == "default"
    assert FALLBACK_BETA in r["betas"]
    assert r["output_config"]["effort"] == "medium"
    assert r["output_config"]["format"] == ANSWER_FORMAT
    assert r["cache_control"] == {"type": "ephemeral"}
    assert "forced" not in json.dumps(r.get("tool_choice", {}))


def test_every_lookup_is_strict_and_complete():
    for t in TOOLS:
        schema = t["input_schema"]
        assert t["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_the_conversation_is_only_ever_added_to(db):
    client = FakeClient(
        turn("tool_use", tool_call("get_overview", {})),
        turn("tool_use", tool_call("get_downtime", {"machine": "V61"}, "call_2")),
        turn("end_turn", reply("V61 lost 198.9 minutes.", [("downtime", "198.9")])),
    )
    ask("How did V61 do?", db=db, client=client)
    sent = [r["messages"] for r in client.requests]
    for earlier, later in zip(sent, sent[1:]):
        assert later[: len(earlier)] == earlier


def test_several_lookups_in_one_turn_go_back_together(db):
    client = FakeClient(
        turn("tool_use", tool_call("get_overview", {}, "a"), tool_call("get_drift", {"machine": None}, "b")),
        turn("end_turn", reply("ok")),
    )
    ask("Anything wrong?", db=db, client=client)
    last = client.requests[1]["messages"][-1]
    assert last["role"] == "user"
    assert [r["tool_use_id"] for r in last["content"]] == ["a", "b"]


def test_a_wrong_machine_goes_back_as_an_error_and_the_model_carries_on(db):
    client = one_lookup_then(
        reply("There is no machine V99.", could_not_answer=True, missing="machine V99 does not exist"),
        args={"machine": "V99"},
    )
    a = ask("What was the OEE of V99?", db=db, client=client)
    result = client.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "V60, V61, V62" in result["content"]
    assert a.could_not_answer
    # the 99 came from the question, so it is not an invented number
    assert a.verified


def test_an_unknown_lookup_name_is_an_error(db):
    client = one_lookup_then(reply("ok"), name="drop_table", args={})
    a = ask("Delete everything", db=db, client=client)
    assert a.lookups[0]["error"] is True


# ---- the number check

def test_an_invented_number_is_flagged(db):
    client = one_lookup_then(reply("V60 lost 7.5 minutes to NO_OPERATOR.", [("minutes", "7.5")]))
    a = ask("How much time did V60 lose to NO_OPERATOR?", db=db, client=client)
    assert a.unverified_numbers == [7.5]
    assert not a.verified


def test_marathi_digits_are_checked_too(db):
    client = one_lookup_then(reply("V60 चे ६.२ मिनिटे वाया गेले.", [("मिनिटे", "6.2")]))
    a = ask("V60 ला किती वेळ वाया गेला?", lang="mr", db=db, client=client)
    assert a.verified

    client = one_lookup_then(reply("V60 चे ७.५ मिनिटे वाया गेले.", [("मिनिटे", "7.5")]))
    assert ask("V60 ला किती वेळ वाया गेला?", lang="mr", db=db, client=client).unverified_numbers == [7.5]


def test_numbers_are_read_as_values():
    assert numbers_in("OEE 73.0% at 09:25, V60") == {73.0, 9.0, 25.0, 60.0}
    assert numbers_in("६.२") == {6.2}
    assert unverified("lost 6.2 min", [], ['{"minutes": 6.2}']) == []
    assert unverified("lost 6.20 min", [], ['{"minutes": 6.2}']) == []


# ---- what can go wrong

def test_a_declined_request_gives_a_plain_answer(db):
    client = FakeClient(turn("refusal"))
    a = ask("Anything", db=db, client=client)
    assert a.could_not_answer
    assert a.stop_reason == "refusal"


def test_a_cut_off_answer_is_an_error(db):
    with pytest.raises(AskError, match="cut off"):
        ask("Anything", db=db, client=FakeClient(turn("max_tokens")))


def test_endless_lookups_stop(db):
    client = FakeClient(*[turn("tool_use", tool_call("get_overview", {}, f"c{i}")) for i in range(3)])
    with pytest.raises(AskError, match="rounds"):
        ask("Anything", db=db, client=client, max_rounds=3)


def test_an_answer_in_the_wrong_form_is_an_error(db):
    bad = SimpleNamespace(type="text", text="not json")
    with pytest.raises(AskError, match="expected form"):
        ask("Anything", db=db, client=FakeClient(turn("end_turn", bad)))


def test_no_api_key_gives_a_clear_message(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(AskError, match="API key"):
        ask("Anything", db=db)


def test_only_known_languages(db):
    with pytest.raises(AskError, match="language"):
        ask("Anything", lang="fr", db=db, client=FakeClient())


def test_no_results_database_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="run_all"):
        ask("Anything", db=str(tmp_path / "none.db"), client=FakeClient())


# ---- grading

def answer(text, facts=(), could_not_answer=False, unverified_numbers=()):
    return Answer(text=text, facts=[{"label": "x", "value": v} for v in facts],
                  could_not_answer=could_not_answer, unverified_numbers=list(unverified_numbers))


ITEM = {"id": "x", "answerable": True, "expect": ["V62", "73.0"]}


def test_a_right_answer_passes():
    assert grade(ITEM, answer("V62 had the highest OEE, 73%."))["passed"]


def test_a_value_only_in_the_facts_still_counts():
    assert grade(ITEM, answer("V62 was best.", facts=["73.0"]))["passed"]


def test_a_wrong_number_fails():
    g = grade(ITEM, answer("V62 had 72.9%."))
    assert not g["passed"] and "missing 73.0" in g["reasons"][0]


def test_giving_up_on_an_answerable_question_fails():
    assert not grade(ITEM, answer("V62 73.0", could_not_answer=True))["passed"]


def test_answering_what_the_data_cannot_fails():
    item = {"id": "y", "answerable": False, "expect": []}
    assert not grade(item, answer("About 400 kWh."))["passed"]
    assert grade(item, answer("Energy is not tracked.", could_not_answer=True))["passed"]


def test_an_invented_number_fails_even_with_the_right_ones():
    assert not grade(ITEM, answer("V62 73.0, up 5%", unverified_numbers=[5.0]))["passed"]


def test_the_runner_scores_and_costs_a_run(db):
    items = [i for i in load() if i["id"] in ("downtime-v60-no-operator", "no-energy")]
    client = FakeClient(
        turn("tool_use", tool_call("get_downtime", {"machine": "V60"})),
        turn("end_turn", reply("V60 lost 6.2 minutes.", [("minutes", "6.2")])),
        turn("end_turn", reply("Energy use is not tracked.", could_not_answer=True, missing="energy")),
    )
    results = evaluate(items, "en", client=client, db=db)
    s = summary(results)
    assert [r["passed"] for r in results] == [True, True]
    assert s["passed"] == 2 and s["total"] == 2
    assert s["approx_cost_usd"] > 0


def test_the_runner_skips_questions_without_that_language(db):
    items = [i for i in load() if i["id"] == "oee-lowest"]
    assert evaluate(items, "mr", client=FakeClient(), db=db) == []
