"""Data-agnostic tests: seven unrelated schemas, no LLM, no network.
The planner is faked where a plan is needed, so these test the deterministic layers: tools, semantics, validation,
corrections, grounding, memory scoping, token compaction and security guards."""
import json
import pathlib
import tempfile

import pandas as pd
import pytest

import analyst
import memory
from analyst import Conversation, LocalTools, Reply
from grounding import enrich_result, unsupported_numbers
from mcp_server import MCPServer
from openrouter import compact_result, compact_schema
from semantics import column_kind, is_pure_complaint, plan_diff, question_intent, validate_plan
from source_loader import column_info

# ---------------------------------------------------------------- datasets
A_EMPLOYEES = pd.DataFrame({"Employee": ["Asha", "Ravi", "Meena", "Karan", "Asha"], "Department": ["Sales", "IT", "IT", "HR", "Sales"],
                            "Salary": [50000, 72000, 68000, 45000, 52000], "Joining Date": ["01/02/2024", "15/03/2023", "20/07/2025", "10/01/2022", "05/05/2026"], "Emp ID": [101, 102, 103, 104, 105]})
B_ORDERS = pd.DataFrame({"Order No": [1, 2, 3, 4, 5, 6], "Customer": ["Acme", "Zed", "Acme", "Bolt", "Zed", "Acme"],
                         "Order Date": ["2026-01-05", "2026-01-20", "2026-02-02", "2026-02-15", "2026-03-01", "2026-03-09"], "Revenue": [1200, 800, 1500, 300, 950, 700], "Units": [3, 2, 5, 1, 4, 2]})
C_STUDENTS = pd.DataFrame({"Student": ["Ira", "Om", "Zara", "Dev"], "Class": ["10A", "10A", "10B", "10B"], "Marks": [88, 67, 92, 74], "Roll No": [1, 2, 3, 4]})
D_PROJECTS = pd.DataFrame({"Project": ["Alpha", "Beta", "Gamma"], "Budget": [100000, 250000, 80000], "Cost": [90000, 270000, 60000], "Owner": ["Ravi", "Meena", "Ravi"]})
E_ATTENDANCE = pd.DataFrame({"Employee": ["Asha", "Ravi", "Asha", "Ravi", "Meena"], "Date": ["2026-09-01", "2026-09-01", "2026-09-02", "2026-09-02", "2026-09-02"], "Hours": [8, 9, 7.5, 8, 6]})
F_TARGETS = pd.DataFrame({"Customer": ["Acme", "Zed", "Bolt", "Nova"], "Target": [3000, 2000, 500, 1000]})
G_UNKNOWN = pd.DataFrame({"zx_1": ["p", "q", "p", "r"], "zx_2": [10.5, 20.0, 30.25, 5.0], "zx_3": ["2026-04-01", "2026-04-02", "2026-05-01", "2026-05-03"], "zx_4": [1, 2, 3, 4]})


def make_source(name, **tabs):
    srv = MCPServer()
    for sid, frames in tabs.items():
        if isinstance(frames, dict):
            srv.register_file(sid, {"kind": "workbook", "name": sid, "tabs": {k: v.copy() for k, v in frames.items()}})
        else:
            srv.register_file(sid, {"kind": "table", "name": sid, "df": frames.copy()})
    return srv


def schemas_of(srv):
    return {sid: srv.source_schema(sid) for sid in srv.sources}


# ---------------------------------------------------------------- tools on unrelated schemas
def test_tools_sum_avg_max_min_count_on_employees():
    srv = make_source("hr", hr=A_EMPLOYEES)
    agg = lambda **a: srv.call_tool("aggregate_source", {"source_id": "hr", "metric": "Salary", **a})["rows"]
    assert agg() == [{"value": 287000}]
    assert agg(aggregation="avg")[0]["value"] == pytest.approx(57400)
    assert agg(aggregation="max") == [{"value": 72000}] and agg(aggregation="min") == [{"value": 45000}]
    assert agg(aggregation="count") == [{"value": 5}]
    top = agg(group_by=["Department"], sort="desc", top_n=1)
    assert top == [{"Department": "IT", "value": 140000}]


