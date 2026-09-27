"""Ambiguous requests never silently become guessed business answers: the system asks (with options), the
answer to the question continues the analysis, and settled choices are remembered. All offline — the planner
raising proves no model was consulted before the meaning was clear."""
import pandas as pd
import pytest

import analyst
from analyst import Conversation, LocalTools
from understanding import understand
from source_loader import column_info
from test_generic import make_source, schemas_of


class NoLLM:
    def __init__(self, *a): pass
    def plan(self, *a, **k): raise AssertionError("planner called before the request was clear")
    def format_result(self, *a, **k): raise AssertionError("wording model called")


PEOPLE = ["Amit", "Bela", "Chand", "Dev", "Esha", "Farah", "Gopi", "Hina", "Ish", "Jay", "Kabir", "Lata"]
SALES = pd.DataFrame({"VOUCHER DATE": ["2026-05-%02d" % (i % 28 + 1) for i in range(60)],
                      "CUSTOMER NAME": [f"Cust{i % 9}" for i in range(60)], "SALES PERSON": [PEOPLE[i % 12] for i in range(60)],
                      "ITEM NAME": [f"Item{i % 7}" for i in range(60)], "QTY": [(i % 5) + 1 for i in range(60)], "AMOUNT": [100.0 * ((i % 11) + 1) for i in range(60)]})
BY_PERSON = SALES.groupby("SALES PERSON")["AMOUNT"].sum()


