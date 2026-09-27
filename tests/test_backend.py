"""Persistence layer (backend/*): accounts, workspaces, the permanent source registry, conversations, learning scope,
credentials, WhatsApp linking, Google OAuth (mocked HTTP) and the V6→V7 migration. All offline, in-memory SQLite.

Master-prompt acceptance tests covered here: A (source survives logout/login), B (website survives a restart with no
network), C (same account from another "browser"), S (credentials stay server-side), T (user A cannot reach user B's
source or chat), §43 (continue a conversation on device 2)."""
import io
import json
import os
from datetime import date as _date

import pandas as pd
import pytest

import analyst
import memory
from analyst import LocalTools
from mcp_server import MCPServer
from response import finalize
from test_generic import make_source, schemas_of  # noqa: F401  (make_source used indirectly)

TODAY = _date(2026, 9, 27)


class date(_date):
    @classmethod
    def today(cls):
        return TODAY


ORDERS = pd.DataFrame({"Bill No": range(1, 7), "Party Name": ["Acme", "Zed", "Acme", "Bolt", "Zed", "Acme"],
                       "Bill Date": ["2026-07-30", "2026-08-03", "2026-08-15", "2026-08-28", "2026-09-05", "2026-09-20"],
                       "Qty": [3, 5, 1, 4, 2, 1], "Net Amount": [1200.0, 1500.0, 300.0, 950.0, 700.0, 400.0]})
AUG = float(ORDERS[ORDERS["Bill Date"].str.startswith("2026-08")]["Net Amount"].sum())
SEP = float(ORDERS[ORDERS["Bill Date"].str.startswith("2026-09")]["Net Amount"].sum())


class NoLLM:
    def __init__(self, *a): pass
    def plan(self, *a, **k): raise AssertionError("planner must not be called")
    def format_result(self, *a, **k): raise AssertionError("wording model must not be called")


@pytest.fixture
def platform(monkeypatch, tmp_path):
    """Fresh in-memory database + isolated DATA_DIR + fixed date + no LLM."""
    from backend import db, sources
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_AUTH_MODE", "password")
    db.configure("sqlite://")
    sources.invalidate()
    monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)
    monkeypatch.setattr("understanding.date", date)
    monkeypatch.setattr("periods.date", date)
    memory.set_scope(None)
    yield db
    memory.set_scope(None)


def csv_bytes(df):
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def fake_web(text="Pricing: Basic plan costs 499 per month. Contact: sales@example.com", title="Demo Site"):
    return lambda url: {"kind": "web", "name": title, "url": url, "text": text, "tables": {"t1": pd.DataFrame({"plan": ["Basic", "Pro"], "price": [499, 999]})}, "images": [], "videos": []}


# ---------------------------------------------------------------- accounts & authorization
def test_register_login_and_personal_workspace(platform):
    from backend import auth
    u = auth.register("a@example.com", "password123", "Asha")
    assert u["workspace_id"].startswith("ws_") and auth.login_password("A@Example.com", "password123")["user_id"] == u["user_id"]
    with pytest.raises(auth.AuthError):
        auth.login_password("a@example.com", "wrong-password")
    with pytest.raises(auth.AuthError):
        auth.register("a@example.com", "password123")
    with pytest.raises(auth.AuthError):
        auth.register("b@example.com", "short")
    o = auth.login_oidc("g@example.com", "G", subject="sub-1")
    assert auth.login_oidc("g@example.com", None, subject="sub-1")["user_id"] == o["user_id"] and o["workspace_id"] != u["workspace_id"]


def test_user_a_cannot_access_user_b_source_or_chat(platform):          # T
    from backend import auth, conversations
    from backend.sources import SourceRegistry
    from backend.workspaces import Forbidden
    a, b = auth.register("a@x.com", "password123"), auth.register("b@x.com", "password123")
    reg = SourceRegistry()
    src = reg.add_file(b["workspace_id"], b["user_id"], "orders.csv", csv_bytes(ORDERS))
    conv = conversations.create(b["workspace_id"], b["user_id"])
    with pytest.raises(Forbidden):
        reg.get(a["workspace_id"], src["id"])
    with pytest.raises(Forbidden):
        reg.refresh(a["workspace_id"], src["id"])
    with pytest.raises(Forbidden):
        conversations.get(a["workspace_id"], conv["id"])
    with pytest.raises(Forbidden):
        conversations.hydrate(a["workspace_id"], conv["id"], {})
    assert reg.list(a["workspace_id"]) == [] and conversations.list_conversations(a["workspace_id"]) == []
    server = MCPServer()
    schemas, _ = reg.ensure(server, a["workspace_id"])
    assert schemas == {} and src["id"] not in server.sources                   # nothing of B is loaded into A's tools