def test_group_filter_date_on_orders():
    srv = make_source("o", o=B_ORDERS)
    r = srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Revenue", "group_by": ["Customer"], "sort": "desc"})
    assert [x["Customer"] for x in r["rows"]] == ["Acme", "Zed", "Bolt"] and r["grand_total_all_groups"] == 5450
    feb = srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Revenue", "date_column": "Order Date", "date_from": "2026-02-01", "date_to": "2026-02-28"})
    assert feb["rows"] == [{"value": 1800}]
    monthly = srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Units", "date_column": "Order Date", "date_grain": "month"})
    assert [(x["period"], x["value"]) for x in monthly["rows"]] == [("2026-01", 5), ("2026-02", 6), ("2026-03", 6)]
    big = srv.call_tool("query_source", {"source_id": "o", "filters": [{"column": "Revenue", "op": "gte", "value": 1000}], "sort_by": "Revenue", "sort": "desc"})
    assert [x["Order No"] for x in big["rows"]] == [3, 1]


def test_tools_on_unknown_column_names():
    srv = make_source("g", g=G_UNKNOWN)
    r = srv.call_tool("aggregate_source", {"source_id": "g", "metric": "zx_2", "group_by": ["zx_1"], "sort": "desc", "top_n": 1})
    assert r["rows"] == [{"zx_1": "p", "value": 40.75}]
    m = srv.call_tool("aggregate_source", {"source_id": "g", "metric": "zx_2", "date_column": "zx_3", "date_grain": "month"})
    assert [x["period"] for x in m["rows"]] == ["2026-04", "2026-05"]


def test_percentage_and_change_are_deterministic():
    r = enrich_result({"rows": [{"period": "2026-01", "value": 2000}, {"period": "2026-02", "value": 1800}]}, {"date_grain": "month"})
    ch = r["derived"]["change_vs_previous_row"][0]
    assert ch["change"] == -200 and ch["change_pct"] == -10.0
    assert r["derived"]["share_pct"][0]["pct"] == pytest.approx(52.63, abs=0.01)


# ---------------------------------------------------------------- semantics
def test_column_kinds_without_domain_knowledge():
    kinds = {c["name"]: column_kind(c) for c in column_info(A_EMPLOYEES)}
    assert kinds["Salary"] == "monetary" and kinds["Emp ID"] == "identifier" and kinds["Department"] == "dimension" and kinds["Joining Date"] == "date"
    kinds = {c["name"]: column_kind(c) for c in column_info(C_STUDENTS)}
    assert kinds["Marks"] == "quantity" and kinds["Roll No"] == "identifier"
    kinds = {c["name"]: column_kind(c) for c in column_info(G_UNKNOWN)}
    assert kinds["zx_2"] == "measure" and kinds["zx_4"] == "identifier" and kinds["zx_3"] == "date"


@pytest.mark.parametrize("q,agg,sup,n,grp,kind", [
    ("total salary batao", "sum", False, None, False, "monetary"),
    ("average marks per class", "avg", False, None, True, "quantity"),
    ("kis employee ki salary sabse zyada hai", "max", True, None, False, "monetary"),
    ("top 3 customers by revenue", "max", True, 3, True, "monetary"),
    ("how many students in 10A", "count", False, None, False, None),
    ("hours kitne lage har employee ke", "count", False, None, True, "quantity"),
    ("budget vs cost compare karo", None, False, None, False, "monetary"),
])
def test_question_intent(q, agg, sup, n, grp, kind):
    i = question_intent(q)
    assert (i["aggregation"], i["superlative"], i["top_n"], i["grouping"], i["measure_kind"]) == (agg, sup, n, grp, kind)


