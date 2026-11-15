# the test set must stay true: every expected answer has to be in the data,
# where the question says it is. This runs without the model.

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assistant"))

from ask import numbers_in  # noqa: E402
from evaluate import load  # noqa: E402
from fixture import build  # noqa: E402
from tools import TOOLS, Shop, run_tool  # noqa: E402

ITEMS = load()
ANSWERABLE = [i for i in ITEMS if i["answerable"]]
NAMES = {t["name"] for t in TOOLS}


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    return Shop(str(build(tmp_path_factory.mktemp("shop") / "fixture.db")))


def test_the_set_is_big_enough_and_mixed():
    assert len(ITEMS) >= 30
    assert sum(not i["answerable"] for i in ITEMS) >= 5
    assert sum("mr" in i["question"] and "hi" in i["question"] for i in ITEMS) >= 8


def test_every_question_is_well_formed():
    ids = [i["id"] for i in ITEMS]
    assert len(ids) == len(set(ids))
    for i in ITEMS:
        assert i["question"]["en"].strip()
        if i["answerable"]:
            assert i["expect"], i["id"]
            assert i["check"]["tool"] in NAMES, i["id"]
        else:
            assert i["expect"] == [], i["id"]


@pytest.mark.parametrize("item", ANSWERABLE, ids=[i["id"] for i in ANSWERABLE])
def test_the_expected_answer_is_in_the_data(shop, item):
    text, is_error = run_tool(shop, item["check"]["tool"], item["check"]["args"])
    assert not is_error, text
    for expected in item["expect"]:
        if numbers_in(expected) and not any(c.isalpha() for c in expected):
            assert numbers_in(expected) <= numbers_in(text), f"{expected} not in {item['check']}"
        else:
            assert expected in text, f"{expected} not in {item['check']}"


def test_a_machine_that_does_not_exist_cannot_be_looked_up(shop):
    text, is_error = run_tool(shop, "get_overview", {})
    assert "V99" not in text
    assert run_tool(shop, "get_downtime", {"machine": "V99"})[1] is True


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value)) if value else set()
    return set()


def test_the_data_has_no_rejects_energy_operators_or_orders(shop):
    # the questions in the "cannot answer" group must really be unanswerable
    fields, texts = set(), []
    for t in TOOLS:
        args = {"status": "all"} if t["name"] == "get_alerts" else {k: None for k in t["input_schema"]["properties"]}
        text, _ = run_tool(shop, t["name"], args)
        fields |= keys(json.loads(text))
        texts.append(text)
    for word in ("reject", "kwh", "energy", "operator", "order", "delivery"):
        assert not [f for f in fields if word in f.lower()], word
    assert "quality not measured" in " ".join(texts)


def test_machine_names_are_matched_whatever_the_case(shop):
    text, is_error = run_tool(shop, "get_downtime", {"machine": "v61"})
    assert not is_error
    assert json.loads(text)["machine"] == "V61"