# ---------------------------------------------------------------- sources persist
def test_source_survives_logout_login_and_other_browser(platform):     # A + C
    from backend import auth
    from backend.sources import SourceRegistry
    u = auth.register("a@x.com", "password123")
    reg = SourceRegistry()
    row = reg.add_file(u["workspace_id"], u["user_id"], "orders.csv", csv_bytes(ORDERS), name="Sales 2026")
    assert row["status"] == "connected" and row["schema_snapshot"]["kind"] == "table"
    # "logout" / "another browser" = a new login with no Streamlit state at all
    again = auth.login_password("a@x.com", "password123")
    listed = SourceRegistry().list(again["workspace_id"])
    assert [s["name"] for s in listed] == ["Sales 2026"] and listed[0]["id"] == row["id"]
    # the saved bytes are re-read from disk (not from any session object)
    from backend import sources as srcmod
    srcmod.invalidate()
    server = MCPServer()
    schemas, errors = SourceRegistry().ensure(server, again["workspace_id"])
    assert errors == {} and schemas[row["id"]]["row_count"] == len(ORDERS)
    # adding the same file again does not create a duplicate
    assert reg.add_file(u["workspace_id"], u["user_id"], "orders copy.csv", csv_bytes(ORDERS))["id"] == row["id"]


def test_website_survives_restart_without_network(platform, monkeypatch):   # B
    import source_loader
    from backend import auth, sources as srcmod
    from backend.sources import SourceRegistry
    u = auth.register("a@x.com", "password123")
    monkeypatch.setattr(source_loader, "read_web", fake_web())
    reg = SourceRegistry()
    row = reg.add_website(u["workspace_id"], u["user_id"], "https://example.com")
    assert row["status"] == "connected" and row["name"] == "Demo Site"

    def offline(url):
        raise RuntimeError("network down")
    monkeypatch.setattr(source_loader, "read_web", offline)
    srcmod.invalidate()                                                       # "app restart": process cache gone
    server = MCPServer()
    schemas, errors = SourceRegistry().ensure(server, u["workspace_id"])
    assert errors == {} and "Pricing" in server.sources[row["id"]]["text"] and list(server.sources[row["id"]]["tables"]) == ["t1"]
    assert schemas[row["id"]]["sheets"][0]["name"] == "t1"
    # refresh re-fetches (and reports the failure honestly, keeping the old copy)
    bad = reg.refresh(u["workspace_id"], row["id"])
    assert bad["status"] == "error" and "network down" in bad["last_error"]
    monkeypatch.setattr(source_loader, "read_web", fake_web(text="Pricing: Basic plan now costs 599 per month."))
    good = reg.refresh(u["workspace_id"], row["id"])
    assert good["status"] == "connected" and good["version"] > row["version"]
    server = MCPServer()
    SourceRegistry().ensure(server, u["workspace_id"])
    assert "599" in server.sources[row["id"]]["text"]                          # the new content, not the stale copy
    assert reg.freshness(good) == "just now"


def test_google_sheet_registry_dedupes_and_marks_public(platform, monkeypatch):
    from backend import auth
    from backend.sources import SourceRegistry
    import mcp_server
    monkeypatch.setattr(mcp_server.MCPServer, "load_google_workbook", lambda self, url: {"SALES": ORDERS.copy()})
    u = auth.register("a@x.com", "password123")
    reg = SourceRegistry()
    url = "https://docs.google.com/spreadsheets/d/ABC123xyz/edit#gid=0"
    row = reg.add_google_sheet(u["workspace_id"], u["user_id"], url, name="Sales")
    assert row["status"] == "connected" and row["auth_mode"] == "public" and row["connection_config"]["sheet_id"] == "ABC123xyz"
    assert [t["name"] for t in row["schema_snapshot"]["sheets"]] == ["SALES"]
    assert reg.add_google_sheet(u["workspace_id"], u["user_id"], "https://docs.google.com/spreadsheets/d/ABC123xyz/view")["id"] == row["id"]
    # remove: soft-deleted, gone from the list and from learning
    from backend import learning
    learning.add_mapping(u["workspace_id"], row["id"], "SALES", "date_column", "Bill Date")
    reg.remove(u["workspace_id"], row["id"])
    assert reg.list(u["workspace_id"]) == [] and learning.get_context(u["workspace_id"], [row["id"]])["mappings"] == []


