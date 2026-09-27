"""Regression tests for the correctness spec (multi-metric, rows_matched, date questions, ordering, single value,
claim validation, corrections). Unrelated schemas only: orders/revenue/customer, employees/salary, students, projects."""
import json

import pandas as pd
import pytest

import agent as agentmod
import analyst
from agent import Agent, classify
from analyst import Conversation, LocalTools, Reply
from grounding import mislabeled_numbers, result_definition, unsupported_claims, unsupported_numbers
from mcp_server import MCPServer
from openrouter import compact_result
from semantics import requested_measures
from test_agent import ScriptedAI
from test_generic import A_EMPLOYEES, B_ORDERS, C_STUDENTS, D_PROJECTS, make_source, schemas_of

ORDERS = pd.DataFrame({
    "Order No": range(1, 9), "Customer": ["Acme", "Zed", "Acme", "Bolt", "Zed", "Acme", "Nova", "Bolt"],
    "Order Date": ["2026-03-05", "2026-01-20", "2026-02-02", "2026-02-15", "2026-03-01", "2026-01-09", "2026-03-20", "2026-03-22"],
    "Revenue": [1200, 800, 1500, 300, 950, 700, 400, 250], "Units": [3, 2, 5, 1, 4, 2, 1, 1]})


def fake_ai(plan_by_call, answers):
    plans, ans = list(plan_by_call), list(answers)

    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx): return plans.pop(0) if len(plans) > 1 else plans[0]
        def format_result(self, q, p, r, feedback=None): return {"answer": ans.pop(0) if len(ans) > 1 else ans[0]}
    return AI


# TEST 1: single metric amount
def test_1_single_metric_amount(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    plan = {"status": "execute", "mode": "data", "source_id": "o", "operation": "aggregate", "metric": "Revenue", "aggregation": "sum",
            "date_column": "Order Date", "date_from": "2026-03-01", "date_to": "2026-03-31"}
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([plan], ["March ka revenue ₹2,800 hai."]))
    r = analyst.answer(conv, "march ka revenue kitna hai", "k", "m", LocalTools(srv))
    assert "₹2,800" in r.text and r.df is None                     # TEST 8: no one-cell "value" table


