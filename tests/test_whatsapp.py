"""WhatsApp channel tests that need no network: OpenWA and the LLM are faked."""
import hashlib
import re
import hmac
import io
import json

import pandas as pd
import pytest
import requests
from starlette.testclient import TestClient

import analyst
from analyst import Reply
from chart_ui import render_png
from conftest import BOT_NUMBER, SESSION, USER, inline_media, payload
from mcp_server import MCPServer, resolve_column
from source_loader import read_file
from whatsapp import bot as botmod
from whatsapp.openwa import OpenWAError, OpenWAService
from whatsapp.webhook import normalize, verify_signature
import whatsapp_server


def fake_answer(text="Total sales ₹1,06,900 hai.", df=None, **kw):
    calls = []

    def answer(conv, question, api_key, model, tools, log=None):
        calls.append({"question": question, "schemas": list(conv.schemas), "last_plan": conv.last_plan})
        conv.last_plan = {"status": "execute", "question": question}
        conv.history += [{"role": "user", "content": question}, {"role": "assistant", "content": text}]
        return Reply(text, df=df, plan={"title": "Sales 2026"}, **kw)
    return answer, calls


# 1. webhook parsing
def test_normalize_message_received():
    m = normalize(payload(mid="3EB0ABC", body=" Total sales? "))
    assert (m.message_id, m.chat_id, m.sender_id, m.message_type, m.text) == ("3EB0ABC", USER, USER, "text", "Total sales?")
    assert m.session_id == SESSION and m.idempotency_key == f"msg_{SESSION}_3EB0ABC" and m.sender_name == "Tester"
    assert not m.from_me and not m.is_group


def test_group_sender_is_author():
    m = normalize(payload(chat="1203630@g.us", is_group=True, author="919999900002@c.us"))
    assert m.is_group and m.sender_id == "919999900002@c.us" and m.chat_id == "1203630@g.us"


# 2. only message.received is handled
@pytest.mark.parametrize("event", ["message.sent", "message.ack", "session.status", "test"])
def test_other_events_ignored(event, make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    assert normalize(payload(event=event)) is None
    bot.handle_payload(payload(event=event))
    assert openwa.sent == []


# 3. duplicate delivery -> one reply
def test_duplicate_message_processed_once(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="DUP1", body="total sales"))
    bot.handle_payload(payload(mid="DUP1", body="total sales"))   # OpenWA retry
    assert len(calls) == 1 and len(openwa.texts()) == 1


# 4. never answer our own messages (loop guard)
@pytest.mark.parametrize("p", [payload(mid="OWN1", from_me=True), payload(mid="OWN2", chat=f"{BOT_NUMBER}@c.us"),
                               payload(mid="ST1", chat="status@broadcast")])
def test_own_and_status_messages_ignored(p, make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(p)
    assert calls == [] and openwa.sent == []


def test_allowlist_and_groups(make_bot, openwa, monkeypatch):
    bot = make_bot(allowed_numbers={"919999900001"})
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="A1", chat="918888800000@c.us"))
    bot.handle_payload(payload(mid="A2", chat="1203630@g.us", is_group=True, author=USER))
    assert calls == []
    bot.handle_payload(payload(mid="A3"))
    assert len(calls) == 1


def test_empty_allowlist_replies_to_nobody(make_bot, openwa, monkeypatch):
    bot = make_bot(allowed_numbers=set())
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    bot.handle_payload(payload(mid="N1"))
    assert openwa.sent == []


# 5. text message -> shared analyst -> WhatsApp text
def test_text_message_uses_shared_analyst(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="T1", body="2026 mein total sales kitni hui?"))
    assert calls[0]["question"] == "2026 mein total sales kitni hui?"
    assert any(s.startswith("file_1_business") for s in calls[0]["schemas"])
    assert openwa.texts() == ["Total sales ₹1,06,900 hai."]


