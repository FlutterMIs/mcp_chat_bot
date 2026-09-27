"""Response policy: one canonical FinalResponse per question; charts/tables/cards only where they help;
internal agent material never reaches the normal UI."""
import pandas as pd

from analyst import Reply
from response import DETAIL_MAX_ROWS, finalize

SERIES = pd.DataFrame({"period": [f"2026-{m:02d}" for m in range(1, 13)], "Customer Count": range(12), "Sales Amount": [100.0 * m for m in range(12)]})
RANK = pd.DataFrame({"CUSTOMER NAME": list("ABCDE"), "AMOUNT": [500.0, 400, 300, 200, 100]})
DETAIL = pd.DataFrame({"CUSTOMER NAME": ["A", "B"] * 80, "ITEM": ["x", "y"] * 80, "QTY": [1, 2] * 80, "AMOUNT": [10.0, 20] * 80})


def test_scalar_answer_has_no_table_or_chart():
    fr = finalize(Reply("Total sales: ₹14,85,907", df=pd.DataFrame({"value": [1485907.0]}), value=1485907.0, metric="AMOUNT", plan={"title": "Total AMOUNT", "operation": "aggregate"}), "total sales kitni hai?")
    assert fr.shape == "scalar" and fr.table is None and fr.chart is None
    assert fr.metrics == [{"label": "Total AMOUNT", "value": 1485907.0, "note": None}]


def test_count_answer_is_concise():
    fr = finalize(Reply("There are 33 unique item categories.", value=33, metric="ITEM CATEGORY", plan={"operation": "aggregate", "aggregation": "count_distinct"}), "ITEM CATEGORY kitna hai?")
    assert fr.shape == "scalar" and fr.table is None and fr.chart is None and fr.metrics[0]["value"] == 33


def test_time_series_gets_chronological_chart_and_cards():
    cards = [{"label": "Customer Count", "value": 66, "note": "unique"}, {"label": "Sales Amount", "value": 6600.0, "note": "total"}, {"label": "Customer Count", "value": 66, "note": "dup"}]
    fr = finalize(Reply("Month-wise …", df=SERIES, chart={"type": "line", "x": "period", "y": "Customer Count"}, cards=cards, drillable="period", plan={"operation": "multi_metric"}), "last 12 months …")
    assert fr.shape == "series" and fr.chart == {"type": "line", "x": "period", "y": "Customer Count"} and fr.table is SERIES and fr.drilldown == "period"
    assert [c["label"] for c in fr.metrics] == ["Customer Count", "Sales Amount"]           # no duplicate card


def test_ranking_gets_bar_chart_no_cards():
    fr = finalize(Reply("Top 5 …", df=RANK, chart={"type": "bar", "x": "CUSTOMER NAME", "y": "AMOUNT"}, cards=[{"label": "AMOUNT", "value": 1500.0, "note": "total"}], plan={"operation": "aggregate", "group_by": ["CUSTOMER NAME"], "top_n": 5}), "top 5 customers")
    assert fr.shape == "ranking" and fr.chart["type"] == "bar" and fr.table is RANK and fr.metrics == []


def test_detail_rows_table_only_and_capped():
    fr = finalize(Reply("August details", df=DETAIL, chart={"type": "bar", "x": "CUSTOMER NAME", "y": "AMOUNT"}, plan={"operation": "query"}), "August details")
    assert fr.shape == "detail" and fr.chart is None and fr.metrics == [] and len(fr.table) == DETAIL_MAX_ROWS and "160" in fr.table_note


def test_chart_only_when_asked_for_detail_or_big_breakdown():
    big = pd.DataFrame({"CITY": [f"c{i}" for i in range(60)], "AMOUNT": range(60)})
    assert finalize(Reply("…", df=big, plan={"operation": "aggregate", "group_by": ["CITY"]}), "city wise sales").chart is None
    assert finalize(Reply("…", df=big, plan={"operation": "aggregate", "group_by": ["CITY"]}), "city wise sales ka graph").chart is not None
    assert finalize(Reply("…", df=DETAIL, plan={"operation": "query"}, want_chart=True), "details").chart is not None