# TEST 2: amount + quantity + customer count (month wise) → agent path, real columns
def test_2_multi_metric_month_wise_agent(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    wanted = requested_measures("month wise revenue, units aur customer count do", srv.source_schema("o")["columns"])
    assert [w["kind"] for w in wanted] == ["monetary", "quantity", "count"] and wanted[2]["entity"] == "customer"
    ai = ScriptedAI([
        [("aggregate_data", {"source_id": "o", "metric": "Revenue", "date_column": "Order Date", "date_grain": "month"})],
        [("aggregate_data", {"source_id": "o", "metric": "Units", "date_column": "Order Date", "date_grain": "month"})],
        [("aggregate_data", {"source_id": "o", "metric": "Customer", "aggregation": "count_distinct", "date_column": "Order Date", "date_grain": "month"})],
        [("join_results", {"left_result_id": "r1", "right_result_id": "r2", "left_key": "period", "right_key": "period"})],
        [("join_results", {"left_result_id": "r4", "right_result_id": "r3", "left_key": "period", "right_key": "period"})],
        [("finish", {"answer": "Month wise table: Jan revenue 1500 (units 4, customers 2), Feb 1800 (6, 2), Mar 2800 (9, 4).", "result_ids": ["r5"], "primary_result_id": "r5"})],
    ])
    a = Agent(ai, LocalTools(srv), conv, "month wise revenue, units aur customer count do", requested=wanted)
    r = a.run()
    assert r.kind == "agent"
    df = r.df
    assert list(df["period"]) == ["2026-01", "2026-02", "2026-03"]                     # TEST 6: chronological
    assert list(df["Revenue"]) == [1500, 1800, 2800] and list(df["Units"]) == [4, 6, 9] and list(df["Customer"]) == [2, 2, 4]


def test_2b_agent_rejects_finish_missing_requested_metric():
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    wanted = requested_measures("month wise revenue aur customer count", srv.source_schema("o")["columns"])
    ai = ScriptedAI([
        [("aggregate_data", {"source_id": "o", "metric": "Revenue", "date_column": "Order Date", "date_grain": "month"})],
        [("finish", {"answer": "Revenue aur customer count month wise add kar diye: Jan 1500, Feb 1800, Mar 2800.", "result_ids": ["r1"]})],   # claims customers
        [("aggregate_data", {"source_id": "o", "metric": "Customer", "aggregation": "count_distinct", "date_column": "Order Date", "date_grain": "month"})],
        [("join_results", {"left_result_id": "r1", "right_result_id": "r2", "left_key": "period", "right_key": "period"})],
        [("finish", {"answer": "Jan 1500 (2 customers), Feb 1800 (2), Mar 2800 (4).", "result_ids": ["r3"], "primary_result_id": "r3"})],
    ])
    a = Agent(ai, LocalTools(srv), conv, "month wise revenue aur customer count", requested=wanted)
    r = a.run()
    assert r.kind == "agent" and "Customer" in r.df.columns and len(a.trace) == 3


# TEST 3: rows_matched must never become "items sold"
def test_3_rows_matched_is_not_a_business_number():
    result = {"rows": [{"value": 14858907.21}], "metric": "AMOUNT", "aggregation": "sum", "rows_matched": 1209}
    assert unsupported_numbers("Total ₹1,48,58,907.21; is mahine 1209 items sell hue.", result) == [1209.0]
    c = compact_result(result)
    assert "rows_matched" not in c and c["matching_rows_technical_metadata"]["rows"] == 1209 and "NOT a business metric" in c["matching_rows_technical_metadata"]["note"]
    d = result_definition(result, {"metric": "AMOUNT"})
    assert d["matching_rows_technical_metadata"] == 1209 and [c["name"] for c in d["columns"]] == ["AMOUNT"]


# TEST 4 + 5: which date did you use / how did you calculate last month → re-check explanation, no planner call
@pytest.mark.parametrize("q", ["bhai ye kon se kon sa date use kiya hai data ke liye", "last month kaise nikala?", "which date range did you use?", "kaunsi date column use hui"])
def test_4_5_date_questions_explain_the_range(q, monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    prev = {"status": "execute", "mode": "data", "source_id": "o", "sheet_name": None, "operation": "aggregate", "metric": "Revenue", "aggregation": "sum",
            "date_column": "Order Date", "date_from": "2026-02-01", "date_to": "2026-02-28"}
    conv.last_plan, conv.recent_plans = dict(prev), [{"question": "last month revenue", "plan": dict(prev)}]
    class NoAI:
        def __init__(self, *a): pass
        def plan(self, *a): raise AssertionError("must not plan")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoAI)
    r = analyst.answer(conv, q, "k", "m", LocalTools(srv))
    assert "Date column: Order Date" in r.text and "01-02-2026 → 28-02-2026" in r.text and "₹1,800" in r.text
    assert r.chart is None and (r.df is None or len(r.df) <= 1)                       # no date dump, no chart


# TEST 6 + 7: chronological order, chart uses the same order
def test_6_7_month_wise_chronological_and_chart_same_order():
    srv = make_source("o", o=ORDERS)
    res = srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Revenue", "date_column": "Order Date", "date_grain": "month", "sort": "desc"})
    assert [r["period"] for r in res["rows"]] == ["2026-01", "2026-02", "2026-03"]      # chronological even if a sort was requested
    df = pd.DataFrame(res["rows"])
    from chart_ui import _build, _prepare, chartable
    c = _prepare(df, "period", "value", "line")
    spec = json.loads(_build(c, "period", "value", "line").to_json())
    assert [d["period"] for d in spec["datasets"][next(iter(spec["datasets"]))]] == ["2026-01", "2026-02", "2026-03"]
    assert spec["encoding"]["x"].get("sort") is None and chartable(df)
    top = srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Revenue", "date_column": "Order Date", "date_grain": "month", "top_n": 1})
    assert [r["period"] for r in top["rows"]] == ["2026-03"]                          # top-N over months is still a ranking


# TEST 9: wrong metric correction (units → revenue)
def test_9_wrong_metric_correction(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    old = {"status": "execute", "mode": "data", "source_id": "o", "operation": "aggregate", "metric": "Units", "aggregation": "sum"}
    conv.last_plan, conv.recent_plans = dict(old), [{"question": "total units", "plan": dict(old)}]
    new = dict(old, metric="Revenue")
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([dict(old), new], ["Total revenue ₹6,100 hai."]))
    r = analyst.answer(conv, "no, units nahi — revenue chahiye", "k", "m", LocalTools(srv))
    assert conv.last_plan["metric"] == "Revenue" and "₹6,100" in r.text


# TEST 10: wrong sheet correction (Attendance → Salaries)
def test_10_wrong_sheet_correction(monkeypatch):
    from test_generic import E_ATTENDANCE
    srv = make_source("wb", wb={"Attendance": E_ATTENDANCE, "Salaries": A_EMPLOYEES})
    conv = Conversation(schemas=schemas_of(srv))
    old = {"status": "execute", "mode": "data", "source_id": "wb", "sheet_name": "Attendance", "operation": "aggregate", "metric": "Hours", "aggregation": "sum"}
    conv.last_plan, conv.recent_plans = dict(old), [{"question": "total hours", "plan": dict(old)}]
    new = dict(old, sheet_name="Salaries", metric="Salary")
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([dict(old), new], ["Total salary ₹2,87,000 hai."]))
    r = analyst.answer(conv, "wrong sheet — use the Salaries data", "k", "m", LocalTools(srv))
    assert conv.last_plan["sheet_name"] == "Salaries" and "₹2,87,000" in r.text