# 6. voice note -> STT -> analyst (inline base64 and archived download)
@pytest.mark.parametrize("inline", [True, False])
def test_voice_note(inline, make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    heard = []
    monkeypatch.setattr(botmod.OpenRouterAI, "transcribe", lambda self, audio, fmt="wav", model=None: heard.append((audio, fmt)) or "2026 mein sabse zyada sales kis customer ki hai?")
    audio = b"OggS-fake-opus"
    media = inline_media(audio, "audio/ogg; codecs=opus") if inline else {"mimetype": "audio/ogg; codecs=opus", "omitted": True, "sizeBytes": 5_000_000}
    openwa.media["V1"] = (audio, "audio/ogg")
    bot.handle_payload(payload(mid="V1", body="", type="voice", media=media))
    assert heard == [(audio, "ogg")]
    assert calls[0]["question"] == "2026 mein sabse zyada sales kis customer ki hai?"


def test_voice_stt_failure_tells_user(make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(botmod.OpenRouterAI, "transcribe", boom)
    bot.handle_payload(payload(mid="V2", body="", type="voice", media=inline_media(b"x", "audio/ogg")))
    assert "samajh nahi aaya" in openwa.texts()[0]


# 7. image -> polite refusal (the analyst reads data files, not photos)
def test_image_not_analysed(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="I1", type="image", body="", media=inline_media(b"\x89PNG", "image/jpeg")))
    assert calls == [] and "analyse nahi" in openwa.texts()[0]


# 8. document -> existing file pipeline -> follow-up questions see that file
def test_document_then_question(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    csv = b"CUSTOMER,AMOUNT\nA,100\nB,250\n"
    bot.handle_payload(payload(mid="D1", type="document", body="orders.csv", media=inline_media(csv, "text/csv", "orders.csv")))
    assert "orders.csv" in openwa.texts()[0] and "2 rows" in openwa.texts()[0]
    bot.handle_payload(payload(mid="D2", body="is file mein total kitna hai?"))
    assert any(s.startswith("wa_") for s in calls[0]["schemas"]), "uploaded file must be a source for this chat"


def test_document_with_caption_is_answered(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    csv = b"CUSTOMER,AMOUNT\nA,100\n"
    bot.handle_payload(payload(mid="D3", type="document", body="total batao", media=inline_media(csv, "text/csv", "o.csv")))
    assert calls[0]["question"] == "total batao"


def test_files_are_per_chat(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="F1", type="document", body="", media=inline_media(b"A,B\n1,2\n", "text/csv", "a.csv")))
    bot.handle_payload(payload(mid="F2", chat="919999900003@c.us", body="total?"))
    assert not any(s.startswith("wa_") for s in calls[0]["schemas"])


# 9. per-chat conversation state is persisted and isolated
def test_context_persisted_per_chat(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="C1", body="total sales 2026"))
    bot.handle_payload(payload(mid="C2", body="highest kaun hai?"))
    bot.handle_payload(payload(mid="C3", chat="919999900003@c.us", body="hello"))
    assert calls[1]["last_plan"]["question"] == "total sales 2026"
    assert calls[2]["last_plan"] is None


def test_reset_clears_only_this_chat(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="R1", body="total sales"))
    bot.handle_payload(payload(mid="R2", chat="919999900003@c.us", body="total sales"))
    bot.handle_payload(payload(mid="R3", body="/reset"))
    bot.handle_payload(payload(mid="R4", body="aur?"))
    bot.handle_payload(payload(mid="R5", chat="919999900003@c.us", body="aur?"))
    assert calls[2]["last_plan"] is None and calls[3]["last_plan"] is not None


def test_help_and_status(make_bot, openwa):
    bot = make_bot()
    bot.handle_payload(payload(mid="H1", body="/help"))
    bot.handle_payload(payload(mid="H2", body="/status"))
    assert "Commands" in openwa.texts()[0]
    assert "SALES" in openwa.texts()[1] and "INVENTORY" in openwa.texts()[1]


# 11. exact normalized column resolution; AMOUNT never becomes QTY
@pytest.mark.parametrize("asked", ["AMOUNT", "Amount", " amount ", "AMOUNT "])
def test_amount_resolution(asked):
    df = pd.DataFrame(columns=["QTY", "ALT_QTY", "RATE", "AMOUNT"])
    assert resolve_column(df, asked) == "AMOUNT"


def test_missing_amount_is_not_substituted():
    df = pd.DataFrame({"QTY": [1], "RATE": [2]})
    assert resolve_column(df, "AMOUNT") is None
    with pytest.raises(ValueError, match='Metric column "AMOUNT" not found'):
        MCPServer().aggregate_dataframe(df, "AMOUNT")


# 13-15. MAX / TOP_N / GROUP BY on the real tools
def test_tools_top_n_group_by_and_max(workbook):
    srv = MCPServer()
    srv.register_file("wb", read_file("business.xlsx", workbook.read_bytes()))
    top = srv.call_tool("aggregate_source", {"source_id": "wb", "sheet_name": "SALES", "metric": "AMOUNT", "group_by": ["CUSTOMER"],
                                             "date_column": "VOUCHER DATE", "date_from": "2026-01-01", "date_to": "2026-12-31", "sort": "desc", "top_n": 1})
    assert top["rows"] == [{"CUSTOMER": "ABC Traders", "value": 56000}]
    mx = srv.call_tool("aggregate_source", {"source_id": "wb", "sheet_name": "SALES", "metric": "AMOUNT", "aggregation": "max",
                                            "date_column": "VOUCHER DATE", "date_from": "2026-01-01", "date_to": "2026-12-31"})
    assert mx["rows"] == [{"value": 45000}]


# 16. chart PNG comes from the result rows
def test_chart_png():
    df = pd.DataFrame({"CUSTOMER": ["ABC Traders", "XYZ Ltd"], "value": [56000, 22000]})
    png = render_png(df, {"type": "barh", "x": "CUSTOMER", "y": "value"}, "AMOUNT", "Top customers")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert render_png(pd.DataFrame({"value": [1]}), None, "AMOUNT") is None


def test_chart_sent_when_asked(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"CUSTOMER": ["ABC Traders", "XYZ Ltd", "PQR Corp"], "value": [56000, 22000, 18000]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("Top 3 customers:", df=df, want_chart=True, chart={"type": "barh", "x": "CUSTOMER", "y": "value"}, metric="AMOUNT")[0])
    bot.handle_payload(payload(mid="G1", body="top 3 ka graph bana"))
    kinds = [s["kind"] for s in openwa.sent]
    assert kinds == ["text", "image"]
    assert "1. ABC Traders — ₹56,000" in openwa.texts()[0]


def test_large_result_sends_csv(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"CUSTOMER": [f"C{i}" for i in range(25)], "value": range(25)})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("25 customers", df=df, metric="AMOUNT")[0])
    bot.handle_payload(payload(mid="L1", body="customer wise"))
    assert [s["kind"] for s in openwa.sent] == ["text", "document"]
    assert "aur 15 rows" in openwa.texts()[0]