# ---------------------------------------------------------------- validation (the wrong-answer scenarios, generic)
def plan(**k):
    return {"status": "execute", "mode": "data", "source_id": "hr", "operation": "aggregate", "aggregation": "sum", **k}


def test_wrong_metric_kind_rejected_salary_vs_headcount():
    schema = MCPServer().register_file("hr", {"kind": "table", "name": "hr", "df": A_EMPLOYEES.assign(**{"Headcount Units": [1, 1, 1, 1, 1]})})
    with pytest.raises(ValueError, match="monetary"):
        validate_plan("total salary kitni hai", plan(metric="Headcount Units"), schema)
    validate_plan("total salary kitni hai", plan(metric="Salary"), schema)     # passes


def test_named_column_must_be_used():
    schema = MCPServer().register_file("p", {"kind": "table", "name": "p", "df": D_PROJECTS})
    with pytest.raises(ValueError, match='names the column "Budget"'):
        validate_plan("total budget batao", plan(source_id="p", metric="Cost"), schema)


def test_identifier_never_a_metric():
    schema = MCPServer().register_file("hr", {"kind": "table", "name": "hr", "df": A_EMPLOYEES})
    with pytest.raises(ValueError, match="identifier"):
        validate_plan("employees ka total", plan(metric="Emp ID"), schema)


def test_wrong_sheet_rejected_generic():
    srv = make_source("wb", wb={"Attendance": E_ATTENDANCE, "Salaries": A_EMPLOYEES})
    schema = srv.source_schema("wb")
    with pytest.raises(ValueError, match='"Salaries"'):
        validate_plan("salary ka total", plan(source_id="wb", sheet_name="Attendance", metric="Hours"), schema)
    validate_plan("salary ka total", plan(source_id="wb", sheet_name="Salaries", metric="Salary"), schema)


def test_superlative_needs_ranking_not_sum():
    schema = MCPServer().register_file("o", {"kind": "table", "name": "o", "df": B_ORDERS})
    with pytest.raises(ValueError, match="highest/lowest"):
        validate_plan("which one is highest?", plan(source_id="o", metric="Revenue"), schema)
    p = validate_plan("kis customer ka revenue sabse zyada hai", plan(source_id="o", metric="Revenue", group_by=["Customer"]), schema)
    assert p["sort"] == "desc" and p["top_n"] == 1


def test_average_must_use_avg():
    schema = MCPServer().register_file("c", {"kind": "table", "name": "c", "df": C_STUDENTS})
    with pytest.raises(ValueError, match="average"):
        validate_plan("average marks", plan(source_id="c", metric="Marks"), schema)


def test_named_dimension_must_be_grouping():
    schema = MCPServer().register_file("hr", {"kind": "table", "name": "hr", "df": A_EMPLOYEES})
    with pytest.raises(ValueError, match='group_by \\["Department"\\]'):
        validate_plan("department wise salary", plan(metric="Salary", group_by=["Employee"]), schema)


# ---------------------------------------------------------------- corrections and follow-ups
def test_pure_complaint_vs_correction():
    schema = MCPServer().register_file("hr", {"kind": "table", "name": "hr", "df": A_EMPLOYEES})
    assert is_pure_complaint("ye galat hai", schema) and is_pure_complaint("data not correct", schema)
    assert not is_pure_complaint("galat hai, salary chahiye", schema)
    assert not is_pure_complaint("wrong — use the department data", schema)
    assert not is_pure_complaint("not quantity, amount", schema)