def test_database_source_keeps_url_encrypted(platform, tmp_path):           # S
    import sqlite3
    from backend import auth, db
    from backend.sources import SourceRegistry
    dbfile = tmp_path / "data" / "erp.db"
    dbfile.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(dbfile) as c:
        c.execute("CREATE TABLE sales (d TEXT, amount REAL)")
        c.executemany("INSERT INTO sales VALUES (?, ?)", [("2026-08-01", 10.0), ("2026-09-01", 20.0)])
    u = auth.register("a@x.com", "password123")
    url = f"sqlite:///{dbfile}"
    row = SourceRegistry().add_database(u["workspace_id"], u["user_id"], url, name="ERP")
    assert row["status"] == "connected", row["last_error"]
    assert url not in json.dumps(row, default=str) and row["auth_mode"] == "secret"          # never in the row the UI sees
    with db.session_scope() as s:
        blob = s.get(db.SourceCredential, row["credential_id"]).encrypted_blob
    assert url not in blob and str(dbfile) not in blob
    from backend import credentials
    assert credentials.reveal(u["workspace_id"], row["credential_id"])["url"] == url
    b = auth.register("b@x.com", "password123")
    from backend.workspaces import Forbidden
    with pytest.raises(Forbidden):
        credentials.reveal(b["workspace_id"], row["credential_id"])


# ---------------------------------------------------------------- conversations persist and continue
def run_turn(reg, workspace_id, user_id, conv_id, q):
    """One web/WhatsApp request: fresh tools, hydrate from DB, answer, save."""
    from backend import conversations
    memory.set_scope(workspace_id, user_id)
    server = MCPServer()
    schemas, _ = reg.ensure(server, workspace_id)
    conv = conversations.hydrate(workspace_id, conv_id, schemas)
    r = analyst.answer(conv, q, "k", "m", LocalTools(server))
    fr = finalize(r, q)
    conversations.save_turn(workspace_id, conv_id, q, r, fr, conv)
    return r, fr