def test_chart_failure_falls_back_to_text(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"CUSTOMER": ["A", "B", "C"], "value": [3, 2, 1]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x", df=df, want_chart=True)[0])
    import chart_ui
    monkeypatch.setattr(chart_ui, "render_png", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("renderer broke")))
    bot.handle_payload(payload(mid="G2", body="graph"))
    assert [s["kind"] for s in openwa.sent] == ["text", "text"] and "graph" in openwa.texts()[1].lower()


def test_voice_reply_when_enabled(make_bot, openwa, monkeypatch):
    bot = make_bot(voice_reply="voice")
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    monkeypatch.setattr(botmod.OpenRouterAI, "transcribe", lambda *a, **k: "total sales")
    import tts_service
    class P:
        def speak(self, text, **k):
            return tts_service.SpeechResult(provider="local", text=text, language="hi", lang_tags=["hi-IN"], audio=b"RIFFwav", mime="audio/wav")
    monkeypatch.setattr(tts_service, "get_provider", lambda name: P())
    bot.handle_payload(payload(mid="VR1", body="", type="voice", media=inline_media(b"OggS", "audio/ogg")))
    audio = [s for s in openwa.sent if s["kind"] == "audio"]
    assert audio and audio[0]["ptt"] is True and audio[0]["mimetype"].startswith("audio/ogg")
    bot.handle_payload(payload(mid="VR2", body="total sales"))       # typed question: text only by default
    assert len([s for s in openwa.sent if s["kind"] == "audio"]) == 1


# 17-19. OpenWA request shapes (from /api/docs-json)
class Recorder:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        resp = requests.Response()
        resp.status_code, resp._content = r[0], (r[1] if isinstance(r[1], bytes) else json_bytes(r[1]))
        resp.headers["Content-Type"] = "application/json"
        return resp


def json_bytes(obj):
    return json.dumps(obj).encode()


def svc(monkeypatch, responses):
    s = OpenWAService("https://w.example.com/", "k-123", "new-testing")
    rec = Recorder(responses)
    monkeypatch.setattr(s.http, "request", rec)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return s, rec


def test_openwa_send_text(monkeypatch):
    s, rec = svc(monkeypatch, [(201, {"messageId": "true_x_1", "timestamp": 1})])
    assert s.send_text(USER, "hello")["messageId"] == "true_x_1"
    c = rec.calls[0]
    assert (c["method"], c["url"]) == ("POST", "https://w.example.com/api/sessions/new-testing/messages/send-text")
    assert c["json"] == {"chatId": USER, "text": "hello"} and s.http.headers["X-API-Key"] == "k-123"


def test_openwa_send_image(monkeypatch):
    s, rec = svc(monkeypatch, [(201, {"messageId": "m", "timestamp": 1})])
    s.send_image(USER, b"\x89PNG", caption="Top 5")
    c = rec.calls[0]
    assert c["url"].endswith("/messages/send-image")
    assert c["json"] == {"chatId": USER, "base64": "iVBORw==", "mimetype": "image/png", "filename": "chart.png", "caption": "Top 5"}


def test_openwa_send_audio_voice_note(monkeypatch):
    s, rec = svc(monkeypatch, [(201, {"messageId": "m", "timestamp": 1})])
    s.send_audio(USER, b"OggS", "audio/ogg; codecs=opus", ptt=True)
    c = rec.calls[0]
    assert c["url"].endswith("/messages/send-audio") and c["json"]["ptt"] is True and c["json"]["mimetype"] == "audio/ogg; codecs=opus"


def test_openwa_media_url(monkeypatch):
    s, rec = svc(monkeypatch, [(200, b"OggS")])
    data, _ = s.download_media(USER, "false_919@c.us_3EB0")
    assert data == b"OggS"
    assert rec.calls[0]["url"] == "https://w.example.com/api/sessions/new-testing/messages/919999900001%40c.us/false_919%40c.us_3EB0/media"


# 20. OpenWA failure: sends are never blindly retried (no duplicate WhatsApp messages)
def test_openwa_send_5xx_not_retried(monkeypatch):
    s, rec = svc(monkeypatch, [(500, {"message": "engine busy"})])
    with pytest.raises(OpenWAError, match="500"):
        s.send_text(USER, "x")
    assert len(rec.calls) == 1


def test_openwa_send_read_timeout_not_retried(monkeypatch):
    s, rec = svc(monkeypatch, [requests.ReadTimeout("slow")])
    with pytest.raises(OpenWAError):
        s.send_text(USER, "x")
    assert len(rec.calls) == 1


def test_openwa_connect_error_is_retried(monkeypatch):
    s, rec = svc(monkeypatch, [requests.exceptions.ConnectTimeout("no route"), (201, {"messageId": "m", "timestamp": 1})])
    s.send_text(USER, "x")
    assert len(rec.calls) == 2


