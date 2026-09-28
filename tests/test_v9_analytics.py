"""V9 natural-language analytics engine — the spec's 25 regression items, all offline (NoLLM):
top-N global vs per period / per group, multi-metric ranking by the money column, business vocabulary ("seller",
"items sold", "customer count", "transaction count"), smart clarification, follow-up memory (sequences A/B/C),
comparisons with deterministic % change, share %, Grand Total vs Displayed Top-N Total, Sep-26 display labels,
chart validation, narrative/table consistency, RAG-only / MCP-only / MCP+RAG routing, and the golden question."""
import re
from datetime import date as _date

import pandas as pd
import pytest

import analyst
import memory
import prompt_builder
import rag
from analyst import Conversation, LocalTools, Reply
from grounding import _numbers
from mcp_server import MCPServer
from response import chart_check, display_table, finalize, period_label
from test_generic import make_source, schemas_of
from test_v8_features import POLICY, planner

TODAY = _date(2026, 9, 27)


class date(_date):
    @classmethod
    def today(cls):
        return TODAY


class NoLLM:
    def __init__(self, *a): pass
    def plan(self, *a, **k): raise AssertionError("planner must not be called")
    def format_result(self, *a, **k): raise AssertionError("wording model must not be called")
    def answer_text(self, *a, **k): raise AssertionError("text model must not be called")


PEOPLE = ["DEEPAK GOYAL", "AKHILESH JI", "KARM VEER", "Niraj Sharma", "Pranjali Ji"]
CUSTS = ["Acme", "Zed", "Bolt", "Nova", "Kilo", "Pranjal Traders"]
STATES = ["Delhi", "Haryana", "UP"]
CATS = ["Electronics", "Furniture", "Clothing"]
rows = []
for m in (7, 8, 9):
    for i in range(15):
        rows.append([f"2026-{m:02d}-{(i % 27) + 1:02d}", PEOPLE[i % 5], CUSTS[i % 6], STATES[i % 3], CATS[i % 3], f"Item{i % 6}", 1 + i % 4, 1000 * (i + 1) * (m - 6), f"INV{m}{i:02d}"])
SALES = pd.DataFrame(rows, columns=["VOUCHER DATE", "SALES PERSON", "CUSTOMER NAME", "STATE", "CATEGORY", "ITEM NAME", "QTY", "AMOUNT", "INVOICE NO"])
INVENTORY = pd.DataFrame({"ITEM": ["Laptop", "Chair"], "CLOSING STOCK": [40, 150], "AMOUNT": [1200000, 450000]})
TOTAL = float(SALES["AMOUNT"].sum())


def top_per_month(n, by="AMOUNT"):
    g = SALES.assign(period=SALES["VOUCHER DATE"].str[:7]).groupby(["period", "SALES PERSON"], as_index=False)[[by, "QTY"]].sum()
    return g.sort_values(["period", by], ascending=[True, False]).groupby("period").head(n).reset_index(drop=True)