@pytest.fixture
def conv(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    srv = make_source("sales", sales=SALES)
    return Conversation(schemas=schemas_of(srv)), LocalTools(srv), srv


def ask(c, t, q):
    return analyst.answer(c, q, "k", "m", t)


def test_top_n_without_dimension_asks_then_runs(conv):
    c, t, _ = conv
    r = ask(c, t, "top 5 sales wale")
    assert r.kind == "clarify" and r.df is None and "SALES PERSON wise total" in r.options and "Individual transactions (rows)" in r.options
    r = ask(c, t, "sales person")                                        # the answer to the question, in words
    assert r.kind == "answer" and list(r.df.columns) == ["SALES PERSON", "AMOUNT"] and len(r.df) == 5
    assert list(r.df["AMOUNT"]) == sorted(BY_PERSON, reverse=True)[:5]  # GROUP BY person, SUM, DESC, 5 — not MAX, not rows
    r = ask(c, t, "make it top 10")                                       # only the limit changes
    assert len(r.df) == 10 and list(r.df.columns) == ["SALES PERSON", "AMOUNT"] and list(r.df["AMOUNT"]) == sorted(BY_PERSON, reverse=True)[:10]


def test_lowest_ranking_keeps_direction(conv):
    c, t, _ = conv
    r = ask(c, t, "bhai sabse kam sale wale top 5 name batao")
    assert r.kind == "clarify"
    r = ask(c, t, "1")                                                    # numbered reply (WhatsApp style)
    assert len(r.df) == 5 and list(r.df["AMOUNT"]) == sorted(BY_PERSON)[:5] and c.state["sort"] == "asc"


def test_items_count_asks_and_remembers(conv):
    c, t, srv = conv
    r = ask(c, t, "items count batao")
    assert r.kind == "clarify" and r.options == ["Total quantity — SUM of QTY", "Different item types — distinct ITEM NAME"]
    r = ask(c, t, "different items")
    assert r.kind == "answer" and r.value == 7 and "distinct" in r.text.lower() or "unique" in r.text.lower()
    c2 = Conversation(schemas=schemas_of(srv))                              # new chat, same source: settled, not re-asked
    r = ask(c2, t, "items count batao")
    assert r.kind == "answer" and r.value == 7


def test_total_items_is_clear(conv):
    c, t, _ = conv
    r = ask(c, t, "total items sold in May 2026")
    assert r.kind == "answer" and r.value == SALES["QTY"].sum()


def test_fully_named_dimension_is_clear(conv):
    c, t, _ = conv
    r = ask(c, t, "top 5 customers by total amount")
    assert r.kind == "answer" and list(r.df.columns)[0] == "CUSTOMER NAME" and len(r.df) == 5


def test_superlative_is_max_not_ranking():
    i = understand("highest sale", column_info(SALES))
    assert i["top_n"] is None and i["group_by"] == [] and i["metrics"][0]["aggregation"] == "max"
    i = understand("total sales of each sales person", column_info(SALES))
    assert i["group_by"] == ["SALES PERSON"] and i["top_n"] is None and i["metrics"][0].get("aggregation") in (None, "sum")


def test_multi_table_source_also_gated(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    srv = make_source("book", book={"SALES": SALES, "STOCK": pd.DataFrame({"ITEM NAME": ["Item0"], "STOCK QTY": [5]})})
    r = analyst.answer(Conversation(schemas=schemas_of(srv)), "top 5 sales wale", "k", "m", LocalTools(srv))
    assert r.kind == "clarify" and r.options


def test_unrelated_reply_is_not_a_choice(conv):
    c, t, _ = conv
    ask(c, t, "top 5 sales wale")
    r = ask(c, t, "total items sold")                                     # a different question: pending choice dropped, answered
    assert r.kind == "answer" and r.value == SALES["QTY"].sum()


def test_sheet_ambiguity_asks_instead_of_guessing(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    stock = pd.DataFrame({"ITEM CATEGORY": ["A", "B"], "ITEM NAME": ["Item0", "Item1"], "CLOSING STOCK": [5, 9]})
    srv = make_source("book", book={"INVENTORY": stock, "SALES": SALES})
    c = Conversation(schemas=schemas_of(srv)); t = LocalTools(srv)
    r = analyst.answer(c, "total items sold", "k", "m", t)
    assert r.kind == "clarify" and set(r.options) == {"INVENTORY sheet", "SALES sheet"}          # never CLOSING STOCK as "sold"
    r = analyst.answer(c, "sales", "k", "m", t)
    assert r.kind == "answer" and r.value == SALES["QTY"].sum() and "QTY" in r.text


def test_two_date_columns_asked_once_and_remembered(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    two = SALES.assign(TIMESTAMP=["2026-05-01 10:00:00"] * len(SALES))
    srv = make_source("sales", sales=two); t = LocalTools(srv)
    c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, "2026 month wise total sales amount", "k", "m", t)
    assert r.kind == "clarify" and set(r.options) == {"VOUCHER DATE", "TIMESTAMP"}
    r = analyst.answer(c, "voucher date", "k", "m", t)
    assert r.kind == "answer" and list(r.df.columns) == ["period", "AMOUNT"] and c.state["date_column"] == "VOUCHER DATE" and r.df["AMOUNT"].sum() == SALES["AMOUNT"].sum()
    r = analyst.answer(Conversation(schemas=schemas_of(srv)), "2026 month wise total sales amount", "k", "m", t)
    assert r.kind == "answer" and list(r.df.columns) == ["period", "AMOUNT"]        # remembered for this sheet


def test_loose_number_and_metric_swap_follow_ups(conv):
    c, t, _ = conv
    ask(c, t, "sales person wise total amount")
    r = ask(c, t, "sabse kam wale 3")
    assert len(r.df) == 3 and list(r.df["AMOUNT"]) == sorted(BY_PERSON)[:3]
    r = ask(c, t, "total quantity")                                                    # metric swap on the same grouping/limit
    assert list(r.df.columns) == ["SALES PERSON", "QTY"] and len(r.df) == 3


def test_clarification_chain_keeps_the_chosen_sheet(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    stock = pd.DataFrame({"ITEM CATEGORY": ["A", "B"], "ITEM NAME": ["Item0", "Item1"], "CLOSING STOCK": [5, 9]})
    srv = make_source("book", book={"INVENTORY": stock, "SALES": SALES}); t = LocalTools(srv)
    c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, "items count batao", "k", "m", t)
    assert r.kind == "clarify" and "SALES sheet" in r.options
    r = analyst.answer(c, "sales", "k", "m", t)                                        # sheet settled → the meaning question
    assert r.kind == "clarify" and r.options[0].endswith("SUM of QTY")
    r = analyst.answer(c, "total quantity", "k", "m", t)                               # meaning settled → runs on SALES, not asked again
    assert r.kind == "answer" and r.value == SALES["QTY"].sum() and c.state["sheet_name"] == "SALES"


def test_screenshot_conversation_lowest_ranking_flow(conv):
    """The conversation from the user's screenshot: 'kam' anywhere means ascending, a bare superlative with a named
    dimension is that one row, 'top 5 only' keeps the direction, 'zyada' flips it."""
    c, t, _ = conv
    r = ask(c, t, "sabse bekar current ken hai jisne abhi tak kab se kam sale kiya hai top 5 name")
    assert r.kind == "clarify"
    r = ask(c, t, "sales person wise total")
    assert list(r.df["AMOUNT"]) == sorted(BY_PERSON)[:5]                                   # ascending, 5
    r = ask(c, t, "bhai sabh se kam sale wala customer ka name do")
    by_cust = SALES.groupby("CUSTOMER NAME")["AMOUNT"].sum()
    assert len(r.df) == 1 and r.df.iloc[0]["AMOUNT"] == by_cust.min() and list(r.df.columns) == ["CUSTOMER NAME", "AMOUNT"]
    r = ask(c, t, "top 5 only")
    assert list(r.df["AMOUNT"]) == sorted(by_cust)[:5]
    r = ask(c, t, "top 5 jisne kam sale kiya hai")
    assert list(r.df["AMOUNT"]) == sorted(by_cust)[:5] and list(r.df.columns) == ["CUSTOMER NAME", "AMOUNT"]
    r = ask(c, t, "top 5 zyada wale")
    assert list(r.df["AMOUNT"]) == sorted(by_cust, reverse=True)[:5]


def test_date_answer_is_not_a_dimension(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    two = SALES.assign(TIMESTAMP=["2026-05-01 10:00:00"] * len(SALES), **{"VOUCHER NAME": ["Sales"] * len(SALES)})
    srv = make_source("sales", sales=two); t = LocalTools(srv); c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, "sabh se kam sale wala customer ka name and total amount this year", "k", "m", t)
    assert r.kind == "clarify" and set(r.options) == {"TIMESTAMP", "VOUCHER DATE"}
    r = analyst.answer(c, "VOUCHER DATE se", "k", "m", t)                        # names the date column, not VOUCHER NAME
    assert r.kind == "answer" and list(r.df.columns) == ["CUSTOMER NAME", "AMOUNT"] and len(r.df) == 1 and c.state["date_column"] == "VOUCHER DATE"


def test_screenshot_two_customer_name_lowest_and_sheet_switch(monkeypatch, tmp_path):
    """Second screenshot: 'customer name bata' lists (no Customer Count column), 'bahut kam' = lowest one, a column that
    lives in another sheet switches the analysis there, and a filter-looking message goes to the planner instead of the
    state executor."""
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    stock = pd.DataFrame({"ITEM CATEGORY": ["A", "B", "C"], "ITEM NAME": ["Item0", "Item1", "Item2"], "CLOSING STOCK": [5, 9, 2]})
    srv = make_source("book", book={"SALES": SALES, "INVENTORY": stock}); t = LocalTools(srv); c = Conversation(schemas=schemas_of(srv))
    by_cust = SALES.groupby("CUSTOMER NAME")["AMOUNT"].sum()
    r = analyst.answer(c, "bhai mujhe wo customer name bata jiske sale this sale bahut kam hua hai", "k", "m", t)
    assert list(r.df.columns) == ["CUSTOMER NAME", "AMOUNT"] and len(r.df) == 1 and r.df.iloc[0]["AMOUNT"] == by_cust.min()
    r = analyst.answer(c, "top 5 only", "k", "m", t)
    assert list(r.df["AMOUNT"]) == sorted(by_cust)[:5] and r.chart["y"] == "AMOUNT"
    r = analyst.answer(c, "ITEM CATEGORY ka sabh se jada hai closing stock hai?", "k", "m", t)
    assert list(r.df.columns) == ["ITEM CATEGORY", "CLOSING STOCK"] and r.df.iloc[0].tolist() == ["B", 9] and c.state["sheet_name"] == "INVENTORY"
    r = analyst.answer(c, "top 5", "k", "m", t)
    assert list(r.df["CLOSING STOCK"]) == [9, 5, 2]
    with pytest.raises(AssertionError, match="planner"):                       # a value filter is not the state executor's job
        analyst.answer(c, 'Code-1032, TELESCOPIC SLIDE 45kg ka item kon kon sale karta hai', "k", "m", t)