def test_bot_survives_openwa_down(make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    openwa.fail_send = "OpenWA unreachable"
    bot.handle_payload(payload(mid="W1", body="total"))     # must not raise
    assert openwa.sent == []


# 21. MCP / data failure: tell the user, never invent a number
def test_data_tool_failure(make_bot, openwa, monkeypatch):
    bot = make_bot()
    def fail(*a, **k):
        raise ValueError('Metric column "PROFIT" not found. Available columns: AMOUNT, QTY')
    monkeypatch.setattr(botmod.analyst, "answer", fail)
    bot.handle_payload(payload(mid="M1", body="profit?"))
    t = openwa.texts()[0]
    assert "nahi nikal paaya" in t and "₹" not in t


def test_mcp_restart_reregisters_and_retries(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    state = {"n": 0}
    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionError("MCP server restarted")
        return answer(*a, **k)
    monkeypatch.setattr(botmod.analyst, "answer", flaky)
    bot.handle_payload(payload(mid="M2", body="total"))
    assert state["n"] == 2 and openwa.texts() == ["Total sales ₹1,06,900 hai."]


def test_source_unavailable(tmp_path, make_bot, openwa, monkeypatch):
    bot = make_bot(data_files=[str(tmp_path / "missing.xlsx")])
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    bot.handle_payload(payload(mid="S1", body="total"))
    assert "reach nahi" in openwa.texts()[0]


# 22. LLM failure -> friendly message
def test_llm_failure(make_bot, openwa, monkeypatch):
    bot = make_bot()
    def down(*a, **k):
        raise requests.HTTPError("502 Bad Gateway")
    monkeypatch.setattr(botmod.analyst, "answer", down)
    bot.handle_payload(payload(mid="LLM1", body="total"))
    assert "AI service" in openwa.texts()[0]


# Webhook endpoint: signature, fast ack, background processing
def signed(body, secret="s" * 32, header="X-Ashveratech-Signature"):
    return {header: "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


class InlineExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *a):
        self.jobs.append((fn, a))

    def shutdown(self, wait=False):
        pass


def test_webhook_signature_and_ack(make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    ex = InlineExecutor()
    client = TestClient(whatsapp_server.build_app(bot=bot, executor=ex, secret="s" * 32))
    body = json.dumps(payload(mid="WH1", body="total")).encode()
    assert client.post("/webhook/openwa", content=body, headers={"Content-Type": "application/json"}).status_code == 401
    assert client.post("/webhook/openwa", content=body, headers=signed(body, "wrong" * 8)).status_code == 401
    r = client.post("/webhook/openwa", content=body, headers=signed(body))
    assert r.status_code == 200 and openwa.sent == [], "must ack before thinking"
    fn, args = ex.jobs[0]
    fn(*args)
    assert openwa.texts() == ["Total sales ₹1,06,900 hai."]


def test_webhook_rejects_when_secret_missing(make_bot):
    client = TestClient(whatsapp_server.build_app(bot=make_bot(), executor=InlineExecutor(), secret="", allow_unsigned=False))
    assert client.post("/webhook/openwa", json=payload()).status_code == 503


def test_verify_signature():
    body = b'{"a":1}'
    assert verify_signature(body, signed(body)["X-Ashveratech-Signature"], "s" * 32)
    assert not verify_signature(body, None, "s" * 32)


def test_logs_mask_numbers_and_keys(caplog):
    from whatsapp.log import _logger, log
    _logger.propagate = True
    try:
        with caplog.at_level("INFO", logger="whatsapp"):
            log("x", chat=USER, note="key sk-or-v1-abcdef123")
    finally:
        _logger.propagate = False
    out = caplog.text
    assert "919999900001" not in out and "0001@c.us" in out and "sk-or" not in out


def test_session_name_resolves_to_uuid(monkeypatch):
    s, rec = svc(monkeypatch, [(200, [{"id": "0b3f-uuid", "name": "new-testing", "status": "ready", "phone": "919990930426"}]),
                               (201, {"messageId": "m", "timestamp": 1})])
    assert s.resolve_session()["id"] == "0b3f-uuid"
    s.send_text(USER, "hi")
    assert rec.calls[0]["url"].endswith("/api/sessions?limit=1000")
    assert rec.calls[1]["url"] == "https://w.example.com/api/sessions/0b3f-uuid/messages/send-text"


def test_other_session_ignored(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    p = payload(mid="OS1")
    p["sessionId"] = "some-other-session"
    bot.handle_payload(p)
    assert calls == []


def test_lid_sender_resolved_for_allowlist(make_bot, openwa, monkeypatch):
    bot = make_bot(allowed_numbers={"919999900001"})
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    lid = "233968511238383@lid"
    openwa.lids[lid] = "919999900001"
    p = payload(mid="LID1", chat=lid)
    p["data"]["contact"] = {"pushName": "Tester"}           # no number: only the privacy id
    bot.handle_payload(p)
    assert len(calls) == 1 and openwa.sent[0]["chat"] == lid   # reply goes back to the same chat id
    openwa.lids.clear()
    p2 = payload(mid="LID2", chat="111122223333@lid"); p2["data"]["contact"] = {}
    bot.handle_payload(p2)
    assert len(calls) == 1, "unknown lid is not allowlisted"


def test_allowlist_without_country_code(make_bot, openwa, monkeypatch):
    bot = make_bot(allowed_numbers={"9999900001"})
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="CC1"))                      # sender 919999900001
    bot.handle_payload(payload(mid="CC2", chat="919999900002@c.us"))
    assert len(calls) == 1


def test_webhook_accepts_upstream_header_name(make_bot, openwa, monkeypatch):
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    ex = InlineExecutor()
    client = TestClient(whatsapp_server.build_app(bot=make_bot(), executor=ex, secret="s" * 32))
    body = json.dumps(payload(mid="UP1")).encode()
    assert client.post("/webhook/openwa", content=body, headers=signed(body, header="X-OpenWA-Signature")).status_code == 200


def test_csv_failure_sends_rest_as_text(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"CUSTOMER": [f"C{i}" for i in range(25)], "value": range(25, 0, -1)})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("25 customers", df=df, metric="AMOUNT")[0])
    def boom(*a, **k):
        raise OpenWAError("OpenWA 500: Internal server error", 500)
    monkeypatch.setattr(openwa, "send_document", boom)
    bot.handle_payload(payload(mid="CF1", body="customer wise"))
    texts = openwa.texts()
    assert len(texts) == 2 and "11. C10" in texts[1] and "25. C24" in texts[1]
    assert "CSV" not in texts[0], "never promise a file before it is sent"


