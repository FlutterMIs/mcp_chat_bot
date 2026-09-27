"""Understanding layer + state executor: the acceptance conversation, all offline (no LLM).
Datasets: orders/customers (business), attendance (HR-ish). Nothing here depends on SALES/INVENTORY names."""
from datetime import date as _date


class date(_date):
    @classmethod
    def today(cls):
        return TODAY

import pandas as pd
import pytest

import analyst
from analysis import build_report, complete_periods
from analyst import Conversation, LocalTools
from grounding import unsupported_numbers
from understanding import parse_period, understand
from test_generic import make_source, schemas_of

TODAY = _date(2026, 9, 27)
rng = pd.date_range("2025-10-03", "2026-09-20", freq="9D")
ORDERS = pd.DataFrame({"Bill No": range(1, len(rng) + 1), "Party Name": [["Acme", "Zed", "Bolt", "Nova"][i % 4] for i in range(len(rng))],
                       "Bill Date": rng.strftime("%Y-%m-%d"), "Qty": [(i % 5) + 1 for i in range(len(rng))], "Net Amount": [100.0 * ((i % 7) + 1) for i in range(len(rng))]})
ORDERS = ORDERS[ORDERS["Bill Date"] < "2026-07-01"].reset_index(drop=True)           # July–September have no rows → must appear as zeros
TRUTH = ORDERS.assign(period=ORDERS["Bill Date"].str[:7]).groupby("period").agg(customers=("Party Name", "nunique"), items=("Qty", "sum"), amount=("Net Amount", "sum"))


class NoLLM:
    def __init__(self, *a): pass
    def plan(self, *a, **k): raise AssertionError("planner must not be called")
    def format_result(self, *a, **k): raise AssertionError("wording model must not be called")


@pytest.fixture
def conv(monkeypatch):
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    srv = make_source("orders", orders=ORDERS)
    return Conversation(schemas=schemas_of(srv)), LocalTools(srv)


def ask(conv, tools, q):
    return analyst.answer(conv, q, "k", "m", tools)


# ---- periods
def test_parse_periods():
    p = parse_period("bhai mujhe last 12 month ka data", TODAY)
    assert (p["from"], p["to"], p["grain"], p["periods"]) == ("2025-10-01", "2026-09-27", "month", 12)
    assert parse_period("last month sales", TODAY)["from"] == "2026-08-01" and parse_period("last month sales", TODAY)["to"] == "2026-08-31"
    assert parse_period("August wala dikhao", TODAY)["from"] == "2026-08-01"
    assert parse_period("pichle 3 mahine", TODAY)["periods"] == 3 and parse_period("2025 ka total", TODAY)["to"] == "2025-12-31"
    assert parse_period("total customers", TODAY) is None


# ---- acceptance: last 12 months × 3 metrics
ACCEPT = "bhai mujhe last 12 month 1 colume me date and uske colume me total customer count and fir uske bad total items count and fir total sales amounts"


def test_acceptance_three_metrics_12_months(conv):
    c, t = conv
    r = ask(c, t, ACCEPT)
    df = r.df
    assert list(df.columns) == ["period", "Customer Count", "Items Count", "Sales Amount"]
    assert len(df) == 12 and list(df["period"]) == [f"{y}-{m:02d}" for y, m in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, m) for m in range(1, 10)]]
    for _, row in df.iterrows():
        exp = TRUTH.loc[row["period"]] if row["period"] in TRUTH.index else None
        assert (row["Customer Count"], row["Items Count"], row["Sales Amount"]) == ((exp["customers"], exp["items"], exp["amount"]) if exp is not None else (0, 0, 0))
    assert "Customer Count = unique count of Party Name" in r.text and "Items Count = SUM of Qty" in r.text and "Sales Amount = SUM of Net Amount" in r.text
    assert r.chart == {"type": "line", "x": "period", "y": "Customer Count"} and r.drillable == "period"
    cards = {x["label"]: x["value"] for x in r.cards}
    assert cards["Customer Count"] == 4 and cards["Items Count"] == ORDERS["Qty"].sum() and cards["Sales Amount"] == ORDERS["Net Amount"].sum()
    assert c.state["grain"] == "month" and len(c.state["metrics"]) == 3


