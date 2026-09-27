"""Conversation tests against the real OpenRouter planner. Run with:  RUN_LIVE=1 pytest tests/test_live_analyst.py
Numbers are checked against pandas truth in conftest, never against the LLM's own text."""
import os

import pytest
from dotenv import load_dotenv

load_dotenv()

import analyst                                   # noqa: E402
import memory                                    # noqa: E402
from analyst import Conversation, LocalTools    # noqa: E402
from conftest import MAX_TXN_2026, TOP_CUSTOMER_2026, TOTAL_2026, payload   # noqa: E402
from mcp_server import MCPServer                 # noqa: E402
from source_loader import read_file              # noqa: E402

pytestmark = pytest.mark.skipif(not (os.getenv("RUN_LIVE") and os.getenv("OPENROUTER_API_KEY")), reason="set RUN_LIVE=1 and OPENROUTER_API_KEY")
KEY, MODEL = os.getenv("OPENROUTER_API_KEY", ""), os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")


@pytest.fixture(autouse=True)
def private_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_FILE", tmp_path / "memory.json")   # never touch the user's learned memory


@pytest.fixture
def chat(workbook):
    srv = MCPServer()
    schema = srv.register_file("business", read_file("business.xlsx", workbook.read_bytes()))
    conv = Conversation(schemas={"business": schema})
    tools = LocalTools(srv)

    def ask(q):
        r = analyst.answer(conv, q, KEY, MODEL, tools)
        print(f"\nQ: {q}\nplan: {r.plan}\nA: {r.text}\nrows: {None if r.df is None else r.df.to_dict(orient='records')[:6]}")
        return r
    return ask


def values(r):
    return [] if r.df is None or "value" not in r.df.columns else [float(v) for v in r.df["value"]]


def test_total_then_highest(chat):
    r1 = chat("What is total sales in 2026?")
    assert r1.plan["sheet_name"] == "SALES" and r1.plan["metric"] == "AMOUNT"
    assert values(r1) == [TOTAL_2026]
    r2 = chat("Which one is highest?")
    assert r2.plan["sheet_name"] == "SALES", "must keep the SALES context"
    shown = values(r2) or [float(v) for v in r2.df.get("AMOUNT", [])]
    assert shown and shown[0] in (MAX_TXN_2026, TOP_CUSTOMER_2026[1]), "highest transaction or top customer, not the total again"
    assert TOTAL_2026 not in shown


def test_customer_wise_top5_then_graph(chat):
    r1 = chat("customer wise sales dikhao 2026 ki")
    assert r1.plan["group_by"] == ["CUSTOMER"] and r1.plan["metric"] == "AMOUNT"
    r2 = chat("only top 5")
    assert r2.plan["group_by"] == ["CUSTOMER"] and r2.plan.get("top_n") == 5 and len(r2.df) == 5
    assert r2.df.iloc[0]["CUSTOMER"] == TOP_CUSTOMER_2026[0]
    r3 = chat("graph bana do")
    assert r3.want_chart and r3.plan["group_by"] == ["CUSTOMER"] and len(r3.df) == 5


def test_correction_inventory_to_sales(chat):
    r1 = chat("closing stock kitna hai?")
    assert r1.plan["sheet_name"] == "INVENTORY"
    r2 = chat("This is closing stock, I am asking about sales amount")
    assert r2.plan["sheet_name"] == "SALES" and r2.plan["metric"] == "AMOUNT"


def test_correction_customer_to_salesman(chat):
    chat("customer wise sales batao")
    r = chat("customer wise nahi, salesman wise")
    assert r.plan["group_by"] == ["SALESMAN"] and r.plan["metric"] == "AMOUNT"


def test_not_quantity_amount(chat):
    r1 = chat("2026 ki total qty batao")
    assert r1.plan["metric"] == "QTY"
    r2 = chat("not quantity, amount")
    assert r2.plan["metric"] == "AMOUNT" and values(r2) == [TOTAL_2026]


def test_sales_amount_uses_sales_not_inventory(chat):
    r = chat("sales amount batao")
    assert r.plan["sheet_name"] == "SALES" and r.plan["metric"] == "AMOUNT"


def test_top_customer_by_sales(chat):
    r = chat("2026 mein sabse zyada sales kis customer ki hai?")
    assert r.df.iloc[0]["CUSTOMER"] == TOP_CUSTOMER_2026[0] and float(r.df.iloc[0]["value"]) == TOP_CUSTOMER_2026[1]


def test_compare_years(chat):
    r = chat("2025 aur 2026 ki sales compare karo")
    assert sorted(values(r)) == [17000.0, TOTAL_2026]
    assert "1,06,900" in r.text or "106,900" in r.text or "106900" in r.text


def test_out_of_scope_has_no_numbers(chat):
    r = chat("India ka GDP kitna hai?")
    assert r.kind == "out_of_scope" and r.df is None