def test_voice_answer_echoes_transcript_and_footer(make_bot, openwa, monkeypatch):
    bot = make_bot()
    plan = {"status": "execute", "mode": "data", "sheet_name": "SALES", "operation": "aggregate", "metric": "AMOUNT", "date_column": "VOUCHER DATE",
            "date_from": "2026-01-01", "date_to": "2026-12-31", "title": "Sales 2026"}
    def answer(conv, q, *a, **k):
        return Reply("Total ₹1,06,900.", df=pd.DataFrame({"value": [106900]}), plan=plan, metric="AMOUNT")
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    monkeypatch.setattr(botmod.OpenRouterAI, "transcribe", lambda *a, **k: "2026 ki total sales")
    bot.handle_payload(payload(mid="VE1", body="", type="voice", media=inline_media(b"OggS", "audio/ogg")))
    t = openwa.texts()[0]
    assert t.startswith('🎤 _"2026 ki total sales"_')
    assert "_📁 SALES · AMOUNT · 01-01-2026 → 31-12-2026_" in t and "month wise" in t


def test_list_not_repeated_in_text():
    from whatsapp.format import compose
    df = pd.DataFrame({"period": ["2026"] * 3, "CUSTOMER": ["ABC Traders", "XYZ Ltd", "PQR Corp"], "value": [56000, 22000, 18000]})
    text, _ = compose(Reply("ABC Traders ₹56,000, XYZ Ltd ₹22,000, PQR Corp ₹18,000.", df=df, metric="AMOUNT", plan={"title": "Top 3"}))
    assert text.count("XYZ Ltd") == 1 and "🏆 Sabse upar: *ABC Traders* — ₹56,000 · Inka kul: ₹96,000" in text
    assert "2026 ·" not in text, "constant period column is not repeated on every line"


# ---------- charts: automatic, and the type the user asked for ----------
def test_auto_chart_for_grouped_results(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"CATEGORY": ["A", "B", "C"], "value": [3, 2, 1]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x", df=df, chart={"type": "bar", "x": "CATEGORY", "y": "value"}, metric="AMOUNT")[0])
    bot.handle_payload(payload(mid="AC1", body="category wise sales"))        # no "graph" word
    assert [s["kind"] for s in openwa.sent] == ["text", "image"]


def test_no_auto_chart_for_single_value(make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("₹5", df=pd.DataFrame({"value": [5]}), metric="AMOUNT")[0])
    bot.handle_payload(payload(mid="AC2", body="total sales"))
    assert [s["kind"] for s in openwa.sent] == ["text"]


def test_auto_chart_can_be_disabled(make_bot, openwa, monkeypatch):
    bot = make_bot(auto_chart=False)
    df = pd.DataFrame({"CATEGORY": ["A", "B", "C"], "value": [3, 2, 1]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x", df=df, chart={"type": "bar", "x": "CATEGORY", "y": "value"})[0])
    bot.handle_payload(payload(mid="AC3", body="category wise"))
    assert [s["kind"] for s in openwa.sent] == ["text"]


@pytest.mark.parametrize("kind", ["pie", "donut", "line", "barh", "area"])
def test_requested_chart_type_is_used(kind):
    df = pd.DataFrame({"CATEGORY": ["A", "B", "C"], "value": [3, 2, 1]})
    hint = analyst.chart_hint({"chart": kind, "group_by": ["CATEGORY"]}, df)
    assert hint == {"type": kind, "x": "CATEGORY", "y": "value"}
    assert render_png(df, hint, "AMOUNT", "t")[:4] == b"\x89PNG"


# ---------- connecting data from the chat ----------
def fake_sheet(monkeypatch, workbook):
    from source_loader import normalize_columns
    tabs = {n: normalize_columns(d) for n, d in pd.read_excel(workbook, sheet_name=None).items()}
    monkeypatch.setattr(MCPServer, "load_google_workbook", lambda self, url: tabs)


def test_google_sheet_link_connects(make_bot, openwa, monkeypatch, workbook):
    fake_sheet(monkeypatch, workbook)
    bot = make_bot(data_files=[])
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="GS1", body="https://docs.google.com/spreadsheets/d/1AbC_x/edit?usp=sharing"))
    assert "connect ho gayi" in openwa.texts()[0] and "SALES" in openwa.texts()[0] and calls == []
    bot.handle_payload(payload(mid="GS2", body="sales amount batao"))
    assert any("_sheet_" in s for s in calls[0]["schemas"])


def test_link_with_question_answers_directly(make_bot, openwa, monkeypatch, workbook):
    fake_sheet(monkeypatch, workbook)
    bot = make_bot(data_files=[])
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="GS3", body="is sheet se total sales batao https://docs.google.com/spreadsheets/d/1AbC_x/edit"))
    assert calls[0]["question"].startswith("is sheet se total sales batao\n[Sawal is sheet ke baare mein hai")
    assert any("_sheet_" in s for s in calls[0]["schemas"])