def test_correction_must_change_the_plan(monkeypatch):
    srv = make_source("hr", hr=A_EMPLOYEES)
    schema = srv.source_schema("hr")
    old = plan(metric="Emp ID", aggregation="count")
    conv = Conversation(schemas={"hr": schema}, last_plan=dict(old), recent_plans=[{"question": "employees kitne", "plan": dict(old)}])
    seen = []

    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx):
            seen.append(ctx.get("correction_of") is not None or ctx.get("failed_attempt") is not None)
            return dict(old) if len(seen) == 1 else plan(metric="Salary")        # first: identical (must be rejected), then corrected
        def format_result(self, q, p, r, feedback=None): return {"answer": "Total salary hai."}
    monkeypatch.setattr(analyst, "OpenRouterAI", AI)
    r = analyst.answer(conv, "galat hai, salary chahiye", "k", "m", LocalTools(srv))
    assert seen[0] and len(seen) == 2 and conv.last_plan["metric"] == "Salary" and "2,87,000" in r.text


def test_plan_diff():
    assert plan_diff({"metric": "A", "group_by": []}, {"metric": "B", "group_by": []}) == {"metric": ("A", "B")}
    assert plan_diff({"metric": "A"}, {"metric": "A"}) == {}


# ---------------------------------------------------------------- grounding
def test_wrong_row_as_total_is_rejected():
    res = enrich_result({"rows": [{"period": "2026-04", "value": 18408445.35}, {"period": "2026-05", "value": 16843710.16}], "metric": "Revenue"}, {"date_grain": "month"})
    assert analyst._missing_total("Total ₹1,84,08,445.35 hai.", "last 12 months ka total", {"date_grain": "month"}, res) == pytest.approx(35252155.51)


def test_small_numbers_are_checked():
    assert unsupported_numbers("Aapke 7 departments hain", {"rows": [{"Department": "IT", "value": 1}, {"Department": "HR", "value": 2}]}) == [7]
    assert unsupported_numbers("2. HR — 2", {"rows": [{"Department": "IT", "value": 1}, {"Department": "HR", "value": 2}]}) == []


def test_single_value_headline_written_by_code(monkeypatch):
    class AI:
        def format_result(self, q, p, r, feedback=None): return {"answer": "Salary total ₹9,99,999 hai."}   # fabricated
    text = analyst.grounded_answer(AI(), "total salary", {"metric": "Salary", "aggregation": "sum"}, {"rows": [{"value": 287000}], "metric": "Salary"})
    assert "₹2,87,000" in text and "9,99,999" not in text


# ---------------------------------------------------------------- memory scoping
def test_memory_is_scoped_and_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    memory.add_rule("count interns separately", "hr")
    memory.add_plan("q1", {"source_id": "hr", "metric": "Salary"}, validated=False)
    memory.add_plan("q2", {"source_id": "hr", "metric": "Salary"}, validated=True)
    for i in range(7):
        memory.add_plan(f"q{i + 10}", {"source_id": "hr", "metric": "Salary", "i": i}, validated=True)
    ctx_hr, ctx_other = memory.get_context(source_ids=["hr"]), memory.get_context(source_ids=["orders"])
    assert ctx_hr["rules"] == ["count interns separately"] and ctx_other["rules"] == []
    assert len(ctx_hr["successful_plans"]) == memory.PLANS_PER_SOURCE and all(p["question"] != "q1" for p in ctx_hr["successful_plans"])
    assert ctx_other["successful_plans"] == []


def test_legacy_global_rules_are_not_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    (tmp_path / "m.json").write_text(json.dumps({"rules": ["Only respond to sales questions"], "mappings": [], "successful_plans": []}))
    assert memory.get_context(source_ids=["hr"])["rules"] == []


def test_rule_needs_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    srv = make_source("hr", hr=A_EMPLOYEES)
    conv = Conversation(schemas=schemas_of(srv), focus="hr")

    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx): return {"status": "learn", "rule": "Interns are not employees", "source_id": "hr"}
    monkeypatch.setattr(analyst, "OpenRouterAI", AI)
    r = analyst.answer(conv, "remember that interns are not employees", "k", "m", LocalTools(srv))
    assert r.kind == "confirm_rule" and conv.pending_rule["source_id"] == "hr"
    r2 = analyst.answer(conv, "haan", "k", "m", LocalTools(srv))
    assert r2.kind == "learn" and memory.get_context(source_ids=["hr"])["rules"] == ["Interns are not employees"]