def test_whatsapp_end_to_end_over_real_mcp(tmp_path, workbook, openwa):
    """Webhook payload -> bot -> analyst -> real MCP stdio server -> reply text + chart PNG (OpenWA faked)."""
    from mcp_tools import McpStdioTools
    from whatsapp.bot import Config, WhatsAppBot
    from whatsapp.store import Store
    tools = McpStdioTools()
    try:
        bot = WhatsAppBot(Config(openrouter_key=KEY, model=MODEL, session="new-testing", allowed_numbers={"*"}, data_files=[str(workbook)],
                                 files_dir=tmp_path / "f"), openwa, Store(tmp_path / "s.db"), tools)
        bot.warm_up()
        bot.handle_payload(payload(mid="E1", body="2026 mein total sales kitni hui?"))
        bot.handle_payload(payload(mid="E2", body="highest kaun hai?"))
        bot.handle_payload(payload(mid="E3", body="customer wise top 5 ka graph bana"))
        for s in openwa.sent:
            print(s["kind"], s.get("text", s.get("caption")))
        texts = openwa.texts()
        assert "1,06,900" in texts[0] or "106,900" in texts[0]
        assert [s["kind"] for s in openwa.sent][-2:] == ["text", "image"]
        assert "ABC Traders" in texts[-1]
    finally:
        tools.close()


def test_named_chart_types(chat):
    r1 = chat("2026 ki category wise sales pie chart mein dikhao")
    assert r1.plan["group_by"] == ["CATEGORY"] and r1.chart["type"] == "pie" and r1.want_chart
    r2 = chat("isko bar graph mein badlo")
    assert r2.plan["group_by"] == ["CATEGORY"] and r2.chart["type"] in ("bar", "barh")
    r3 = chat("month wise sales ka line graph")
    assert r3.plan.get("date_grain") == "month" and r3.chart["type"] == "line"


def test_database_source(tmp_path):
    import sqlite3
    db = tmp_path / "erp.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE invoices (invoice_no TEXT, customer TEXT, invoice_date TEXT, amount REAL, qty INTEGER)")
    con.executemany("INSERT INTO invoices VALUES (?,?,?,?,?)", [("I1", "ABC", "2026-01-05", 1000, 2), ("I2", "XYZ", "2026-02-07", 2500, 1), ("I3", "ABC", "2026-02-20", 700, 5)])
    con.commit(); con.close()
    srv = MCPServer()
    schema = srv.register_database("erp", f"sqlite:///{db}")
    conv = Conversation(schemas={"erp": schema})
    r = analyst.answer(conv, "sabse zyada invoice amount kis customer ka hai?", KEY, MODEL, LocalTools(srv))
    print(r.plan, r.text)
    assert r.plan["sheet_name"] == "invoices" and r.df.iloc[0]["customer"] == "XYZ" and float(r.df.iloc[0]["value"]) == 2500


def test_web_table_source(monkeypatch):
    import pandas as pd
    import mcp_server
    monkeypatch.setattr(mcp_server, "read_web", lambda url: {"kind": "web", "name": "Price list", "url": url, "text": "Our prices",
                                                              "tables": {"table_1": pd.DataFrame({"ITEM": ["Laptop", "Phone", "Tablet"], "PRICE": [50000, 20000, 30000]})}})
    srv = MCPServer()
    conv = Conversation(schemas={"web1": srv.register_web("web1", "https://example.com/p")})
    r = analyst.answer(conv, "sabse mehenga item kaunsa hai?", KEY, MODEL, LocalTools(srv))
    print(r.plan, r.text)
    assert "Laptop" in r.text and "50,000" in r.text.replace("50000", "50,000")


def test_last_12_months_top_is_not_split_by_month(chat):
    r = chat("Mujhe last 12 month ka sabse jada sale hua customer data chahiye graph me")
    assert r.plan["group_by"] == ["CUSTOMER"] and not r.plan.get("date_grain") in ("month", "day")
    assert r.df["CUSTOMER"].is_unique and r.want_chart
    assert "chart" not in r.text.lower() and "graph" not in r.text.lower(), "the text must not claim a chart was made"


def test_refers_back_to_earlier_answer(chat):
    chat("2026 ki customer wise sales batao")
    chat("2026 ki total qty kitni hai?")
    chat("month wise sales 2026")
    r = chat("pehle wali customer wali list ka graph bana do")
    assert r.plan["group_by"] == ["CUSTOMER"] and r.plan["metric"] == "AMOUNT" and r.want_chart


def test_agent_multi_source_targets_live(tmp_path):
    """Real LLM: orders (revenue) in one table, targets in another → who exceeded target. Truth from pandas."""
    import pandas as pd
    from test_generic import B_ORDERS, F_TARGETS
    srv = MCPServer()
    srv.register_file("orders", {"kind": "table", "name": "orders", "df": B_ORDERS.copy()})
    srv.register_file("targets", {"kind": "table", "name": "targets", "df": F_TARGETS.copy()})
    conv = Conversation(schemas={"orders": srv.source_schema("orders"), "targets": srv.source_schema("targets")})
    r = analyst.answer(conv, "which customers exceeded their target?", KEY, MODEL, LocalTools(srv))
    print(r.kind, r.trace, r.text)
    rev = B_ORDERS.groupby("Customer")["Revenue"].sum()
    truth = {c for c, t in F_TARGETS.set_index("Customer")["Target"].items() if rev.get(c, 0) > t}
    assert r.kind == "agent" and any("join_results" in t for t in r.trace)
    assert all(c in r.text for c in truth) and truth == {"Acme"}
    assert not any(c in r.text.split("Acme")[0] for c in ("Zed", "Bolt", "Nova")) or "Acme" in r.text