def test_website_link_connects(make_bot, openwa, monkeypatch):
    import mcp_server
    monkeypatch.setattr(mcp_server, "read_web", lambda url: {"kind": "web", "name": "Price list", "url": url, "text": "Laptop 50000",
                                                              "tables": {"table_1": pd.DataFrame({"ITEM": ["Laptop"], "PRICE": [50000]})}})
    bot = make_bot()
    answer, calls = fake_answer("Ye ek price list hai.")
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="WB1", body="https://example.com/prices"))
    q = calls[0]["question"]
    assert q.startswith("Is website ka short summary do") and "Price list" in q and any("_web_" in x for x in calls[0]["schemas"])


def test_bare_domain_and_quoted_reply(make_bot, openwa, monkeypatch):
    import mcp_server
    seen = []
    monkeypatch.setattr(mcp_server, "read_web", lambda url: seen.append(url) or {"kind": "web", "name": "Callsaathi", "url": url, "text": "AI calling", "tables": {}})
    bot = make_bot()
    answer, calls = fake_answer("Callsaathi ek AI calling tool hai.")
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="BD1", body="Callsaathi.ai"))
    assert seen == ["https://Callsaathi.ai"] and calls[0]["question"].startswith("Is website ka short summary do")
    p = payload(mid="BD2", body="Isme bane me btao kiya hai ye bro")
    p["data"]["quotedMessage"] = {"id": "x", "body": "Callsaathi.ai"}
    bot.handle_payload(p)
    assert calls[1]["question"].startswith("Isme bane me btao kiya hai ye bro\n[Sawal is website ke baare mein hai: Callsaathi")


def test_quoted_answer_is_context(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    p = payload(mid="QA1", body="iska graph do")
    p["data"]["quotedMessage"] = {"id": "y", "body": "📊 Top 5 customers 2026"}
    bot.handle_payload(p)
    assert 'reply hai: "📊 Top 5 customers 2026"' in calls[0]["question"]


def test_repeated_refusal_is_short(workbook):
    from analyst import Conversation
    conv = Conversation(schemas={"x": {}}, history=[{"role": "assistant", "content": "long refusal", "kind": "out_of_scope"}])
    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx): return {"status": "out_of_scope", "message": "Main sirf data ... bahut lamba message with examples"}
    import analyst as A
    orig = A.OpenRouterAI; A.OpenRouterAI = AI
    try:
        r = A.answer(conv, "aur bhai kya hai", "k", "m", None)
    finally:
        A.OpenRouterAI = orig
    assert r.text.startswith("Main sirf aapke connected data") and "/help" in r.text


def test_private_url_is_blocked(make_bot, openwa):
    bot = make_bot()
    bot.handle_payload(payload(mid="WB2", body="http://localhost:8600/health"))
    assert "khol nahi paaya" in openwa.texts()[0] and "private" in openwa.texts()[0]


def test_database_credentials_in_chat_refused(make_bot, openwa, monkeypatch):
    bot = make_bot()
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="DB1", body="connect postgresql://admin:secret@db.example.com/erp"))
    assert calls == [] and "mat bhejo" in openwa.texts()[0] and "secret" not in openwa.texts()[0]


def test_database_from_env_is_a_source(tmp_path, make_bot, openwa, monkeypatch):
    import sqlite3
    db = tmp_path / "erp.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE invoices (customer TEXT, amount REAL)")
    con.executemany("INSERT INTO invoices VALUES (?, ?)", [("ABC", 100), ("XYZ", 250)])
    con.commit(); con.close()
    bot = make_bot(data_files=[], database_url=f"sqlite:///{db}")
    answer, calls = fake_answer()
    monkeypatch.setattr(botmod.analyst, "answer", answer)
    bot.handle_payload(payload(mid="DB2", body="/status"))
    assert "invoices (2 rows)" in openwa.texts()[0]
    bot.handle_payload(payload(mid="DB3", body="total amount?"))
    assert "database" in calls[0]["schemas"]
    top = bot.tools.call("aggregate_source", {"source_id": "database", "sheet_name": "invoices", "metric": "amount", "group_by": ["customer"], "sort": "desc", "top_n": 1})
    assert top["rows"] == [{"customer": "XYZ", "value": 250.0}]


# ---------- OpenWA media broken (engine returns 500): charts/CSVs go out as links ----------
def broken_media(openwa):
    def boom(*a, **k):
        raise OpenWAError("OpenWA 500: Internal server error", 500)
    openwa.send_image = boom
    openwa.send_document = boom


def test_chart_link_when_media_fails(make_bot, openwa, monkeypatch):
    bot = make_bot()
    bot.public_base = "https://bot.example.com"
    broken_media(openwa)
    df = pd.DataFrame({"CATEGORY": ["A", "B", "C"], "value": [3, 2, 1]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x", df=df, chart={"type": "bar", "x": "CATEGORY", "y": "value"}, want_chart=True)[0])
    bot.handle_payload(payload(mid="LK1", body="graph bana"))
    link_msg = openwa.texts()[1]
    url = re.search(r"https://bot\.example\.com/r/([0-9a-f]{32}\.png)", link_msg)
    assert url, link_msg
    data, mime = bot.shared.get(url.group(1))
    assert data[:4] == b"\x89PNG" and mime == "image/png"
    # second chart skips the failing media call (no 10s delay per message)
    calls = {"n": 0}
    def counting(*a, **k):
        calls["n"] += 1
        raise OpenWAError("x", 500)
    openwa.send_image = counting
    bot.handle_payload(payload(mid="LK2", body="graph bana"))
    assert calls["n"] == 0 and "/r/" in openwa.texts()[-1]


def test_csv_link_when_documents_fail(make_bot, openwa, monkeypatch):
    bot = make_bot()
    bot.public_base = "https://bot.example.com"
    broken_media(openwa)
    df = pd.DataFrame({"CUSTOMER": [f"C{i}" for i in range(25)], "value": range(25)})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("25", df=df, metric="AMOUNT")[0])
    bot.handle_payload(payload(mid="LK3", body="customer wise"))
    name = re.search(r"/r/([0-9a-f]{32}\.csv)", openwa.texts()[-1]).group(1)
    assert bot.shared.get(name)[0].decode("utf-8-sig").startswith("CUSTOMER,value")


def test_shared_route_and_learnt_public_url(make_bot, openwa, monkeypatch):
    bot = make_bot()
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer()[0])
    client = TestClient(whatsapp_server.build_app(bot=bot, executor=InlineExecutor(), secret="s" * 32))
    body = json.dumps(payload(mid="PU1")).encode()
    client.post("/webhook/openwa", content=body, headers={**signed(body), "Host": "abc.trycloudflare.com", "X-Forwarded-Proto": "https"})
    assert bot.public_base == "https://abc.trycloudflare.com"
    name = bot.shared.save(b"\x89PNGdata", "png")
    r = client.get(f"/r/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/r/../state.db").status_code == 404 and client.get("/r/" + "0" * 32 + ".png").status_code == 404


