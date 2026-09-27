"""Bounded agent loop with a scripted LLM (no network): multi-source join, limits, error recovery, grounding, allowlist."""
import json

import pandas as pd
import pytest

import agent as agentmod
from agent import Agent, classify, tool_definitions
from analyst import Conversation, LocalTools
from mcp_server import MCPServer
from resultstore import ResultStore, safe_eval
from test_generic import B_ORDERS, F_TARGETS, A_EMPLOYEES, E_ATTENDANCE, make_source, schemas_of


class ScriptedAI:
    """Plays back a list of assistant turns: each is a list of (tool_name, args) or a plain-text string."""

    def __init__(self, turns):
        self.turns, self.messages_seen = list(turns), []

    def chat_with_tools(self, messages, tools, tool_choice="auto"):
        self.messages_seen.append([m.get("role") for m in messages])
        if not self.turns:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "finish", "arguments": json.dumps({"answer": "done", "result_ids": []})}}]}
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return {"role": "assistant", "content": turn}
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(turn)]}

    def last_tool_observation(self, messages):
        return next(json.loads(m["content"]) for m in reversed(messages) if m.get("role") == "tool")


def orders_and_targets():
    srv = make_source("x", orders=B_ORDERS, targets=F_TARGETS)
    return srv, Conversation(schemas=schemas_of(srv))


# ---------------------------------------------------------------- multi-source join, all numbers bound to results
def test_customers_exceeding_target_via_join_and_calculate():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([
        [("aggregate_data", {"source_id": "orders", "metric": "Revenue", "group_by": ["Customer"]})],
        [("query_data", {"source_id": "targets"})],
        [("join_results", {"left_result_id": "r1", "right_result_id": "r2", "left_key": "Customer", "right_key": "Customer", "join_type": "outer"})],
        [("calculate", {"result_id": "r3", "expression": "Revenue / Target * 100", "new_column": "achievement_pct"})],
        [("finish", {"answer": "Acme ne target paar kiya: revenue 3400 vs target 3000 (113.33%). Zed 1750 vs 2000, Bolt 300 vs 500. Nova ka koi order nahi.", "result_ids": ["r4"], "primary_result_id": "r4"})],
    ])
    a = Agent(ai, LocalTools(srv), conv, "which customers exceeded their target?")
    r = a.run()
    assert r.kind == "agent" and "Acme" in r.text and len(a.trace) == 4
    df = r.df
    assert set(df["Customer"]) == {"Acme", "Zed", "Bolt", "Nova"}
    assert float(df.loc[df["Customer"] == "Acme", "achievement_pct"].iloc[0]) == pytest.approx(113.3333, abs=0.01)
    joined = a.store.items["r3"]["result"]
    assert joined["matched"] == 3 and joined["unmatched_right"] == 1 and joined["unmatched_right_sample"] == ["Nova"]


def test_fabricated_number_in_finish_is_rejected_then_partial():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([
        [("aggregate_data", {"source_id": "orders", "metric": "Revenue"})],
        [("finish", {"answer": "Total revenue 9999 hai.", "result_ids": ["r1"]})],      # not in any result
        [("finish", {"answer": "Total revenue 8888 hai.", "result_ids": ["r1"]})],      # again wrong → stop
    ])
    r = Agent(ai, LocalTools(srv), conv, "total revenue aur kuch").run()
    assert r.kind == "agent_partial" and "5,450" in r.text and "9999" not in r.text


def test_grounded_finish_accepts_result_numbers():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([[("aggregate_data", {"source_id": "orders", "metric": "Revenue"})],
                     [("finish", {"answer": "Total revenue ₹5,450 hai.", "result_ids": ["r1"]})]])
    r = Agent(ai, LocalTools(srv), conv, "total revenue").run()
    assert r.kind == "agent" and r.text == "Total revenue ₹5,450 hai."


def test_step_limit_returns_partial_without_guessing(monkeypatch):
    srv, conv = orders_and_targets()
    turns = [[("aggregate_data", {"source_id": "orders", "metric": "Revenue", "group_by": ["Customer"], "top_n": i + 1})] for i in range(10)]
    ai = ScriptedAI(turns)
    a = Agent(ai, LocalTools(srv), conv, "keep going")
    r = a.run()
    assert r.kind == "agent_partial" and "step limit" in r.text and len(a.trace) == agentmod.MAX_STEPS


