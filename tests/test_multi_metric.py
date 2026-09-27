"""Multi-metric time-series answers are computed in code: every requested metric resolved from the schema,
aggregated, joined by period, chronologically sorted and named. No LLM wording, no rows_matched as a business number."""
import pandas as pd
import pytest

import analyst
from analyst import Conversation, LocalTools
from grounding import unsupported_numbers
from semantics import AmbiguousMeasure, requested_measures, resolve_measures
from source_loader import column_info
from test_generic import make_source, schemas_of

ORDERS = pd.DataFrame({
    "Order No": range(1, 9), "Customer": ["Acme", "Zed", "Acme", "Bolt", "Zed", "Acme", "Nova", "Bolt"],
    "Order Date": ["2026-03-05", "2026-01-20", "2026-02-02", "2026-02-15", "2026-03-01", "2026-01-09", "2026-03-20", "2026-03-22"],
    "Revenue": [1200, 800, 1500, 300, 950, 700, 400, 250], "Units": [3, 2, 5, 1, 4, 2, 1, 1]})
TRUTH = ORDERS.assign(period=ORDERS["Order Date"].str[:7]).groupby("period").agg(customers=("Customer", "nunique"), units=("Units", "sum"), revenue=("Revenue", "sum"))
BASE = {"status": "execute", "mode": "data", "source_id": "o", "operation": "aggregate", "metric": "Revenue", "aggregation": "sum",
        "date_column": "Order Date", "date_grain": "month", "date_from": "2025-04-01", "date_to": "2026-03-31", "group_by": []}


def planner(plan):
    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx): return dict(plan)
        def format_result(self, *a, **k): raise AssertionError("no wording call for multi-metric answers")
    return AI


Q = "bhai mujhe last 12 month 1 column me date and uske column me total customer count and fir uske bad total items count and fir total sales amounts"


def test_three_metrics_by_month(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    monkeypatch.setattr(analyst, "OpenRouterAI", planner(BASE))
    r = analyst.answer(conv, Q, "k", "m", LocalTools(srv))
    df = r.df
    assert list(df.columns) == ["period", "Customer Count", "Items Count", "Sales Amount"]
    assert len(df) == 12 and list(df["period"])[-3:] == ["2026-07", "2026-08", "2026-09"] and list(df["period"]) == sorted(df["period"])   # 12 months, chronological
    got = df.set_index("period")
    assert list(got.loc[TRUTH.index, "Customer Count"]) == list(TRUTH["customers"])                  # distinct customers
    assert list(got.loc[TRUTH.index, "Items Count"]) == list(TRUTH["units"])                         # SUM of quantity, not row count
    assert list(got.loc[TRUTH.index, "Sales Amount"]) == list(TRUTH["revenue"])                      # SUM of amount
    assert got.loc["2026-08"].sum() == 0
    assert "Customer Count, Items Count, Sales Amount" in r.text and r.chart["y"] == "Customer Count" and r.chart["x"] == "period"
    assert conv.last_plan["operation"] == "multi_metric"


def test_items_count_is_not_row_count():
    cols = column_info(ORDERS)
    m = resolve_measures(requested_measures("month wise items count and customer count", cols), cols)
    assert {x["label"]: (x["column"], x["aggregation"]) for x in m} == {"Items Count": ("Units", "sum"), "Customer Count": ("Customer", "count_distinct")}
    assert unsupported_numbers("Total 8 items sold", {"rows": [{"period": "2026-01", "value": 1500}], "rows_matched": 8}) == [8.0]


def test_missing_metric_asks_instead_of_guessing():
    cols = column_info(ORDERS.drop(columns=["Units"]))
    with pytest.raises(AmbiguousMeasure, match="quantity"):
        resolve_measures([{"kind": "quantity", "entity": None}], cols)


def test_ambiguous_column_asks(monkeypatch):
    two = ORDERS.assign(**{"Net Revenue": ORDERS["Revenue"] * 0.9})
    srv = make_source("o", o=two)
    conv = Conversation(schemas=schemas_of(srv))
    monkeypatch.setattr(analyst, "OpenRouterAI", planner(BASE))
    r = analyst.answer(conv, "month wise sales amount aur customer count", "k", "m", LocalTools(srv))
    assert r.kind == "clarify" and "Revenue" in r.text and "Net Revenue" in r.text and r.df is None


def test_dimension_wise_multi_metric(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    monkeypatch.setattr(analyst, "OpenRouterAI", planner(dict(BASE, date_grain=None, date_from=None, date_to=None, group_by=["Customer"])))
    r = analyst.answer(conv, "customer wise total units aur revenue", "k", "m", LocalTools(srv))
    assert list(r.df.columns) == ["Customer", "Items Count", "Sales Amount"] and list(r.df["Customer"]) == ["Acme", "Bolt", "Nova", "Zed"]
    assert r.df.loc[r.df["Customer"] == "Acme", "Sales Amount"].iloc[0] == 3400


def test_students_schema_multi_metric(monkeypatch):
    from test_generic import C_STUDENTS
    srv = make_source("c", c=C_STUDENTS.assign(**{"Fees Paid": [100, 200, 300, 400]}))
    conv = Conversation(schemas=schemas_of(srv))
    monkeypatch.setattr(analyst, "OpenRouterAI", planner({"status": "execute", "mode": "data", "source_id": "c", "operation": "aggregate", "metric": "Marks", "aggregation": "sum", "group_by": ["Class"]}))
    r = analyst.answer(conv, "class wise total marks, fees paid aur student count", "k", "m", LocalTools(srv))
    assert list(r.df.columns) == ["Class", "Items Count", "Sales Amount", "Student Count"] or set(r.df.columns) == {"Class", "Items Count", "Sales Amount", "Student Count"}
    assert list(r.df["Student Count"]) == [2, 2]