def test_conversation_continues_on_another_device(platform):               # §43 + K/L/M
    from backend import auth, conversations
    from backend.sources import SourceRegistry
    u = auth.register("a@x.com", "password123")
    reg = SourceRegistry()
    reg.add_file(u["workspace_id"], u["user_id"], "orders.csv", csv_bytes(ORDERS), name="Sales 2026")
    c = conversations.create(u["workspace_id"], u["user_id"])
    r, fr = run_turn(reg, u["workspace_id"], u["user_id"], c["id"], "last month ki sale kitni hui?")
    assert fr.shape == "scalar" and fr.value == AUG
    r, fr = run_turn(reg, u["workspace_id"], u["user_id"], c["id"], "customer wise")
    assert fr.shape == "ranking" and list(fr.table.columns)[0] == "Party Name" and fr.table["Net Amount"].sum() == AUG        # same August range
    r, fr = run_turn(reg, u["workspace_id"], u["user_id"], c["id"], "only total")
    assert fr.shape == "scalar" and fr.value == AUG and fr.table is None
    # device 2: new login, open the same conversation, continue
    again = auth.login_password("a@x.com", "password123")
    chats = conversations.list_conversations(again["workspace_id"])
    assert len(chats) == 1 and chats[0]["title"].startswith("last month ki sale")
    msgs = conversations.messages(again["workspace_id"], chats[0]["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 3 and msgs[3].get("df") is not None and msgs[5].get("df") is None
    assert msgs[5]["final_response"]["shape"] == "scalar" and msgs[5]["final_response"]["value"] == AUG
    r, fr = run_turn(SourceRegistry(), again["workspace_id"], again["user_id"], chats[0]["id"], "August ka detail dikhao")
    assert fr.shape in ("scalar", "detail") and fr.value in (AUG, None)                # context (August, sales) understood, no re-adding anything
    r, fr = run_turn(SourceRegistry(), again["workspace_id"], again["user_id"], chats[0]["id"], "September ka?")
    assert fr.value == SEP
    # results are stored and readable
    res = conversations.result(again["workspace_id"], msgs[3]["final_response"]["result_id"])
    assert res["row_count"] >= 2 and res["metric"] and list(res["df"].columns)[0] == "Party Name"
    conversations.rename(again["workspace_id"], chats[0]["id"], "August analysis")
    assert conversations.get(again["workspace_id"], chats[0]["id"])["title"] == "August analysis"
    conversations.delete_conversation(again["workspace_id"], chats[0]["id"])
    assert conversations.list_conversations(again["workspace_id"]) == []


def test_learning_is_scoped_per_workspace(platform):
    from backend import auth
    a, b = auth.register("a@x.com", "password123"), auth.register("b@x.com", "password123")
    memory.set_scope(a["workspace_id"], a["user_id"])
    memory.add_mapping("src_1", None, "count:item", "Qty")
    memory.add_rule("turnover means AMOUNT", "src_1")
    memory.add_plan("total sales", {"source_id": "src_1", "operation": "aggregate", "metric": "AMOUNT"}, validated=True)
    ctx = memory.get_context(source_ids=["src_1"])
    assert ctx["mappings"][0]["column"] == "Qty" and ctx["rules"] == ["turnover means AMOUNT"] and len(ctx["successful_plans"]) == 1
    memory.set_scope(b["workspace_id"], b["user_id"])
    assert memory.get_context(source_ids=["src_1"]) == {"rules": [], "mappings": [], "successful_plans": []}
    memory.set_scope(a["workspace_id"], a["user_id"])
    for i in range(8):
        memory.add_plan(f"q{i}", {"source_id": "src_1", "operation": "aggregate", "metric": "AMOUNT", "i": i}, validated=True)
    assert len(memory.get_context(source_ids=["src_1"])["successful_plans"]) == 5           # cap per source
    memory.add_plan("bad", {"source_id": "src_1", "metric": "QTY"}, validated=False)
    assert all(p["plan"].get("metric") != "QTY" for p in memory.get_context(source_ids=["src_1"])["successful_plans"])


def test_preferences_follow_the_user(platform):
    from backend import auth, preferences
    u = auth.register("a@x.com", "password123")
    assert preferences.get(u["user_id"], u["workspace_id"])["number_style"] == "indian"
    preferences.save(u["user_id"], u["workspace_id"], {"number_style": "international", "tts_auto": True, "junk": 1})
    p = preferences.get(u["user_id"], u["workspace_id"])
    assert p["number_style"] == "international" and p["tts_auto"] is True and "junk" not in p


# ---------------------------------------------------------------- WhatsApp linking
def test_whatsapp_link_code_binds_number_to_workspace(platform, make_bot, openwa):
    from backend import auth, whatsapp_link
    from backend.sources import SourceRegistry
    from test_whatsapp import USER, payload
    u = auth.register("a@x.com", "password123")
    SourceRegistry().add_file(u["workspace_id"], u["user_id"], "orders.csv", csv_bytes(ORDERS), name="Workspace Sales")
    code = whatsapp_link.create_code(u["workspace_id"], u["user_id"])
    assert [l["link_code"] for l in whatsapp_link.list_links(u["workspace_id"])] == [code]
    bot = make_bot(allowed_numbers=set())                                      # not allowlisted in .env …
    bot.handle_payload(payload(mid="L0", body="/status"))
    assert openwa.texts() == []                                                 # … so ignored before linking
    bot = make_bot(allowed_numbers={"*"})
    bot.handle_payload(payload(mid="L1", body="/link WRONG"))
    assert "galat" in openwa.texts()[-1]
    bot.handle_payload(payload(mid="L2", body=f"/link {code}"))
    assert "jud gaya" in openwa.texts()[-1]
    linked = whatsapp_link.workspace_for(USER.split("@")[0])
    assert linked["workspace_id"] == u["workspace_id"] and whatsapp_link.list_links(u["workspace_id"])[0]["external_last4"] == USER.split("@")[0][-4:]
    bot.handle_payload(payload(mid="L3", body="/status"))
    assert "Workspace Sales" in openwa.texts()[-1]
    # a linked number is allowed even without the .env allowlist
    bot2 = make_bot(allowed_numbers=set())
    bot2.handle_payload(payload(mid="L4", body="/status"))
    assert "Workspace Sales" in openwa.texts()[-1]


# ---------------------------------------------------------------- Google OAuth (HTTP mocked)
class FakeHTTP:
    def __init__(self):
        self.calls = []

    class R:
        def __init__(self, status, data):
            self.status_code, self._d = status, data

        def json(self):
            return self._d

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, dict(data)))
        if data.get("grant_type") == "authorization_code":
            return self.R(200, {"access_token": "at-1", "refresh_token": "rt-secret-token", "scope": "sheets", "expires_in": 3600})
        return self.R(200, {"access_token": "at-2", "expires_in": 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params))
        if "userinfo" in url:
            return self.R(200, {"email": "owner@gmail.com"})
        if url.endswith("/values:batchGet"):
            return self.R(200, {"valueRanges": [{"values": [["Bill Date", "Net Amount"], ["2026-08-01", 100], ["2026-09-01", "1,200.50"]]}]})
        return self.R(200, {"properties": {"title": "Private Sales"}, "sheets": [{"properties": {"title": "SALES"}}]})


