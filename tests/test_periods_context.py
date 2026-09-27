"""Canonical date resolution + conversational analytical state, all offline (no LLM).

Covers the deployed-app bugs: "last month" = the previous calendar month (never month-to-date), "last 2 months" means one
thing on every path (typed, voice, planner, agent), compound period requests answer every part, "total kar ke batao" /
"only total" collapse the previous series into one scalar (no repeated table/chart), month-wise values reconcile with
the total, simple questions get simple answers, and money looks the same in cards and tables."""
from datetime import date as _date

import pandas as pd
import pytest

import analyst
from analyst import Conversation, LocalTools, _fmt, sanitize_plan
from periods import enforce_period, resolve_date_expression
from response import display_table, finalize
from understanding import understand
from test_generic import make_source, schemas_of

TODAY = _date(2026, 9, 27)


class date(_date):
    @classmethod
    def today(cls):
        return TODAY


# July has rows on purpose: a rolling "today - 60 days" window (29 Jul → 27 Sep) would include some of them.
ORDERS = pd.DataFrame({
    "Bill No": range(1, 9), "Party Name": ["Acme", "Zed", "Acme", "Bolt", "Zed", "Acme", "Nova", "Bolt"],
    "Bill Date": ["2026-07-30", "2026-07-31", "2026-08-03", "2026-08-15", "2026-08-28", "2026-09-05", "2026-09-20", "2026-09-26"],
    "Qty": [3, 2, 5, 1, 4, 2, 1, 1], "Net Amount": [1200.0, 800.0, 1500.0, 300.0, 950.0, 700.0, 400.0, 250.0]})
BY_MONTH = ORDERS.assign(p=ORDERS["Bill Date"].str[:7]).groupby("p")["Net Amount"].sum()
AUG, SEP, JUL = float(BY_MONTH["2026-08"]), float(BY_MONTH["2026-09"]), float(BY_MONTH["2026-07"])
ROLLING_60 = float(ORDERS[ORDERS["Bill Date"] >= "2026-07-29"]["Net Amount"].sum())
assert ROLLING_60 != AUG + SEP


class NoLLM:
    def __init__(self, *a): pass
    def plan(self, *a, **k): raise AssertionError("planner must not be called")
    def format_result(self, *a, **k): raise AssertionError("wording model must not be called")