def test_single_metric_12_months_and_zero_months(conv):
    c, t = conv
    r = ask(c, t, "last 12 months sales")
    assert list(r.df.columns) == ["period", "Net Amount"] and len(r.df) == 12 and list(r.df["Net Amount"])[-3:] == [0, 0, 0]
    r2 = ask(c, t, "last 12 months customer count")
    assert list(r2.df.columns) == ["period", "Customer Count"] and r2.df["Customer Count"].max() <= 4
    r3 = ask(c, t, "last 12 months items count")
    assert list(r3.df.columns) == ["period", "Qty"] and r3.df["Qty"].sum() == ORDERS["Qty"].sum()      # SUM of quantity, not rows, not COUNT(name)


def test_items_never_rows_matched_or_name_count(conv):
    c, t = conv
    r = ask(c, t, "last 12 months items count")
    assert "Bill No" not in r.text and r.df["Qty"].sum() == ORDERS["Qty"].sum() != len(ORDERS)
    assert unsupported_numbers(f"{len(ORDERS)} items sold", {"rows": [{"period": "2026-01", "value": 5}], "rows_matched": len(ORDERS)}) == [float(len(ORDERS))]


# ---- follow-ups keep context
def test_follow_ups_add_metric_drill_top_report(conv):
    c, t = conv
    ask(c, t, "last 12 months sales")
    r = ask(c, t, "customer count bhi add karo")
    assert list(r.df.columns) == ["period", "Sales Amount", "Customer Count"] and len(r.df) == 12       # additive, same period
    r = ask(c, t, "items bhi")
    assert list(r.df.columns) == ["period", "Sales Amount", "Customer Count", "Items Count"]
    r = ask(c, t, "August")                                                                            # drill into a month with no rows
    assert r.df is None and "August 2026" in r.text and "koi data nahi" not in r.text and c.state["period"]["from"] == "2026-08-01" and c.state["grain"] is None
    r = ask(c, t, "March")                                                                             # drill into a month with data
    assert r.df is None and c.state["period"]["from"] == "2026-03-01"
    r = ask(c, t, "party wise dikhao")
    assert list(r.df.columns)[0] == "Party Name" and c.state["period"]["from"] == "2026-03-01" and len(r.df) >= 2   # March context kept
    r = ask(c, t, "top 2")
    assert len(r.df) == 2 and c.state["top_n"] == 2
    r = ask(c, t, "iska report bana do")
    assert r.kind == "report" and r.files and r.files[0]["name"].endswith(".xlsx")
    sheets = pd.read_excel(pd.io.common.BytesIO(r.files[0]["bytes"]), sheet_name=None)
    assert {"Summary", "Data"} <= set(sheets) and len(sheets["Data"]) == 2


def test_correction_changes_metric_reading(conv):
    c, t = conv
    ask(c, t, "last 12 months customer count")
    r = ask(c, t, "nahi, total items chahiye")
    assert list(r.df.columns) == ["period", "Qty"] and c.state["metrics"][0]["kind"] == "count" and c.state["resolved"][0]["aggregation"] == "sum"


def test_date_explanation_from_state(conv):
    c, t = conv
    ask(c, t, ACCEPT)
    r = ask(c, t, "date kaunsi use ki thi?")
    assert "Date column: Bill Date" in r.text and "01-10-2025 → 27-09-2026" in r.text and "unique count of column *Party Name*" in r.text
    assert r.df is None


def test_chart_matches_table_order(conv):
    c, t = conv
    r = ask(c, t, "last 12 months sales")
    from chart_ui import _build, _prepare
    import json
    spec = json.loads(_build(_prepare(r.df, "period", "Net Amount", "line"), "period", "Net Amount", "line").to_json())
    assert [d["period"] for d in spec["datasets"][next(iter(spec["datasets"]))]] == list(r.df["period"])