# ---------------------------------------------------------------- token compaction
def test_compact_result_and_schema():
    big = {"rows": [{"k": i, "value": i} for i in range(500)], "metric": "x"}
    c = compact_result(big)
    assert len(c["rows"]) == 20 and c["rows_total"] == 500 and c["rows_omitted"] == 480
    detail = {"rows": [{f"c{j}": j for j in range(15)} for _ in range(300)]}
    d = compact_result(detail)
    assert len(d["rows"]) == 10 and len(d["rows"][0]) == 8 and len(d["columns_not_shown"]) == 7
    srv = make_source("hr", hr=A_EMPLOYEES)
    s = compact_schema(srv.source_schema("hr"))
    assert all(len(c["sample_values"]) <= 8 for c in s["columns"]) and "min" not in s["columns"][0]


# ---------------------------------------------------------------- security guards
def test_load_file_outside_allowed_roots_refused(tmp_path):
    srv = MCPServer()
    with pytest.raises(ValueError, match="allowed folders"):
        srv.call_tool("load_file", {"source_id": "x", "path": str(pathlib.Path.home() / ".ssh" / "config")})
    p = pathlib.Path(tempfile.gettempdir()) / "ok_generic_test.csv"
    p.write_text("a,b\n1,2\n")
    assert srv.call_tool("load_file", {"source_id": "x", "path": str(p)})["row_count"] == 1


def test_sqlite_outside_allowed_roots_refused():
    from source_loader import read_database
    with pytest.raises(ValueError, match="allowed folders"):
        read_database(f"sqlite:///{pathlib.Path.home()}/Library/Application Support/x.db")


def test_data_overview_and_how_computed_questions(monkeypatch):
    srv = make_source("wb", wb={"Attendance": E_ATTENDANCE, "Salaries": A_EMPLOYEES})
    conv = Conversation(schemas=schemas_of(srv))
    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx): raise AssertionError("planner must not be called for an overview question")
    monkeypatch.setattr(analyst, "OpenRouterAI", AI)
    r = analyst.answer(conv, "hi isme kya data hai", "k", "m", LocalTools(srv))
    assert r.kind == "overview" and "Attendance" in r.text and "Salaries" in r.text and "Salary" in r.text and "5 rows" in r.text
    # "kaise lagaya" after an answer → re-check explanation of the previous plan, no planner call
    conv.last_plan = {"status": "execute", "mode": "data", "source_id": "wb", "sheet_name": "Salaries", "operation": "aggregate", "metric": "Salary", "aggregation": "sum",
                      "date_column": "Joining Date", "date_from": "2026-01-01", "date_to": "2026-12-31"}
    conv.recent_plans = [{"question": "is saal ki salary", "plan": dict(conv.last_plan)}]
    r2 = analyst.answer(conv, "ye current month data apne kise lagaya hai ?", "k", "m", LocalTools(srv))
    assert "Salaries" in r2.text and "Joining Date" in r2.text and "01-01-2026" in r2.text


def test_missing_filter_column_hints_where_value_lives():
    srv = make_source("hr", hr=A_EMPLOYEES)
    with pytest.raises(ValueError, match='column Employee'):
        srv.call_tool("aggregate_source", {"source_id": "hr", "metric": "Salary", "filters": [{"column": "Staff", "value": "Ravi"}]})


def test_planner_json_repair(monkeypatch):
    from openrouter import OpenRouterAI
    ai = OpenRouterAI("k", "m")
    replies = iter(['{"status":"execute","mode":"data"', '{"status":"execute","mode":"data"}'])
    monkeypatch.setattr(ai, "_call", lambda msgs, kind="plan", model=None: next(replies))
    assert ai.plan("q", {})["status"] == "execute"
    replies = iter(["{bad", "{still bad"])
    with pytest.raises(ValueError, match="valid JSON"):
        ai.plan("q", {})
