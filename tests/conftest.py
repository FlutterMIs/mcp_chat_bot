import base64
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyst import LocalTools                     # noqa: E402
from mcp_server import MCPServer                  # noqa: E402
from whatsapp.bot import Config, WhatsAppBot      # noqa: E402
from whatsapp.store import Store                  # noqa: E402

SESSION = "new-testing"
BOT_NUMBER = "919990930426"
USER = "919999900001@c.us"

SALES = pd.DataFrame([
    # VOUCHER DATE is DD/MM/YYYY like Indian Tally exports
    ["05/01/2025", "ABC Traders", "Ravi", "Electronics", 2, 2, 5000, 10000],
    ["18/06/2025", "XYZ Ltd", "Sunita", "Furniture", 1, 1, 7000, 7000],
    ["10/01/2026", "ABC Traders", "Ravi", "Electronics", 3, 3, 15000, 45000],
    ["12/02/2026", "XYZ Ltd", "Sunita", "Furniture", 4, 4, 5500, 22000],
    ["03/03/2026", "PQR Corp", "Ravi", "Electronics", 1, 1, 18000, 18000],
    ["20/04/2026", "Demo Traders", "Amit", "Clothing", 10, 10, 640, 6400],
    ["11/05/2026", "ABC Traders", "Amit", "Furniture", 2, 2, 5500, 11000],
    ["25/05/2026", "Test Industries", "Sunita", "Clothing", 5, 5, 900, 4500],
], columns=["VOUCHER DATE", "CUSTOMER", "SALESMAN", "CATEGORY", "QTY", "ALT_QTY", "RATE", "AMOUNT"])
# INVENTORY also has numeric AMOUNT (stock value) — a sales question must still pick SALES.
INVENTORY = pd.DataFrame([["Laptop", 40, 40, 1200000], ["Chair", 150, 150, 450000], ["Shirt", 500, 500, 250000]],
                         columns=["ITEM", "CLOSING STOCK", "QTY", "AMOUNT"])
MAIN = pd.DataFrame([["Company", "Demo Pvt Ltd"], ["FY", "2025-26"]], columns=["KEY", "VALUE"])

# Truth computed directly with pandas, independent of the app.
TOTAL_2026 = float(SALES[SALES["VOUCHER DATE"].str.endswith("2026")]["AMOUNT"].sum())   # 106900
TOP_CUSTOMER_2026 = ("ABC Traders", 56000.0)
MAX_TXN_2026 = 45000.0


@pytest.fixture
def workbook(tmp_path):
    p = tmp_path / "business.xlsx"
    with pd.ExcelWriter(p) as w:
        MAIN.to_excel(w, sheet_name="MAIN SHEET", index=False)
        INVENTORY.to_excel(w, sheet_name="INVENTORY", index=False)
        SALES.to_excel(w, sheet_name="SALES", index=False)
    return p


class FakeOpenWA:
    """Records every call; set fail_* to simulate OpenWA errors."""

    def __init__(self):
        self.sent, self.fail_send, self.media = [], None, {}

    def _rec(self, kind, chat_id, **kw):
        if self.fail_send:
            from whatsapp.openwa import OpenWAError
            raise OpenWAError(self.fail_send, 500)
        self.sent.append({"kind": kind, "chat": chat_id, **kw})
        return {"messageId": f"true_{chat_id}_{len(self.sent)}", "timestamp": 1}

    def get_session(self):
        return {"id": SESSION, "status": "ready", "phone": BOT_NUMBER, "engineLoaded": True}

    def send_text(self, chat_id, text):
        return self._rec("text", chat_id, text=text)

    def send_image(self, chat_id, png, caption=None, **kw):
        return self._rec("image", chat_id, png=png, caption=caption)

    def send_image_url(self, chat_id, url, caption=None):
        return self._rec("image_url", chat_id, url=url, caption=caption)

    def send_document(self, chat_id, data, filename, mimetype, caption=None):
        return self._rec("document", chat_id, data=data, filename=filename, mimetype=mimetype)

    def send_audio(self, chat_id, data, mimetype, ptt=False):
        return self._rec("audio", chat_id, data=data, mimetype=mimetype, ptt=ptt)

    def send_typing(self, chat_id, state="typing"):
        return None

    def download_media(self, chat_id, message_id):
        return self.media[message_id]

    lids = {}

    def resolve_phone(self, contact_id):
        return self.lids.get(contact_id)

    def convert_voice(self, data):
        return b"OggS" + data[:10], "audio/ogg; codecs=opus"

    def texts(self):
        return [s["text"] for s in self.sent if s["kind"] == "text"]


@pytest.fixture
def openwa():
    return FakeOpenWA()


@pytest.fixture
def make_bot(tmp_path, workbook, openwa):
    def build(**cfg_over):
        opts = dict(openrouter_key=os.getenv("OPENROUTER_API_KEY", "test-key"), session=SESSION, allowed_numbers={"*"},
                    data_files=[str(workbook)], files_dir=tmp_path / "files", shared_dir=tmp_path / "shared") | cfg_over
        cfg = Config(**opts)
        bot = WhatsAppBot(cfg, openwa, Store(tmp_path / "state.db"), LocalTools(MCPServer()))
        bot.warm_up()
        return bot
    return build


def payload(mid="MSG1", body="hi", type="text", from_me=False, chat=USER, media=None, event="message.received", is_group=False, author=None):
    data = {"id": mid, "from": chat, "to": f"{BOT_NUMBER}@c.us", "chatId": chat, "body": body, "type": type,
            "timestamp": 1790000000, "fromMe": from_me, "isGroup": is_group, "kind": "group" if is_group else "personal",
            "contact": {"pushName": "Tester", "number": chat.split("@")[0]}}
    if author:
        data["author"] = author
    if media:
        data["media"] = media
    return {"event": event, "timestamp": "2026-09-25T10:00:00.000Z", "sessionId": SESSION,
            "idempotencyKey": f"msg_{SESSION}_{mid}", "deliveryId": f"d-{mid}", "data": data}


def inline_media(data: bytes, mimetype, filename=None):
    m = {"mimetype": mimetype, "data": base64.b64encode(data).decode(), "sizeBytes": len(data)}
    if filename:
        m["filename"] = filename
    return m