def test_clarification_and_chat_render_text_only():
    fr = finalize(Reply("❓ Kaunsa?", kind="clarify", options=["A", "B"]), "top 5")
    assert fr.shape == "text" and fr.table is None and fr.chart is None and fr.options == ["A", "B"]
    fr = finalize(Reply("Main data questions mein help karta hoon…", kind="text"), "what can you help me with?")
    assert fr.shape == "text" and not fr.metrics and fr.table is None


def test_internal_material_only_in_debug():
    r = Reply("Top 5 …", df=RANK, trace=["aggregate_data(...)", "rows_matched=253"], tool_calls=[{"name": "aggregate_data"}], plan={"operation": "aggregate", "group_by": ["CUSTOMER NAME"]})
    assert finalize(r, "top 5").debug == {}
    assert finalize(r, "top 5", debug=True).debug["trace"][1] == "rows_matched=253"
    assert "rows_matched" not in finalize(r, "top 5").answer


def test_one_message_renders_one_table_and_one_chart_in_ui(monkeypatch):
    import os
    from streamlit.testing.v1 import AppTest
    from backend import db
    monkeypatch.setenv("APP_AUTH_MODE", "none")                                                # single-user local mode: no login screen
    db.configure("sqlite://")
    at = AppTest.from_file(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), default_timeout=60)
    fr = finalize(Reply("Month-wise …", df=SERIES, chart={"type": "line", "x": "period", "y": "Customer Count"}, value=66.0, cards=[{"label": "Customer Count", "value": 66, "note": ""}, {"label": "Sales Amount", "value": 6600.0, "note": ""}], drillable="period", plan={"operation": "multi_metric"}), "last 12 months")
    m = {"id": "a1", "role": "assistant", "content": fr.answer, "question": "q", "df": fr.table, "debug": {"trace": ["secret trace"]},
         "final_response": {"shape": fr.shape, "chart": fr.chart, "metrics": fr.metrics, "drilldown": fr.drilldown}}
    at.session_state.messages = [{"id": "u1", "role": "user", "content": "q"}, m]
    at.run()
    assert not at.exception, at.exception
    assert len(at.dataframe) == 1 and len(at.get("vega_lite_chart")) == 1 and len(at.metric) == 2
    assert not [e for e in at.expander if "Debug" in e.label]                                   # trace hidden without debug mode
    scalar = finalize(Reply("Total: ₹1,000", value=1000.0, metric="AMOUNT", plan={"title": "Total AMOUNT"}), "total")
    at.session_state.messages = [{"id": "u1", "role": "user", "content": "q"}, {"id": "a2", "role": "assistant", "content": scalar.answer, "question": "q",
                                                                                    "final_response": {"shape": "scalar", "metrics": scalar.metrics}}]
    at.run()
    assert not at.exception and len(at.dataframe) == 0 and len(at.get("vega_lite_chart")) == 0 and len(at.metric) == 1


def test_demo_conversation_shapes(monkeypatch, tmp_path):
    """The master-prompt demo: every turn continues the context and renders only what helps."""
    import memory, analyst
    from analyst import Conversation, LocalTools
    from test_understanding import ORDERS, NoLLM, date
    from test_generic import make_source, schemas_of
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    srv = make_source("orders", orders=ORDERS); t = LocalTools(srv); c = Conversation(schemas=schemas_of(srv))
    seen = []
    for q, shape in [("last 12 months sales dikhao", "series"), ("customer count bhi", "series"), ("items bhi", "series"),
                     ("March ka detail dikhao", "scalar"), ("party wise", "ranking"), ("top 2", "ranking")]:
        fr = finalize(analyst.answer(c, q, "k", "m", t), q)
        seen.append((q, fr.shape, None if fr.table is None else list(fr.table.columns), bool(fr.chart)))
        assert fr.shape == shape, seen
    assert seen[2][2] == ["period", "Sales Amount", "Customer Count", "Items Count"] and seen[2][3]      # 3 measures kept, chart shown
    assert seen[3][2] is None and not seen[3][3]                                                         # March detail: numbers only, no chart
    assert seen[4][2][0] == "Party Name" and c.state["period"]["from"] == "2026-03-01"                 # March context kept
    assert seen[5][3] and len(finalize(analyst.answer(c, "top 2", "k", "m", t), "top 2").table) == 2