# TEST 11: unsupported metric claim is rejected
def test_11_unsupported_claim_rejected(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    plan = {"status": "execute", "mode": "data", "source_id": "o", "operation": "aggregate", "metric": "Revenue", "aggregation": "sum", "date_column": "Order Date", "date_grain": "month"}
    lie = "Month wise revenue, units count aur distinct customer count add kiye gaye hain: Jan 1500, Feb 1800, Mar 2800."
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([plan], [lie, lie]))
    r = analyst.answer(conv, "month wise revenue do", "k", "m", LocalTools(srv))
    assert "customer" not in r.text.lower() and "add kiye" not in r.text and list(r.df.columns)[0] == "period" and len(r.df.columns) == 2
    d = result_definition({"rows": [{"period": "2026-01", "value": 1500}], "metric": "Revenue", "aggregation": "sum"}, plan)
    assert set(unsupported_claims(lie, d, srv.source_schema("o")["columns"])) == {"quantity column", "count of customer"}
    assert unsupported_claims("Month wise revenue diya gaya hai: Jan 1500.", d, srv.source_schema("o")["columns"]) == []


# TEST 12: number/label combination rejected
def test_12_mislabeled_number_rejected():
    result = {"rows": [{"Customer": "Acme", "value": 3400}, {"Customer": "Zed", "value": 1750}, {"Customer": "Bolt", "value": 550}], "metric": "Revenue"}
    assert mislabeled_numbers("Zed ka revenue 3400 hai.", result) == [(3400.0, "zed")]
    assert mislabeled_numbers("Acme ka revenue 3400 hai. Zed 1750.", result) == []


# TEST 13: completely different schema end to end (students / marks / class)
def test_13_students_schema(monkeypatch):
    srv = make_source("c", c=C_STUDENTS)
    conv = Conversation(schemas=schemas_of(srv))
    plan = {"status": "execute", "mode": "data", "source_id": "c", "operation": "aggregate", "metric": "Marks", "aggregation": "avg", "group_by": ["Class"]}
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([plan], ["Class wise average marks: 10A 77.5, 10B 83."]))
    r = analyst.answer(conv, "class wise average marks batao", "k", "m", LocalTools(srv))
    assert list(r.df["Class"]) == ["10A", "10B"] and list(r.df.iloc[:, 1]) == [77.5, 83.0] and "₹" not in r.text
    proj = make_source("p", p=D_PROJECTS)
    res = proj.call_tool("aggregate_source", {"source_id": "p", "metric": "Owner", "aggregation": "count_distinct"})
    assert res["rows"] == [{"value": 2}]


# TEST 14: "what can you help me with" is not a refusal
def test_14_help_question(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    class NoAI:
        def __init__(self, *a): pass
        def plan(self, *a): raise AssertionError("must not plan")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoAI)
    r = analyst.answer(conv, "tum meri kiya help kar sakte ho ?", "k", "m", LocalTools(srv))
    assert r.kind == "help" and "Totals" in r.text and "o" in r.text


def test_multi_metric_is_computed_in_code(monkeypatch):
    srv = make_source("o", o=ORDERS)
    conv = Conversation(schemas=schemas_of(srv))
    base = {"status": "execute", "mode": "data", "source_id": "o", "operation": "aggregate", "metric": "Revenue", "aggregation": "sum",
            "date_column": "Order Date", "date_grain": "month", "group_by": []}
    monkeypatch.setattr(agentmod, "run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent must not be used")))
    monkeypatch.setattr(analyst, "OpenRouterAI", fake_ai([base], ["unused"]))
    r = analyst.answer(conv, "month wise revenue aur units aur customer count", "k", "m", LocalTools(srv))
    assert list(r.df.columns) == ["period", "Items Count", "Sales Amount", "Customer Count"] or set(r.df.columns) == {"period", "Items Count", "Sales Amount", "Customer Count"}
    assert list(r.df["period"]) == ["2026-01", "2026-02", "2026-03"]