@pytest.fixture
def biz(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    prompt_builder.set_instructions(None)
    srv = make_source("biz", biz={"INVENTORY": INVENTORY, "SALES": SALES})
    yield Conversation(schemas=schemas_of(srv)), LocalTools(srv), srv


def ask(c, t, q):
    r = analyst.answer(c, q, "k", "m", t)
    return r, finalize(r, q)


# ---------------------------------------------------------------- 1-3 basics stay exact
def test_01_sales_total_02_category_wise_03_closing_stock(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "bhai sales ka total bata")
    assert fr.shape == "scalar" and fr.value == TOTAL
    r, fr = ask(Conversation(schemas=c.schemas), t, "sales amount category wise batao")
    assert r.plan["group_by"] == ["CATEGORY"] and r.plan["metric"] == "AMOUNT" and fr.totals["label"] == "Grand Total" and fr.totals["all"]["AMOUNT"] == TOTAL
    r, fr = ask(Conversation(schemas=c.schemas), t, "inventory closing stock batao")
    assert r.plan["sheet_name"] == "INVENTORY" and r.plan["metric"] == "CLOSING STOCK" and fr.value == 190.0


# ---------------------------------------------------------------- 4-8 rankings: global vs per period vs per group
def test_04_top_seller_is_one_row_no_clarification(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "top seller batao")
    assert fr.kind == "answer" and r.plan["group_by"] == ["SALES PERSON"] and r.plan["top_n"] == 1 and r.plan["rank_scope"] is None
    best = SALES.groupby("SALES PERSON")["AMOUNT"].sum().idxmax()
    assert fr.table.iloc[0]["SALES PERSON"] == best and len(fr.table) == 1


def test_05_month_wise_top_seller_is_one_winner_per_month(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "month wise top seller batao")
    assert r.plan["rank_scope"] == "period" and r.plan["top_n"] == 1
    exp = top_per_month(1)
    assert list(fr.table["period"]) == ["2026-07", "2026-08", "2026-09"] and list(fr.table["SALES PERSON"]) == list(exp["SALES PERSON"])
    assert list(fr.table["AMOUNT"]) == list(exp["AMOUNT"])


def test_06_month_wise_top_5_seller_never_sort_all_and_head(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "har month ke top 3 seller")
    assert r.plan["rank_scope"] == "period" and r.plan["top_n"] == 3
    per = fr.table.groupby("period").size()
    assert per.max() <= 3 and set(per.index) == {"2026-07", "2026-08", "2026-09"} and list(fr.table["period"]) == sorted(fr.table["period"])
    exp = top_per_month(3)
    assert list(fr.table["AMOUNT"]) == list(exp["AMOUNT"])
    # a global "sort all + head(3)" would give three September rows — that is exactly what must not happen
    assert not (fr.table["period"] == "2026-09").all()


def test_07_state_wise_top_3_customer_per_state(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "har state ke top 3 customer")
    assert r.plan["group_by"] == ["STATE", "CUSTOMER NAME"] and r.plan["rank_scope"] == "STATE" and r.plan["top_n"] == 3
    assert fr.table.groupby("STATE").size().max() <= 3 and set(fr.table["STATE"]) == set(STATES)
    exp = SALES.groupby(["STATE", "CUSTOMER NAME"], as_index=False)["AMOUNT"].sum().sort_values(["STATE", "AMOUNT"], ascending=[True, False]).groupby("STATE").head(3)
    assert list(fr.table["AMOUNT"]) == list(exp["AMOUNT"])
    r2, fr2 = ask(Conversation(schemas=c.schemas), t, "state wise top customer")
    assert r2.plan["rank_scope"] == "STATE" and r2.plan["top_n"] == 1 and len(fr2.table) == 3 and r2.plan["date_grain"] is None


def test_08_bottom_5_customer_and_hinglish_lowest(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "bottom 5 customer")
    by = SALES.groupby("CUSTOMER NAME")["AMOUNT"].sum().sort_values()
    assert r.plan["sort"] == "asc" and list(fr.table["AMOUNT"]) == list(by.head(5))
    r, fr = ask(Conversation(schemas=c.schemas), t, "sabse kam sale wale 5 customer")
    assert r.plan["sort"] == "asc" and list(fr.table["CUSTOMER NAME"]) == list(by.head(5).index)


# ---------------------------------------------------------------- 9-10 multi-metric, ranked by the money column
def test_09_top_seller_with_amount_and_qty(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "top seller ki sale amount aur items sold batao")
    cols = [m["column"] for m in r.plan["metrics"]]
    assert cols == ["AMOUNT", "QTY"] and r.plan["rank_by"] == "AMOUNT" and len(fr.table) == 1
    best = SALES.groupby("SALES PERSON")[["AMOUNT", "QTY"]].sum().sort_values("AMOUNT", ascending=False).iloc[0]
    assert fr.table.iloc[0]["Sales Amount"] == best["AMOUNT"] and fr.table.iloc[0]["Items Count"] == best["QTY"]


def test_10_golden_question_month_wise_top_seller_amount_and_items(biz):
    c, t, _ = biz
    q = "bhai mujhe kuchh esa chaiye ki date month wise sir mujhe usme usme top saler usak sale amount and fir mujhe usak total items sale count"
    r, fr = ask(c, t, q)
    assert fr.kind == "answer" and not fr.options, fr.answer
    p = r.plan
    assert p["date_grain"] == "month" and p["group_by"] == ["SALES PERSON"] and p["top_n"] == 1 and p["rank_scope"] == "period" and p["rank_by"] == "AMOUNT"
    assert [m["column"] for m in p["metrics"]] == ["AMOUNT", "QTY"]
    exp = top_per_month(1)
    assert list(fr.table["period"]) == ["2026-07", "2026-08", "2026-09"] and list(fr.table["SALES PERSON"]) == list(exp["SALES PERSON"])
    assert list(fr.table["Sales Amount"]) == list(exp["AMOUNT"]) and list(fr.table["Items Count"]) == list(exp["QTY"])
    shown = display_table(fr.table, fr.totals)
    assert list(shown["period"])[:3] == ["Jul-26", "Aug-26", "Sep-26"] and shown["Sales Amount"][0].startswith("₹") and not shown["Items Count"][0].startswith("₹")
    assert shown.iloc[-1]["period"] == "Displayed Top 1 Total"
    assert fr.chart == {"type": "bar", "x": "period", "y": "Sales Amount", "color": "SALES PERSON"}
    # narrative numbers all come from the table / totals
    allowed = {round(float(v), 2) for v in list(fr.table["Sales Amount"]) + list(fr.table["Items Count"]) + list(fr.totals["all"].values()) + list(fr.totals["displayed"].values())} | {1, 2, 3}
    assert all(any(abs(n - a) < 0.01 for a in allowed) or n in (2026, 26) or n < 100 for n in _numbers(fr.answer)), fr.answer


# ---------------------------------------------------------------- 11-12 count semantics
def test_11_customer_count_12_transaction_count(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "customer count")
    assert fr.value == float(SALES["CUSTOMER NAME"].nunique()) and r.plan["aggregation"] == "count_distinct"
    r, fr = ask(Conversation(schemas=c.schemas), t, "number of customers")
    assert fr.value == float(SALES["CUSTOMER NAME"].nunique())
    r, fr = ask(Conversation(schemas=c.schemas), t, "transaction count")
    assert fr.value == float(SALES["INVOICE NO"].nunique()) and r.plan["metrics"][0]["label"] == "Transaction Count"
    from semantics import resolve_measures
    from source_loader import column_info
    m = resolve_measures([{"kind": "count", "entity": "order", "column_hint": None}], column_info(SALES.drop(columns=["INVOICE NO"])))
    assert m[0]["aggregation"] == "count" and m[0]["label"] == "Transaction Count"     # no id column → row count, never SUM(QTY)


# ---------------------------------------------------------------- 13-14 typos and date ambiguity
def test_13_typo_seller_and_entity(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "har month ka no 1 saler")
    assert r.plan["group_by"] == ["SALES PERSON"] and r.plan["top_n"] == 1 and r.plan["rank_scope"] == "period"
    analyst.OpenRouterAI = planner({"status": "execute", "mode": "data", "source_id": "biz", "sheet_name": "SALES", "operation": "aggregate", "metric": "AMOUNT", "aggregation": "sum",
                                    "filters": [{"column": "SALES PERSON", "op": "eq", "value": "Pranjli ji"}]})
    r, fr = ask(Conversation(schemas=c.schemas), t, "Pranjli ji ki sales")
    assert fr.value == float(SALES[SALES["SALES PERSON"] == "Pranjali Ji"]["AMOUNT"].sum())


def test_14_two_date_columns_asked_once_and_remembered(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    two = SALES.assign(TIMESTAMP=SALES["VOUCHER DATE"])
    srv = make_source("s", s={"SALES": two})
    c, t = Conversation(schemas=schemas_of(srv)), LocalTools(srv)
    r, fr = ask(c, t, "month wise top seller")
    assert fr.kind == "clarify" and set(fr.options) == {"TIMESTAMP", "VOUCHER DATE"}
    r, fr = ask(c, t, "VOUCHER DATE")
    assert fr.kind == "answer" and r.plan["date_column"] == "VOUCHER DATE" and r.plan["rank_scope"] == "period"
    r, fr = ask(c, t, "September ki sales")                     # not asked again
    assert fr.kind == "answer" and r.plan["date_column"] == "VOUCHER DATE"


# ---------------------------------------------------------------- 15-16 follow-up memory (sequences A, B, C)
def test_15_16_sequence_a_top_seller_amount_items(biz):
    c, t, _ = biz
    r1, f1 = ask(c, t, "top seller batao")
    r2, f2 = ask(c, t, "amount bhi")
    r3, f3 = ask(c, t, "items bhi")
    assert [m["column"] for m in r2.plan["metrics"]] == ["AMOUNT"] and r2.plan["top_n"] == 1 and r2.plan["group_by"] == ["SALES PERSON"]
    assert [m["column"] for m in r3.plan["metrics"]] == ["AMOUNT", "QTY"] and r3.plan["top_n"] == 1 and f3.table.iloc[0]["SALES PERSON"] == f1.table.iloc[0]["SALES PERSON"]


def test_sequence_b_month_wise_then_top_seller_then_metrics(biz):
    c, t, _ = biz
    r1, f1 = ask(c, t, "month wise sales")
    r2, f2 = ask(c, t, "top seller bhi bata")
    r3, f3 = ask(c, t, "uska amount aur quantity")
    assert f1.shape == "series" and f2.kind == "answer" and not f2.options
    assert r2.plan["date_grain"] == "month" and r2.plan["group_by"] == ["SALES PERSON"] and r2.plan["top_n"] == 1 and r2.plan["rank_scope"] == "period"
    assert [m["column"] for m in r3.plan["metrics"]] == ["AMOUNT", "QTY"] and r3.plan["rank_scope"] == "period" and list(f3.table["SALES PERSON"]) == list(f2.table["SALES PERSON"])


def test_sequence_c_september_then_top_5_customer_then_amount(biz):
    c, t, _ = biz
    r1, f1 = ask(c, t, "September sales")
    r2, f2 = ask(c, t, "top 5 customer")
    r3, f3 = ask(c, t, "amount ke saath")
    sep = SALES[SALES["VOUCHER DATE"].str.startswith("2026-09")]
    assert f1.value == float(sep["AMOUNT"].sum())
    assert r2.plan["date_from"] == "2026-09-01" and r2.plan["top_n"] == 5 and list(f2.table["AMOUNT"]) == list(sep.groupby("CUSTOMER NAME")["AMOUNT"].sum().sort_values(ascending=False).head(5))
    assert r3.plan["date_from"] == "2026-09-01" and [m["column"] for m in r3.plan["metrics"]] == ["AMOUNT"] and len(f3.table) == 5


# ---------------------------------------------------------------- 17-18 pivot and comparison
def test_17_pivot_still_works_and_ranking_is_not_pivoted(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "month wise sales by state")
    assert r.pivot and list(fr.table["period"])[:3] == ["2026-07", "2026-08", "2026-09"]
    r, fr = ask(Conversation(schemas=c.schemas), t, "month wise top seller")
    assert not r.pivot


def test_18_comparison_difference_and_percent_are_deterministic(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "this month vs last month sales")
    sep = float(SALES[SALES["VOUCHER DATE"].str.startswith("2026-09")]["AMOUNT"].sum())
    aug = float(SALES[SALES["VOUCHER DATE"].str.startswith("2026-08")]["AMOUNT"].sum())
    vals = {m["label"]: m["value"] for m in fr.metrics}
    assert vals["this month"] == sep and vals["last month"] == aug and vals["Change"] == sep - aug
    assert f"({(sep - aug) / aug * 100:+.1f}%)" in fr.answer
    c2 = Conversation(schemas=c.schemas)
    ask(c2, t, "September sales")
    r, fr = ask(c2, t, "pichhle month se kitna difference hai")
    assert {m["label"]: m["value"] for m in fr.metrics}["Change"] == sep - aug and "September 2026 vs last month" in fr.answer


# ---------------------------------------------------------------- 19-20 grand total vs displayed top-N total
def test_19_20_grand_total_vs_displayed_top_n_total(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "customer wise sales")
    assert fr.totals["label"] == "Grand Total" and fr.totals["all"]["AMOUNT"] == TOTAL and not fr.totals["cut"]
    assert display_table(fr.table, fr.totals).iloc[-1].tolist()[0] == "Grand Total"
    r, fr = ask(Conversation(schemas=c.schemas), t, "top 3 customer")
    shown = float(fr.table["AMOUNT"].sum())
    assert fr.totals["cut"] and fr.totals["label"] == "Displayed Top 3 Total" and fr.totals["displayed"]["AMOUNT"] == shown and fr.totals["all"]["AMOUNT"] == TOTAL and shown < TOTAL
    assert "poore data ka total" in fr.answer and display_table(fr.table, fr.totals).iloc[-1].tolist()[0] == "Displayed Top 3 Total"
    from whatsapp.format import compose
    wa, _ = compose(r)
    assert "*Displayed Top 3 Total:*" in wa
    # planner path (LLM plan with top_n) carries the same distinction from grand_total_all_groups
    analyst.OpenRouterAI = planner({"status": "execute", "mode": "data", "source_id": "biz", "sheet_name": "SALES", "operation": "aggregate", "metric": "AMOUNT", "aggregation": "sum",
                                    "group_by": ["CUSTOMER NAME"], "sort": "desc", "top_n": 1, "filters": [{"column": "STATE", "op": "eq", "value": "Delhi"}]})
    import entity_filter
    monkeypatch_none = lambda *a, **k: None
    orig, orig2 = entity_filter.find_entity_filter, entity_filter.filter_like_spans
    entity_filter.find_entity_filter, entity_filter.filter_like_spans = monkeypatch_none, lambda *a, **k: []   # force the planner path for this check
    try:
        r, fr = ask(Conversation(schemas=c.schemas), t, "Delhi ka top party by amount")     # (Delhi has 2 customers; top 1 is a cut)
    finally:
        entity_filter.find_entity_filter, entity_filter.filter_like_spans = orig, orig2
    delhi = float(SALES[SALES["STATE"] == "Delhi"]["AMOUNT"].sum())
    assert r.plan["operation"] == "aggregate" and fr.totals["cut"] and fr.totals["all"]["AMOUNT"] == delhi and fr.totals["displayed"]["AMOUNT"] == float(fr.table["value"].sum()) < delhi


def test_share_percent_from_all_data_total(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "category wise sales share")
    assert "Share %" in fr.table.columns and abs(fr.table["Share %"].sum() - 100) < 0.05
    assert display_table(fr.table).iloc[0]["Share %"].endswith("%")


# ---------------------------------------------------------------- 21-22 charts and narrative consistency
def test_21_chart_metric_correctness(biz):
    c, t, _ = biz
    r, fr = ask(c, t, "month wise sales")
    assert fr.chart == {"type": "line", "x": "period", "y": "AMOUNT"}
    r, fr = ask(Conversation(schemas=c.schemas), t, "category wise sales")
    assert fr.chart["type"] == "bar" and fr.chart["x"] == "CATEGORY" and fr.chart["y"] == "AMOUNT"
    r, fr = ask(Conversation(schemas=c.schemas), t, "month wise top seller")
    assert fr.chart["x"] == "period" and fr.chart["y"] == "AMOUNT" and fr.chart["color"] == "SALES PERSON"
    df = pd.DataFrame({"period": ["2026-08", "2026-09"], "SALES PERSON": ["A", "B"], "AMOUNT": [1, 2]})
    assert chart_check(df, {"type": "bar", "x": "period", "y": "SALES PERSON"}) is None          # a text field is never a numeric axis
    assert chart_check(df, {"type": "bar", "x": "AMOUNT", "y": "AMOUNT"}) is None
    assert chart_check(df, {"type": "bar", "x": "period", "y": "AMOUNT", "color": "MISSING"}) == {"type": "bar", "x": "period", "y": "AMOUNT"}
    assert period_label("2026-09") == "Sep-26" and period_label("2026-09-05") == "2026-09-05" and period_label("2026") == "2026"


def test_22_narrative_numbers_match_table_and_totals(biz):
    c, t, _ = biz
    for q in ["month wise top 3 seller", "top 5 customer", "state wise top customer", "customer wise sales"]:
        r, fr = ask(Conversation(schemas=c.schemas), t, q)
        cells = {round(float(v), 2) for col in fr.table.columns for v in fr.table[col] if isinstance(v, (int, float)) and not isinstance(v, bool)}
        cells |= {round(float(v), 2) for d in (fr.totals or {}).values() if isinstance(d, dict) for v in d.values()}
        for n in _numbers(re.sub(r"\d{4}-\d{2}(-\d{2})?", " ", fr.answer)):
            assert n < 100 or any(abs(n - a) < 0.01 for a in cells), (q, n, fr.answer)


def test_result_check_rejects_too_many_winners_and_wrong_totals(biz):
    c, t, _ = biz
    from result_check import problems
    df = pd.DataFrame({"period": ["2026-08", "2026-08", "2026-09"], "SALES PERSON": ["A", "B", "C"], "AMOUNT": [1.0, 2.0, 3.0]})
    plan = {"status": "execute", "mode": "data", "operation": "multi_metric", "source_id": "biz", "sheet_name": "SALES", "metric": "AMOUNT", "aggregation": "sum",
            "metrics": [{"column": "AMOUNT", "aggregation": "sum", "label": "AMOUNT"}], "group_by": ["SALES PERSON"], "date_grain": "month", "top_n": 1, "rank_scope": "period"}
    bad = Reply("x", kind="answer", df=df, plan=plan, totals={"all": {"AMOUNT": 6.0}, "displayed": {"AMOUNT": 5.0}, "cut": True, "label": "Displayed Top 1 Total"})
    probs = problems(bad, c.schemas)
    assert any("more than top 1" in p for p in probs) and any("displayed total" in p for p in probs)
    ok = Reply("x", kind="answer", df=df.iloc[[1, 2]], plan=plan, totals={"all": {"AMOUNT": 6.0}, "displayed": {"AMOUNT": 5.0}, "cut": True, "label": "Displayed Top 1 Total"})
    assert problems(ok, c.schemas) == []


# ---------------------------------------------------------------- 23-25 RAG-only / MCP-only / MCP+RAG
def test_23_24_25_rag_only_mcp_only_hybrid(biz):
    c, t, srv = biz
    rag.configure(RAG_ENABLED="true", RAG_TOP_K=3, RAG_INDEX_PATH="")
    try:
        srv.register_file("doc", POLICY)
        c.schemas["doc"] = srv.source_schema("doc")
        from agent import classify
        assert classify("company return policy kya hai", c.schemas) == "simple"                 # → planner text mode → RAG
        r, fr = ask(c, t, "month wise top seller")                                                # MCP only, RAG untouched
        assert fr.kind == "answer" and r.plan["mode"] == "data" and "search_source" not in (r.tool_calls or []) and r.plan["rank_scope"] == "period"
        assert classify("September sales aur return policy ke according kya action hona chahiye?", c.schemas) == "complex"   # MCP + RAG via the agent
        seen = {}
        analyst.OpenRouterAI = planner({"status": "execute", "mode": "text", "source_id": "doc", "operation": "search"},
                                       lambda q, retrieved: seen.setdefault("r", retrieved) and {"answer": "7 days ke andar return ho sakta hai.", "found": True})
        r, fr = ask(Conversation(schemas=c.schemas), t, "company return policy kya hai")
        assert "📄 Source: Return_Policy.pdf, Page 4" in fr.answer and seen["r"]["chunks"][0]["page"] == 4
    finally:
        rag.configure()


# ---------------------------------------------------------------- V9.1 entity filters in code (screenshot: "jsp trader ki merko last 12 mnths ki sale dedo")
def test_entity_filter_resolves_typed_customer_without_the_planner(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "m.json")
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    s2 = SALES.copy()
    s2.loc[s2["CUSTOMER NAME"] == "Acme", "CUSTOMER NAME"] = "JSP TRADERS (HISAR)"
    srv = make_source("s", s={"SALES": s2, "INVENTORY": INVENTORY[["ITEM", "CLOSING STOCK"]]})
    c, t = Conversation(schemas=schemas_of(srv)), LocalTools(srv)
    r, fr = ask(c, t, "jsp trader ki merko last 12 mnths ki sale dedo")
    jsp = s2[s2["CUSTOMER NAME"] == "JSP TRADERS (HISAR)"]
    assert fr.kind == "answer" and fr.shape == "series" and r.plan["filters"] == [{"column": "CUSTOMER NAME", "op": "eq", "value": "JSP TRADERS (HISAR)"}]
    assert len(fr.table) == 12 and list(fr.table["period"])[:2] == ["2025-10", "2025-11"] and float(fr.table["AMOUNT"].sum()) == float(jsp["AMOUNT"].sum())
    r, fr = ask(c, t, "total kar ke batao")
    assert fr.value == float(jsp["AMOUNT"].sum()) and r.plan["filters"][0]["value"] == "JSP TRADERS (HISAR)"      # the entity stays in the follow-up
    r, fr = ask(c, t, "month wise sales with difference")
    assert {"Previous AMOUNT", "Change", "Change %"} <= set(fr.table.columns) and fr.table["Change"].iloc[-1] == fr.table["AMOUNT"].iloc[-1] - fr.table["AMOUNT"].iloc[-2]
    r, fr = ask(Conversation(schemas=c.schemas), t, "Pranjal ki sales batao")
    assert fr.kind == "clarify" and set(fr.options) == {"Pranjal Traders (CUSTOMER NAME)", "Pranjali Ji (SALES PERSON)"}    # a customer AND a salesperson fit → ask
    r, fr = ask(c, t, "Pranjal Traders")
    assert fr.kind == "answer" and r.plan["filters"] == [{"column": "CUSTOMER NAME", "op": "eq", "value": "Pranjal Traders"}]
    r, fr = ask(Conversation(schemas=c.schemas), t, "Delhi ki sales")
    assert fr.value == float(s2[s2["STATE"] == "Delhi"]["AMOUNT"].sum())
    with pytest.raises(AssertionError, match="planner"):                        # "kon kon … karta hai" is a row list → planner
        ask(Conversation(schemas=c.schemas), t, "Item0 kon kon kharidta hai")


# ---------------------------------------------------------------- V9.2 a value that exists nowhere → clear no-data reply; cross-sheet → agent
def test_missing_entity_gives_no_data_reply_never_an_unfiltered_answer(biz):
    c, t, srv = biz
    r, fr = ask(c, t, "mobile last year ki sale chahiye month wise customer wise amount aur items")
    assert fr.kind == "no_data" and "mobile" in fr.answer and fr.table is None and fr.value is None
    r, fr = ask(Conversation(schemas=c.schemas), t, "Kilo wale ki sales")
    assert fr.kind == "answer" and r.plan["filters"][0]["value"] == "Kilo"
    # the multi-metric path: a planner filter on a value that does not exist is also a no-data reply with suggestions
    analyst.OpenRouterAI = planner({"status": "execute", "mode": "data", "source_id": "biz", "sheet_name": "SALES", "operation": "aggregate", "metric": "AMOUNT", "aggregation": "sum",
                                    "group_by": ["CUSTOMER NAME"], "filters": [{"column": "CUSTOMER NAME", "op": "eq", "value": "Zorblax Traders"}]})
    r, fr = ask(Conversation(schemas=c.schemas), t, "Zorblax Traders ka sale amount aur qty customer wise")
    assert fr.kind == "no_data" and "Zorblax Traders" in fr.answer and "❌" not in fr.answer
    from agent import classify
    q = "mobile last year ki sale month wise client name amount items sold aur abhi kitna closing stock hai"
    assert classify(q, c.schemas) == "complex"                       # SALES + INVENTORY → the agent, not the multi-metric shortcut