def test_complete_periods_helper():
    df = pd.DataFrame({"period": ["2026-02"], "X": [5]})
    out = complete_periods(df, ["period"], "month", "2026-01-01", "2026-03-31")
    assert list(out["period"]) == ["2026-01", "2026-02", "2026-03"] and list(out["X"]) == [0, 5, 0]


def test_ambiguous_metric_asks(monkeypatch):
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    two = ORDERS.assign(**{"Gross Amount": ORDERS["Net Amount"] * 1.1})
    srv = make_source("orders", orders=two)
    c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, "last 12 months sales amount", "k", "m", LocalTools(srv))
    assert r.kind == "clarify" and "Net Amount" in r.text and "Gross Amount" in r.text


def test_hr_dataset_month_wise_hours(monkeypatch):
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    att = pd.DataFrame({"Employee": ["Asha", "Ravi", "Asha", "Meena"], "Work Date": ["2026-08-01", "2026-08-02", "2026-09-01", "2026-09-03"], "Hours": [8, 9, 7.5, 6]})
    srv = make_source("att", att=att)
    c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, "last 3 months month wise hours aur employee count", "k", "m", LocalTools(srv))
    assert list(r.df.columns) == ["period", "Items Count", "Employee Count"] and list(r.df["period"]) == ["2026-07", "2026-08", "2026-09"]
    assert list(r.df["Employee Count"]) == [0, 2, 2] and list(r.df["Items Count"]) == [0, 17, 13.5]


def test_column_choice_is_asked_with_options_then_remembered(monkeypatch, tmp_path):
    import memory
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    two = ORDERS.assign(**{"Box Qty": ORDERS["Qty"] * 2})
    srv = make_source("orders", orders=two)
    c = Conversation(schemas=schemas_of(srv)); t = LocalTools(srv)
    r = analyst.answer(c, ACCEPT, "k", "m", t)
    assert r.kind == "clarify" and r.options == ["Qty", "Box Qty"] and c.pending_choice["key"] == "count:item"
    r2 = analyst.answer(c, "Qty se bro", "k", "m", t)                                    # the answer to the question
    assert r2.kind == "answer" and list(r2.df.columns) == ["period", "Customer Count", "Items Count", "Sales Amount"] and c.pending_choice is None
    c2 = Conversation(schemas=schemas_of(srv))
    r3 = analyst.answer(c2, ACCEPT, "k", "m", t)                                          # new conversation, same source: not asked again
    assert r3.kind == "answer" and r3.df["Items Count"].sum() == ORDERS["Qty"].sum()
    from analyst import _pick_option
    assert _pick_option("2", ["Qty", "Box Qty"]) == "Box Qty" and _pick_option("box qty wala", ["Qty", "Box Qty"]) == "Box Qty" and _pick_option("kuch bhi", ["Qty", "Box Qty"]) is None


def test_secondary_column_not_asked(conv, monkeypatch):
    two = ORDERS.assign(ALT_QTY=ORDERS["Qty"] * 2).rename(columns={"Qty": "QTY"})
    srv = make_source("orders", orders=two)
    c = Conversation(schemas=schemas_of(srv))
    r = analyst.answer(c, ACCEPT, "k", "m", LocalTools(srv))
    assert r.kind == "answer" and "Items Count = SUM of QTY" in r.text


def test_clicked_row_period_drills_into_that_month(conv):
    c, t = conv
    ask(c, t, ACCEPT)
    r = ask(c, t, "2026-01 details")
    assert r.df is None and c.state["period"]["from"] == "2026-01-01" and c.state["period"]["to"] == "2026-01-31" and c.state["grain"] is None and "January 2026" in r.text


def test_planner_top_n_ignored_unless_asked(monkeypatch):
    from test_multi_metric import ORDERS as O, planner, BASE, Q
    srv = make_source("o", o=O)
    monkeypatch.setattr(analyst, "OpenRouterAI", planner(dict(BASE, top_n=12, sort="asc")))
    r = analyst.answer(Conversation(schemas=schemas_of(srv)), Q, "k", "m", LocalTools(srv))
    assert list(r.df["period"]) == sorted(r.df["period"]) and len(r.df) == 12