def test_tool_call_limit():
    srv, conv = orders_and_targets()
    many = [("aggregate_data", {"source_id": "orders", "metric": "Revenue", "top_n": i + 1, "group_by": ["Customer"]}) for i in range(agentmod.MAX_TOOL_CALLS + 2)]
    a = Agent(ScriptedAI([many]), LocalTools(srv), conv, "x")
    r = a.run()
    assert r.kind == "agent_partial" and "tool-call limit" in r.text and a.calls == agentmod.MAX_TOOL_CALLS + 1


def test_time_limit(monkeypatch):
    srv, conv = orders_and_targets()
    monkeypatch.setattr(agentmod, "MAX_SECONDS", -1)
    r = Agent(ScriptedAI([[("aggregate_data", {"source_id": "orders", "metric": "Revenue"})]]), LocalTools(srv), conv, "x").run()
    assert r.kind == "agent_failed" and "time limit" in r.text


def test_tool_error_is_observed_and_recovered():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([
        [("aggregate_data", {"source_id": "orders", "metric": "Turnover"})],              # no such column
        [("aggregate_data", {"source_id": "orders", "metric": "Revenue"})],
        [("finish", {"answer": "Revenue total 5450.", "result_ids": ["r1"]})],
    ])
    a = Agent(ai, LocalTools(srv), conv, "revenue total")
    r = a.run()
    assert r.kind == "agent" and a.trace[0].endswith(a.trace[0]) and "✗" in a.trace[0] and "Metric column" in a.trace[0]


def test_repeated_identical_call_is_refused():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([[("aggregate_data", {"source_id": "orders", "metric": "Revenue"})],
                     [("aggregate_data", {"source_id": "orders", "metric": "Revenue"})],
                     [("finish", {"answer": "5450", "result_ids": ["r1"]})]])
    a = Agent(ai, LocalTools(srv), conv, "x")
    a.run()
    assert len(a.store.order) == 1


def test_validation_guards_apply_inside_agent():
    srv = make_source("hr", hr=A_EMPLOYEES)
    conv = Conversation(schemas=schemas_of(srv))
    ai = ScriptedAI([[("aggregate_data", {"source_id": "hr", "metric": "Emp ID"})], [("finish", {"answer": "ok", "result_ids": []})]])
    a = Agent(ai, LocalTools(srv), conv, "total salary")
    a.run()
    assert "identifier" in a.trace[0]


def test_dangerous_tools_are_not_exposed():
    names = {t["function"]["name"] for t in tool_definitions()}
    assert not names & {"load_file", "register_web", "register_database", "register_google_sheet", "aggregate_data_sql", "get_database_schema"}
    srv, conv = orders_and_targets()
    a = Agent(ScriptedAI([[("load_file", {"source_id": "x", "path": "/etc/passwd"})], "done"]), LocalTools(srv), conv, "x")
    a.run()
    assert "not available" in a.trace[0]


def test_compare_periods_tool():
    srv, conv = orders_and_targets()
    ai = ScriptedAI([[("compare_periods", {"source_id": "orders", "metric": "Revenue", "date_column": "Order Date", "period_a_from": "2026-01-01", "period_a_to": "2026-01-31",
                                            "period_b_from": "2026-02-01", "period_b_to": "2026-02-28"})],
                     [("finish", {"answer": "Jan 2000 se Feb 1800: change -200 (-10%).", "result_ids": ["r1"]})]])
    a = Agent(ai, LocalTools(srv), conv, "compare january with february revenue")
    r = a.run()
    row = a.store.items["r1"]["result"]["rows"][0]
    assert (row["period_a"], row["period_b"], row["change"], row["change_pct"]) == (2000, 1800, -200, -10.0) and r.kind == "agent"