def test_detail_rows_are_compact():
    from whatsapp.format import result_list
    df = pd.DataFrame([{"VOUCHER DATE": "2026-04-04T00:00:00", "CUSTOMER NAME": "NARESH", "SALES PERSON": "AKHILESH", "STATE": "RJ", "CITY": "GANGANAGAR",
                        "ITEM NAME": "Code-1078", "GROUP 1": "Cat", "QTY": 50, "UNIT": "Set", "ALT_QTY": 0, "RATE": 150, "AMOUNT": 7500, "Party_Pan": "30 DAY"}] * 9)
    text, more = result_list(df, "AMOUNT")
    assert text.count("•") == 5 and more == 4
    assert "2026-04-04 |" in text and "AMOUNT: ₹7,500" in text and "Party_Pan" not in text and "T00:00:00" not in text


def test_strip_chart_claims_keeps_decimals():
    from analyst import strip_chart_claims
    t = "Electronics ₹63,000 hai, jo 58.93% hai. Yeh bar graph mein dikhaya gaya hai. Total ₹1,06,900."
    assert strip_chart_claims(t) == "Electronics ₹63,000 hai, jo 58.93% hai. Total ₹1,06,900."


# ---------- regressions from the real sheet (26-09-2026) ----------
@pytest.mark.parametrize("q,prev,expected", [
    ("Last 12 months ki sale kitni hai ?", None, None), ("last 6 month ka top customer", None, None), ("Mujhe best selling data chahiye", None, None),
    ("month wise sales dikhao", None, "month"), ("mahine ke hisaab se batao", None, "month"), ("monthly trend", None, "month"),
    ("2025 aur 2026 compare karo", None, "month"), ("sirf top 5", {"date_grain": "month"}, "month")])
def test_period_is_a_filter_not_a_split(q, prev, expected):
    plan = {"status": "execute", "mode": "data", "operation": "aggregate", "date_grain": "month", "date_column": "VOUCHER DATE", "group_by": []}
    assert analyst.sanitize_plan(plan, q, prev)["date_grain"] == expected


def test_grand_total_survives_top_n(workbook):
    srv = MCPServer()
    srv.register_file("wb", read_file("business.xlsx", workbook.read_bytes()))
    r = srv.call_tool("aggregate_source", {"source_id": "wb", "sheet_name": "SALES", "metric": "AMOUNT", "group_by": ["CUSTOMER"], "sort": "desc", "top_n": 2})
    assert len(r["rows"]) == 2 and r["grand_total_all_groups"] == 123900 and r["groups_total"] == 5


def test_complaint_reruns_previous_plan(workbook):
    srv = MCPServer()
    schema = srv.register_file("wb", read_file("business.xlsx", workbook.read_bytes()))
    bad = {"status": "execute", "mode": "data", "source_id": "wb", "sheet_name": "SALES", "operation": "aggregate", "metric": "AMOUNT",
           "aggregation": "sum", "group_by": [], "date_column": "VOUCHER DATE", "date_grain": "month", "date_from": "2026-01-01", "date_to": "2026-12-31"}
    conv = analyst.Conversation(schemas={"wb": schema}, last_plan=dict(bad), recent_plans=[{"question": "2026 ki sale kitni?", "plan": dict(bad)}])
    r = analyst.answer(conv, "ye galat hai", "no-llm-needed", "m", analyst.LocalTools(srv))
    assert "pichhla jawab galat tha" in r.text and "₹1,06,900" in r.text and conv.last_plan["date_grain"] is None
    r2 = analyst.answer(conv, "data not correct", "no-llm-needed", "m", analyst.LocalTools(srv))
    assert "dobara check" in r2.text and "₹1,06,900" in r2.text and "Rows counted: 6" in r2.text


def test_contains_with_full_real_name_is_exact():
    df = pd.DataFrame({"CUSTOMER NAME": ["DILIP JI", "SAMPLE   ( DILIP JI )", "DILIP JI"], "AMOUNT": [100, 7, 50],
                       "ITEM NAME": ["TELESCOPIC 18", "HINGE", "TELESCOPIC 20"]})
    srv = MCPServer()
    assert srv.aggregate_dataframe(df, "AMOUNT", filters=[{"column": "CUSTOMER NAME", "op": "contains", "value": "dilip ji"}])["rows"] == [{"value": 150}]
    assert srv.aggregate_dataframe(df, "ITEM NAME", aggregation="count", filters=[{"column": "ITEM NAME", "op": "contains", "value": "telescopic"}])["rows"] == [{"value": 2}]