def test_google_oauth_tokens_stay_server_side(platform, monkeypatch):      # S
    from backend import auth, db, google_oauth
    from backend.sources import SourceRegistry
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("APP_BASE_URL", "https://analyst.example.com")
    u = auth.register("a@x.com", "password123")
    assert google_oauth.configured()
    url = google_oauth.start_url(u["workspace_id"], u["user_id"])
    assert "accounts.google.com" in url and "redirect_uri=https%3A%2F%2Fanalyst.example.com%2Fsources" in url and "access_type=offline" in url
    state = url.split("state=")[1].split("&")[0]
    http = FakeHTTP()
    other = auth.register("b@x.com", "password123")
    with pytest.raises(google_oauth.OAuthError):
        google_oauth.finish("code-1", state, other["workspace_id"], other["user_id"], http=http)      # state bound to A
    acc = google_oauth.finish("code-1", state, u["workspace_id"], u["user_id"], http=http)
    assert acc["google_account_email"] == "owner@gmail.com" and "rt-secret-token" not in json.dumps(acc, default=str)
    with db.session_scope() as s:
        blobs = [c.encrypted_blob for c in s.query(db.SourceCredential).all()]
    assert blobs and all("rt-secret-token" not in b for b in blobs)
    assert [a["google_account_email"] for a in google_oauth.accounts(u["workspace_id"])] == ["owner@gmail.com"]
    wb = google_oauth.load_workbook(u["workspace_id"], acc["id"], "SHEET1", http=http)
    assert list(wb["tabs"]) == ["SALES"] and list(wb["tabs"]["SALES"]["Net Amount"]) == [100.0, 1200.5]
    # a private sheet source uses the credential; the row never contains the token
    monkeypatch.setattr(google_oauth, "load_workbook", lambda ws, cid, sid, name=None, http=None: wb)
    row = SourceRegistry().add_google_sheet(u["workspace_id"], u["user_id"], "https://docs.google.com/spreadsheets/d/SHEET1/edit", credential_id=acc["id"])
    assert row["status"] == "connected" and row["auth_mode"] == "oauth" and "rt-secret" not in json.dumps(row, default=str)
    assert google_oauth.accounts(other["workspace_id"]) == []


# ---------------------------------------------------------------- migration
def test_migration_seeds_bootstrap_workspace(platform, monkeypatch, tmp_path):
    import mcp_server
    monkeypatch.setattr(mcp_server.MCPServer, "load_google_workbook", lambda self, url: {"SALES": ORDERS.copy()})
    sheet = "https://docs.google.com/spreadsheets/d/MIG123/edit"
    monkeypatch.setenv("WHATSAPP_GOOGLE_SHEET_URL", sheet)
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "919999900001")
    monkeypatch.delenv("WHATSAPP_DATA_FILES", raising=False)
    monkeypatch.delenv("WHATSAPP_DATABASE_URL", raising=False)
    from scripts import migrate_v6_to_v7 as mig
    mem = tmp_path / "workspace_memory.json"
    mem.write_text(json.dumps({"rules": [{"text": "turnover means AMOUNT", "source_id": "google_sheet"}], "mappings": [{"source": "google_sheet", "sheet": "SALES", "term": "date_column", "column": "Bill Date"}], "successful_plans": []}))
    monkeypatch.setattr(mig, "ROOT", tmp_path)
    assert mig.main(["--email", "owner@x.com", "--password", "password123"]) == 0
    from backend import auth, learning, whatsapp_link
    from backend.sources import SourceRegistry
    u = auth.login_password("owner@x.com", "password123")
    srcs = SourceRegistry().list(u["workspace_id"])
    assert len(srcs) == 1 and srcs[0]["type"] == "google_sheet" and srcs[0]["status"] == "connected"
    ctx = learning.get_context(u["workspace_id"], [srcs[0]["id"]])
    assert ctx["rules"] == ["turnover means AMOUNT"] and ctx["mappings"][0]["column"] == "Bill Date"
    assert whatsapp_link.workspace_for("919999900001")["workspace_id"] == u["workspace_id"]
    assert mig.main(["--email", "owner@x.com", "--password", "password123"]) == 0 and len(SourceRegistry().list(u["workspace_id"])) == 1   # idempotent