def test_context_trim_keeps_transcript_under_budget(monkeypatch):
    srv, conv = orders_and_targets()
    monkeypatch.setattr(agentmod, "MAX_CONTEXT_CHARS", 3000)
    turns = [[("query_data", {"source_id": "orders", "limit": 200})] for _ in range(4)] + [[("finish", {"answer": "ok", "result_ids": ["r1"]})]]
    # identical calls would be refused, so vary the limit
    turns = [[("query_data", {"source_id": "orders", "limit": 200 - i})] for i in range(4)] + [[("finish", {"answer": "ok", "result_ids": ["r1"]})]]
    a = Agent(ScriptedAI(turns), LocalTools(srv), conv, "rows")
    msgs_holder = {}
    orig = a._trim
    def spy(messages):
        orig(messages); msgs_holder["size"] = sum(len(json.dumps(m, default=str)) for m in messages)
    a._trim = spy
    a.run()
    assert msgs_holder["size"] < 3000 + 6500      # last observation may still be full size; older ones are compressed


# ---------------------------------------------------------------- classification
def test_classify_routes_only_complex_questions():
    srv, conv = orders_and_targets()
    s = conv.schemas
    assert classify("total revenue", s) == "simple"
    assert classify("top 3 customers by revenue", s) == "simple"
    assert classify("which customers exceeded their target", s) == "complex"          # words from both tables
    assert classify("revenue kyun gira?", s) == "complex"
    assert classify("total revenue", s, direct_failed=True) == "complex"
    one = Conversation(schemas={"hr": MCPServer().register_file("hr", {"kind": "table", "name": "hr", "df": A_EMPLOYEES})}).schemas
    assert classify("why is salary high", one) == "simple"     # a single table has nothing to join


# ---------------------------------------------------------------- result store + safe calculator
def test_safe_eval_rejects_code():
    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    assert list(safe_eval("a + b * 2", df)) == [7, 10]
    assert list(safe_eval("share(a)", df)) == pytest.approx([33.3333, 66.6667], abs=0.01)
    assert safe_eval("sum(b)", df) == 7
    for bad in ["__import__('os').system('x')", "a.__class__", "open('x')", "a if b else 1", "[1,2]", "lambda: 1"]:
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval(bad, df)


def test_join_reports_unmatched_and_normalises_keys():
    st = ResultStore()
    l = st.put({"rows": [{"Item": "Code-1053, HINGE 3D", "value": 10}, {"Item": "Widget", "value": 5}]}, "l")
    r = st.put({"rows": [{"ITEM NAME": "code-1053,  hinge 3d", "CLOSING STOCK": 40}, {"ITEM NAME": "Other", "CLOSING STOCK": 1}]}, "r")
    j = st.join(l, r, "Item", "ITEM NAME", "left")
    res = st.items[j]["result"]
    assert res["matched"] == 1 and res["unmatched_left"] == 1 and res["unmatched_right"] == 1 and len(res["rows"]) == 2
    with pytest.raises(ValueError):
        st.join(l, r, "Item", "Nope", "inner")


def test_join_refuses_truncated_lookup_but_allows_topn_driver():
    st = ResultStore()
    top = st.put({"rows": [{"Customer": "Acme", "value": 10}], "metric": "Revenue", "aggregation": "sum", "groups_total": 3}, "top1")
    partial = st.put({"rows": [{"Customer": "Acme", "Target": 5}], "rows_matched": 4}, "targets(limit 1)")
    full = st.put({"rows": [{"Customer": "Acme", "Target": 5}, {"Customer": "Zed", "Target": 2}], "rows_matched": 2}, "targets")
    with pytest.raises(ValueError, match="lookup"):
        st.join(top, partial, "Customer", "Customer", "left")
    j = st.join(top, full, "Customer", "Customer", "left")          # top-N driver on the left is fine
    assert st.items[j]["result"]["matched"] == 1
    j2 = st.join(full, top, "Customer", "Customer", "inner")          # inner join: one complete side is enough
    assert st.items[j2]["result"]["matched"] == 1
    with pytest.raises(ValueError, match="lookup"):
        st.join(top, top, "Customer", "Customer", "outer")           # outer needs both sides complete


def test_malformed_filters_give_clear_error():
    srv = MCPServer()
    srv.register_file("o", {"kind": "table", "name": "o", "df": B_ORDERS})
    with pytest.raises(ValueError, match="each filter must be an object"):
        srv.call_tool("aggregate_source", {"source_id": "o", "metric": "Revenue", "filters": [["Customer", "Acme"]]})