def test_parse_json_tolerates_extra_output():
    from openrouter import OpenRouterAI
    assert OpenRouterAI.parse_json('{"status":"execute"}{"status":"x"}') == {"status": "execute"}


def test_vague_question_goes_to_just_added_website(monkeypatch):
    import analyst as A
    from analyst import Conversation
    srv = MCPServer()
    srv.sources["w"] = {"kind": "web", "name": "CallSaathi", "url": "https://x", "text": "CallSaathi is a call recording CRM.", "tables": {}}
    conv = Conversation(schemas={"w": srv.source_schema("w"), "s": {"kind": "workbook", "sheets": []}}, focus="w")
    class AI:
        def __init__(self, *a): pass
        def plan(self, q, ctx):
            assert ctx["just_connected_source"]["source_id"] == "w"
            return {"status": "out_of_scope", "message": "Main sirf sales..."}      # the LLM mistake we saw
        def answer_text(self, q, retrieved): return {"answer": "CallSaathi ek call recording CRM hai."}
    monkeypatch.setattr(A, "OpenRouterAI", AI)
    r = A.answer(conv, "ye kiya hia bro", "k", "m", A.LocalTools(srv))
    assert r.kind == "text" and "CallSaathi" in r.text


def test_website_explanations_are_text_not_table():
    schemas = {"w": {"kind": "web", "sheets": [{"name": "table_1"}]}, "s": {"kind": "workbook"}}
    row_plan = {"status": "execute", "mode": "data", "source_id": "w", "sheet_name": "table_1", "operation": "rows", "filters": [{"column": "Metric", "value": "Call Recording"}]}
    assert analyst.route_documents(dict(row_plan), "bhai mujhe call reoding chhaiye", schemas)["mode"] == "text"
    assert analyst.route_documents(dict(row_plan), "sabse sasta plan kaunsa hai", schemas)["mode"] == "data"
    sheet_plan = dict(row_plan, source_id="s")
    assert analyst.route_documents(dict(sheet_plan), "details do", schemas)["mode"] == "data"


def test_text_table_has_no_chart():
    from chart_ui import chartable
    assert not chartable(pd.DataFrame({"Metric": ["Call Recording"], "Cloud": ["Only cloud"], "CallSaathi": ["Every SIM call"]}))
    assert chartable(pd.DataFrame({"CITY": ["A", "B"], "value": [1, 2]}))


def test_page_images_are_sent(make_bot, openwa, monkeypatch):
    import mcp_server
    imgs = [{"alt": "CallSaathi dashboard - Agent Performance", "url": "https://x.ai/images/dash/agent.jpg"},
            {"alt": "Client logo", "url": "https://x.ai/images/logos/a.png"}]
    monkeypatch.setattr(mcp_server, "read_web", lambda url: {"kind": "web", "name": "CallSaathi – CRM", "url": url, "text": "reports", "tables": {}, "images": imgs})
    bot = make_bot(data_files=[])
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x")[0])
    bot.handle_payload(payload(mid="IM0", body="https://x.ai"))
    monkeypatch.undo()
    bot.handle_payload(payload(mid="IM1", body="report images dikhao"))
    sent = [s for s in openwa.sent if s["kind"] == "image_url"]
    assert [s["url"] for s in sent] == ["https://x.ai/images/dash/agent.jpg"] and "Agent Performance" in openwa.texts()[-1]


def test_share_after_videos_on_whatsapp(make_bot, openwa, monkeypatch):
    import mcp_server
    vids = [{"title": "How CallSaathi Works", "url": "https://youtu.be/E034Uy_tLpE"}, {"title": "Install the app", "url": "https://youtu.be/ynnbfOSwC1E"}]
    monkeypatch.setattr(mcp_server, "read_web", lambda url: {"kind": "web", "name": "CallSaathi – CRM", "url": url, "text": "x", "tables": {}, "images": [], "videos": vids})
    bot = make_bot(data_files=[])
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("x")[0])
    bot.handle_payload(payload(mid="V0", body="https://x.ai"))
    monkeypatch.undo()
    bot.handle_payload(payload(mid="V1", body="tutorials hai kya"))
    assert "youtu.be/E034Uy_tLpE" in openwa.texts()[-1]
    bot.handle_payload(payload(mid="V2", body="mujhe share kro"))
    assert openwa.texts()[-1].startswith("Ye rahe links") and "youtu.be/ynnbfOSwC1E" in openwa.texts()[-1]


def test_agent_reply_renders_on_whatsapp(make_bot, openwa, monkeypatch):
    bot = make_bot()
    df = pd.DataFrame({"ITEM NAME": ["A", "B", "C"], "AMOUNT": [100.5, 50, 25], "CLOSING STOCK": [3, 0, 9]})
    monkeypatch.setattr(botmod.analyst, "answer", fake_answer("Top 3 items aur unka stock: A 100.5 (stock 3), B 50 (0), C 25 (9).", df=df, kind="agent",
                                                              trace=["aggregate_data(x) → r1", "query_data(y) → r2", "join_results(k) → r3"])[0])
    bot.handle_payload(payload(mid="AG1", body="top items aur unka stock"))
    t = openwa.texts()[0]
    assert "Top 3 items" in t and "🔎 aggregate_data → query_data → join_results" in t and "CLOSING STOCK: 3" in t