@pytest.fixture
def conv(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    srv = make_source("orders", orders=ORDERS)
    return Conversation(schemas=schemas_of(srv)), LocalTools(srv)


def ask(c, t, q):
    r = analyst.answer(c, q, "k", "m", t)
    return r, finalize(r, q)


# ---- TEST 1: "last month" is the previous calendar month, never month-to-date
@pytest.mark.parametrize("q", ["last month ki total sale?", "pichle mahine ki sale kitni hui", "previous month's sales", "last month ka total", "Last moment ki total Sel kitni hai"])
def test_last_month_is_previous_calendar_month(q):
    p = resolve_date_expression(q, TODAY)
    assert (p["from"], p["to"], p["period_type"]) == ("2026-08-01", "2026-08-31", "previous_month")
    assert resolve_date_expression("this month", TODAY)["from"] == "2026-09-01" and resolve_date_expression("this month", TODAY)["to"] == "2026-09-27"


def test_last_month_conversation(conv):
    c, t = conv
    r, fr = ask(c, t, "last month ki total sale kitni hui?")
    assert fr.shape == "scalar" and fr.value == AUG and fr.table is None and fr.chart is None
    assert c.state["period"]["from"] == "2026-08-01" and c.state["period"]["to"] == "2026-08-31"
    assert "01-08-2026 → 31-08-2026" in r.text


# ---- TEST 2 + 7: "last 2 months" = the 2 calendar months ending today, identical for every phrasing (typed or voice)
@pytest.mark.parametrize("q", ["last 2 months", "last two months ki sales", "Last 2 month ki Sel Kitni Hui Hai", "pichle do mahine ki sale",
                               "do mahine ka total", "previous 2 months", "past two months sales", "पिछले दो महीने की सेल"])
def test_last_two_months_one_canonical_reading(q):
    p = resolve_date_expression(q, TODAY)
    assert (p["from"], p["to"], p["period_type"], p["n"]) == ("2026-08-01", "2026-09-27", "last_n_calendar_months", 2)
    assert [x["label"] for x in p["periods"]] == ["August 2026", "September 2026"]
    assert p["periods"][1]["end"] == "2026-09-27" and p["periods"][0]["end"] == "2026-08-31"


def test_typed_and_voice_forms_give_the_same_plan():
    cols = [{"name": "Bill Date", "role": "date"}, {"name": "Net Amount", "role": "metric"}, {"name": "Party Name", "role": "dimension"}]
    a, b, h = (understand(q, cols, today=TODAY) for q in ("last 2 months ki sales kitni hai", "last two months ki sales kitni hai", "pichle do mahine ki sales kitni hai"))
    key = lambda i: (i["period"]["from"], i["period"]["to"], i["period"]["grain"], i["grain"], i["group_by"], [m["kind"] for m in i["metrics"]])
    assert key(a) == key(b) == key(h) == ("2026-08-01", "2026-09-27", None, None, [], ["monetary"])


def test_planner_dates_never_bypass_the_resolver(monkeypatch):
    monkeypatch.setattr("periods.date", date)
    rolling = {"status": "execute", "mode": "data", "source_id": "orders", "operation": "aggregate", "metric": "Net Amount", "aggregation": "sum",
               "date_column": "Bill Date", "date_from": "2026-07-29", "date_to": "2026-09-27"}
    fixed = sanitize_plan(dict(rolling), "last two months ki sale kitni hui", None)
    assert (fixed["date_from"], fixed["date_to"]) == ("2026-08-01", "2026-09-27") and fixed["period"]["period_type"] == "last_n_calendar_months"
    fixed = sanitize_plan(dict(rolling, date_from="2026-09-01", date_to="2026-09-27"), "last month ki sale", None)
    assert (fixed["date_from"], fixed["date_to"]) == ("2026-08-01", "2026-08-31")
    # agent steps may query a sub-period, but never outside the asked one
    step = enforce_period({"date_column": "Bill Date", "date_from": "2026-06-01", "date_to": "2026-08-31"}, "last 2 months", TODAY, clamp_only=True)
    assert (step["date_from"], step["date_to"]) == ("2026-08-01", "2026-08-31")
    assert "date_from" not in enforce_period({"metric": "x"}, "last 2 months", TODAY, clamp_only=True)   # no date filter on the step: untouched
    assert sanitize_plan(dict(rolling), "Acme ka total", None)["date_from"] == "2026-07-29"           # no period in the words: LLM dates kept


def test_llm_answer_seeds_context_and_total_follow_up_reuses_filters(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    plan = {"status": "execute", "mode": "data", "source_id": "orders", "operation": "aggregate", "metric": "Net Amount", "aggregation": "sum",
            "date_column": "Bill Date", "date_from": "2026-07-29", "date_to": "2026-09-27", "filters": [{"column": "Party Name", "op": "eq", "value": "Acme"}]}

    class Planner:
        def __init__(self, *a): pass
        def plan(self, q, ctx): return dict(plan)
        def format_result(self, *a, **k): return {"answer": ""}
    monkeypatch.setattr(analyst, "OpenRouterAI", Planner)
    srv = make_source("orders", orders=ORDERS); c = Conversation(schemas=schemas_of(srv)); t = LocalTools(srv)
    acme = float(ORDERS[(ORDERS["Party Name"] == "Acme") & (ORDERS["Bill Date"] >= "2026-08-01")]["Net Amount"].sum())
    r, fr = ask(c, t, "Acme ka last two months ka total sale kitna hai")       # unknown word "Acme" → planner path
    assert fr.shape == "scalar" and fr.value == acme and c.last_plan["date_from"] == "2026-08-01"
    assert c.state and c.state["filters"] == plan["filters"] and c.state["period"]["from"] == "2026-08-01"
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    r, fr = ask(c, t, "total kar ke batao")                                    # context reused: same filter, same period, no LLM
    assert fr.shape == "scalar" and fr.value == acme and fr.table is None


# ---- TEST 3 + 4 + 8: series → total → only total
def test_series_then_total_then_only_total(conv):
    c, t = conv
    r, fr = ask(c, t, "last 2 months sales dikhao")
    assert fr.shape == "series" and list(fr.table["period"]) == ["2026-08", "2026-09"] and list(fr.table["Net Amount"]) == [AUG, SEP] and fr.chart
    r, fr = ask(c, t, "total kar ke batao")
    assert fr.shape == "scalar" and fr.value == AUG + SEP and fr.table is None and fr.chart is None and len(fr.metrics) == 1
    assert c.state["grain"] is None and c.state["period"]["from"] == "2026-08-01" and c.state["period"]["to"] == "2026-09-27"
    r2, fr2 = ask(c, t, "mujhe only total bata do bhai")
    assert fr2.shape == "scalar" and fr2.value == AUG + SEP and fr2.table is None and fr2.chart is None and len(fr2.metrics) == 1
    assert "Calculation" not in r2.text and "Calculation" in r.text and _fmt(AUG + SEP, "Net Amount") in r2.text
    r3, fr3 = ask(c, t, "bas total bata")
    assert fr3.value == AUG + SEP and fr3.table is None


@pytest.mark.parametrize("q", ["total karke bata", "sab mila ke kitna hua", "grand total?", "sum kar do", "kul kitna", "sirf number batao total"])
def test_collapse_phrasings(conv, q):
    c, t = conv
    ask(c, t, "last 2 months sales month wise")
    if not resolve_date_expression(q, TODAY) and understand(q, [], {"grain": "month", "metrics": [{"kind": "monetary", "entity": None}]}, TODAY)["collapse"]:
        r, fr = ask(c, t, q)
        assert fr.shape == "scalar" and fr.value == AUG + SEP and fr.table is None
    else:
        pytest.fail(f"{q!r} should read as a collapse")


def test_total_does_not_collapse_when_the_metric_changes_or_adds(conv):
    c, t = conv
    ask(c, t, "last 2 months sales dikhao")
    r, fr = ask(c, t, "total qty bhi add karo")                       # additive: still a series with two measures
    assert fr.shape == "series" and list(fr.table.columns) == ["period", "Sales Amount", "Items Count"]


# ---- TEST 5: compound period request answers every part
def test_compound_period_request_answers_both_parts(conv):
    c, t = conv
    ask(c, t, "last 2 months ki sale kitni hui?")
    r, fr = ask(c, t, "August September ki aur August ki total chahiye")
    assert fr.shape == "scalar" and fr.table is None and fr.chart is None
    assert [(m["label"], m["value"]) for m in fr.metrics] == [("August + September 2026", AUG + SEP), ("August 2026", AUG)]
    assert _fmt(AUG + SEP, "Net Amount") in r.text and _fmt(AUG, "Net Amount") in r.text
    r, fr = ask(c, t, "August ka?")
    assert fr.shape == "scalar" and fr.value == AUG


def test_compound_resolver_shapes():
    p = resolve_date_expression("August September ka total aur August ka total", TODAY)
    assert [(a["label"], a["from"], a["to"]) for a in p["asks"]] == [("August + September 2026", "2026-08-01", "2026-09-27"), ("August 2026", "2026-08-01", "2026-08-31")]
    p = resolve_date_expression("August and September", TODAY)
    assert [a["label"] for a in p["asks"]] == ["August 2026", "September 2026", "August + September 2026"]
    assert "asks" not in resolve_date_expression("August", TODAY) and "asks" not in resolve_date_expression("August se September tak", TODAY)


# ---- TEST 6: month-wise values reconcile with the total
def test_series_total_reconciles_with_scalar(conv):
    c, t = conv
    r, fr = ask(c, t, "last 2 months sales month wise")
    assert float(fr.table["Net Amount"].sum()) == AUG + SEP and fr.metrics == []                 # single measure: the table is the answer
    r, fr = ask(c, t, "total kar ke batao")
    assert fr.value == AUG + SEP and "⚠️" not in r.text and len(c.totals) == 1                 # same key, same value: silent
    key = next(iter(c.totals))
    c.totals[key] = 30788965.33                                                                  # a stale, conflicting number from another path
    r, fr = ask(c, t, "total?")
    assert fr.value == AUG + SEP and "⚠️" in r.text and c.totals[key] == AUG + SEP               # recomputed, flagged, replaced — never shown silently


# ---- TEST 9: a simple question gets a simple answer
def test_simple_scalar_question_has_no_chart_or_table(conv):
    c, t = conv
    for q in ("total sale kitni hui?", "last 2 months ki total sale kitni hui?", "last month ki sale?"):
        r, fr = ask(c, t, q)
        assert fr.shape == "scalar" and fr.chart is None and fr.table is None and len(fr.metrics) == 1, q
    r, fr = ask(c, t, "last 2 months sales dikhao")
    assert fr.shape == "series" and fr.chart is not None                                       # asked to see it: chart + table


def test_period_explanation_when_asked(conv):
    c, t = conv
    r, fr = ask(c, t, "last 2 months ki sales kitni hai aur kaun si date tak ki hai?")
    assert fr.shape == "scalar" and fr.value == AUG + SEP
    assert "August 2026 (01-08-2026 → 31-08-2026)" in r.text and "September 2026 (01-09-2026 → 27-09-2026)" in r.text
    assert fr.period["period_type"] == "last_n_calendar_months" and fr.period["reference_date"] == "2026-09-27" and fr.date_range == "2026-08-01 → 2026-09-27"


# ---- TEST 10: one currency format everywhere
def test_currency_format_consistent_between_cards_and_table():
    df = pd.DataFrame({"period": ["2026-08", "2026-09"], "Net Amount": [14858907.21, 12108412.28], "Customer Count": [3, 2]})
    shown = display_table(df)
    assert list(shown["Net Amount"]) == ["₹1,48,58,907.21", "₹1,21,08,412.28"] and list(shown["Customer Count"]) == ["3", "2"] and list(shown["period"]) == ["2026-08", "2026-09"]
    assert shown["Net Amount"][0] == _fmt(14858907.21, "Net Amount") and list(df["Net Amount"]) == [14858907.21, 12108412.28]   # raw frame untouched
    assert _fmt(26967319.49, "AMOUNT") == "₹2,69,67,319.49" and _fmt(1500, "Qty") == "1,500"


# ---- the acceptance conversation, end to end
def test_acceptance_conversation(conv):
    c, t = conv
    seen = []
    for q, shape, value in [("Last 2 month ki sale kitni hui?", "scalar", AUG + SEP),
                            ("August September ki aur August ki total chahiye.", "scalar", None),
                            ("Last month ki total sale?", "scalar", AUG),
                            ("Last 2 months aur kaun si date tak?", "scalar", AUG + SEP),
                            ("Total kar ke batao.", "scalar", AUG + SEP),
                            ("Mujhe only total bata do bhai.", "scalar", AUG + SEP)]:
        r, fr = ask(c, t, q)
        seen.append((q, fr.shape, fr.value, len(fr.metrics), fr.table is not None, fr.chart is not None))
        assert fr.shape == shape and fr.table is None and fr.chart is None, seen
        if value is not None:
            assert fr.value == value and len(fr.metrics) == 1, seen
    assert [m["value"] for m in finalize(analyst.answer(c, "August September ki aur August ki total chahiye", "k", "m", t), "").metrics] == [AUG + SEP, AUG]
