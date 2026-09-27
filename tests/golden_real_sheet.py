"""Golden questions on the REAL connected Google Sheet. Expected values come from pandas, never from the LLM.

Run:  .venv/bin/python tests/golden_real_sheet.py      (needs OPENROUTER_API_KEY and WHATSAPP_GOOGLE_SHEET_URL/GOOGLE_SHEET_URL)
Exit code 1 if any question fails. Uses a throwaway memory file, so learned memory is untouched.
"""
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import memory  # noqa: E402
memory.MEMORY_FILE = Path(tempfile.mkdtemp()) / "memory.json"
import analyst  # noqa: E402
from analyst import Conversation, LocalTools  # noqa: E402
from mcp_server import MCPServer  # noqa: E402

KEY, MODEL = os.getenv("OPENROUTER_API_KEY"), os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
URL = os.getenv("WHATSAPP_GOOGLE_SHEET_URL") or os.getenv("GOOGLE_SHEET_URL")

srv = MCPServer()
schema = srv.register_google_sheet("google_sheet", URL)
S, INV = srv.sources["google_sheet"]["tabs"]["SALES"].copy(), srv.sources["google_sheet"]["tabs"]["INVENTORY"].copy()
S["AMT"] = pd.to_numeric(S["AMOUNT"], errors="coerce")
INV["CS"] = pd.to_numeric(INV["CLOSING STOCK"], errors="coerce")
today = pd.Timestamp(date.today())
last12 = S[(S["VOUCHER DATE"] >= today - pd.DateOffset(months=12)) & (S["VOUCHER DATE"] <= today + pd.Timedelta(days=1))]


def variants(df):
    """Sales with and without SALES PENDING — the counting rule is not decided yet, so both are accepted and reported."""
    return {"all": df, "sales_only": df[df["VOUCHER NAME"] == "Sales"]}


def nums(text):
    return {round(float(t.replace(",", "")), 2) for t in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def top(df, col, val="AMT"):
    g = df.groupby(col)[val].sum().sort_values(ascending=False)
    return g.index[0], round(float(g.iloc[0]), 2)


def check_total(r, truths):
    got = set(nums(r.text))
    if getattr(r, "value", None) is not None:
        got.add(round(float(r.value), 2))
    if r.df is not None and "value" in r.df.columns and len(r.df) == 1:
        got.add(round(float(r.df["value"].iloc[0]), 2))
    hit = [k for k, v in truths.items() if round(v, 2) in got or round(v) in got]
    return (bool(hit), hit[0] if hit else f"expected one of {truths}")


def check_top(r, truths, col):
    if r.df is None or r.df.empty or col not in r.df.columns:
        return False, f"no {col} table"
    first = (str(r.df.iloc[0][col]), round(float(r.df.iloc[0]["value"]), 2))
    hit = [k for k, v in truths.items() if (str(v[0]), v[1]) == first]
    return (bool(hit), hit[0] if hit else f"got {first}, expected {truths}")


def single(r):
    if getattr(r, "value", None) is not None:
        return float(r.value)
    return float(r.df["value"].iloc[0]) if r.df is not None and "value" in r.df.columns and len(r.df) == 1 else None


def no_rupee(check):
    """Inventory answers are quantities: a ₹ sign there is a wrong unit."""
    def run(r):
        ok, detail = check(r)
        return (ok and "₹" not in r.text, detail if "₹" not in r.text else "₹ on a quantity")
    return run


def v_total(df):
    return {k: float(d["AMT"].sum()) for k, d in variants(df).items()}


def v_top(df, col):
    return {k: top(d, col) for k, d in variants(df).items()}


sept = S[S["VOUCHER DATE"].dt.to_period("M") == pd.Period("2026-09")]
tel = INV["ITEM NAME"].str.contains("telescopic", case=False, na=False)
monthly = {k: len(d.groupby(d["VOUCHER DATE"].dt.to_period("M"))) for k, d in variants(last12).items()}

CASES = [
    # (name, [questions...], checker on the LAST reply)
    ("last 12 months total", ["Last 12 months ki sale kitni hai ?"], lambda r: check_total(r, v_total(last12))),
    ("2026 total", ["2026 mein total sales kitni hui?"], lambda r: check_total(r, v_total(S[S["VOUCHER DATE"].dt.year == 2026]))),
    ("september total", ["September 2026 ki sale kitni hai?"], lambda r: check_total(r, v_total(sept))),
    ("customer total", ["DILIP JI ne kitne ka maal kharida?"], lambda r: check_total(r, v_total(S[S["CUSTOMER NAME"] == "DILIP JI"]))),
    ("top customer", ["top 10 customers by sales"], lambda r: check_top(r, v_top(S, "CUSTOMER NAME"), "CUSTOMER NAME")),
    ("top item", ["sabse zyada bikne wala item kaunsa hai amount ke hisaab se?"], lambda r: check_top(r, v_top(S, "ITEM NAME"), "ITEM NAME")),
    ("top city", ["city wise sales batao"], lambda r: check_top(r, v_top(S, "CITY"), "CITY")),
    ("top salesperson", ["sales person wise sales"], lambda r: check_top(r, v_top(S, "SALES PERSON"), "SALES PERSON")),
    ("month wise rows", ["month wise sales dikhao last 12 months"],
     lambda r: (r.df is not None and len(r.df) in monthly.values(), f"{0 if r.df is None else len(r.df)} rows, expected {monthly}")),
    ("follow-up month wise", ["Last 12 months ki sale kitni hai ?", "month wise dikhao"],
     lambda r: (r.df is not None and len(r.df) in monthly.values(), f"{0 if r.df is None else len(r.df)} rows")),
    ("distinct customers", ["kitne customers hain?"],
     lambda r: check_total(r, {k: float(d["CUSTOMER NAME"].nunique()) for k, d in variants(S).items()})),
    ("total closing stock", ["total closing stock kitna hai?"], no_rupee(lambda r: check_total(r, {"inventory": float(INV["CS"].sum())}))),
    ("count telescopic", ["telescopic items kitne hain?"],
     lambda r: (single(r) == float(tel.sum()) or (r.df is not None and "distinct_count" not in r.df and len(r.df) == int(tel.sum()) and "value" not in r.df),
                f"single value {single(r)}, expected {int(tel.sum())}")),
    ("telescopic stock", ["telescopic item ka closing stock bta do"], no_rupee(lambda r: check_total(r, {"inventory": float(INV.loc[tel, "CS"].sum())}))),
    ("top stock item", ["sabse zyada stock kis item ka hai?"],
     lambda r: check_top(r, {"inventory": top(INV, "ITEM NAME", "CS")}, "ITEM NAME") if r.df is not None and "value" in r.df.columns
     else (INV.loc[INV["CS"].idxmax(), "ITEM NAME"] in r.text, "top item name not in answer")),
    ("complaint re-check", ["Last 12 months ki sale kitni hai ?", "ye galat hai"], lambda r: check_total(r, v_total(last12))),
    ("source question", ["telescopic item ka closing stock bta do", "ye abhi bhi sales mein se le rha hai"],
     lambda r: ("INVENTORY" in r.text, "answer does not say INVENTORY")),
    ("out of scope", ["India ka GDP kitna hai?"], lambda r: (r.kind == "out_of_scope" and r.df is None, r.kind)),
]


# Cross-sheet (agent) case: top items by AMOUNT from SALES joined with CLOSING STOCK from INVENTORY.
_top5 = S.groupby("ITEM NAME")["AMT"].sum().sort_values(ascending=False).head(5)
_stock = INV.set_index(INV["ITEM NAME"].str.strip().str.lower())["CS"]


def check_cross_sheet(r):
    text = r.text.replace(",", "")
    have = [it for it in _top5.index if it.split(",")[0].strip().lower() in r.text.lower() or (r.df is not None and any(str(it) == str(v) for v in r.df.astype(str).values.ravel()))]
    stocks = [float(_stock.get(it.strip().lower(), float("nan"))) for it in _top5.index]
    stock_hits = sum(1 for v in stocks if v == v and (f"{int(v)}" in text or (r.df is not None and any(str(int(v)) == str(x).split(".")[0] for x in r.df.astype(str).values.ravel()))))
    return (r.kind == "agent" and len(have) >= 3 and stock_hits >= 3, f"kind={r.kind} items={len(have)}/5 stocks={stock_hits}/5 steps={len(r.trace)}")


CASES.append(("agent: top items + stock", ["sabse zyada bikne wale 5 items (amount ke hisaab se) aur unka closing stock batao"], check_cross_sheet))

# Website questions (sheet + website connected together, website just added), exactly as users type them.
WEB_URL = "https://callsaathi.ai/"
try:
    web_schema = srv.register_web("web_callsaathi", WEB_URL)
except Exception as e:  # network down: report instead of silently skipping
    web_schema = None
    print(f"WARNING: could not load {WEB_URL}: {e}")


def web_ok(*must):
    def run(r):
        bad = [w for w in ("bri", "bro", "tujhe", "tu", "tera") if re.search(rf"\b{w}\b", r.text, re.I)] + (["starts with Bhai"] if r.text.lower().startswith("bhai") else [])
        miss = [m for m in must if m.lower() not in r.text.lower()]
        hinglish = len(re.findall(r"\b(hai|hain|ke|ki|ka|mein|aur|hota|hoti|karta|karti|se)\b", r.text, re.I)) >= 3
        ok = r.kind == "text" and r.df is None and not bad and not miss and hinglish
        return ok, f"kind={r.kind} table={r.df is not None} echoed={bad} missing={miss} hinglish={hinglish}"
    return run


WEB_CASES = [
    ("web: vague after adding", ["ye kiya hia bro"], web_ok("callsaathi")),
    ("web: call recording", ["bhai mujhe call reoding chhaiye"], web_ok("record")),
    ("web: whatsapp typo", ["bhai whastapp call kese recoin hoti hai bri"], web_ok("whatsapp", "record")),
    ("web: features", ["iske features kya hai"], web_ok("record")),
    ("web then sheet", ["ye kya hai", "last 12 months ki sale kitni hai"], lambda r: check_total(r, v_total(last12))),
]


# Only the website connected (no sheet) — the setup from the 26-09 screenshot.
WEB_ONLY_CASES = [
    ("web-only: dashboard", ["bhai mujhe btao dashboard report ke bare me"], web_ok("dashboard")),
    ("web-only: images", ["images bhi btai"], lambda r: (r.kind == "images" and len(r.images) > 0, f"{r.kind} images={len(r.images)}")),
    ("web-only: report list", ["bhai mujhe report btao kon kon hi hai isme"],
     lambda r: (sum(w in r.text.lower() for w in ("agent performance", "missed call", "heatmap", "hourly", "team status", "weekly", "monthly")) >= 3
                and not re.search(r"agents? ke naam", r.text, re.I), r.text[:90])),
    ("web-only: report images", ["report images"],
     lambda r: (r.kind == "images" and r.images and all("logo" not in i["url"] for i in r.images) and any("dashboard" in i["alt"].lower() for i in r.images),
                [i["alt"][:30] for i in r.images[:3]])),
    ("web-only: tutorials typo", ["tutiriios"], lambda r: (r.kind == "videos" and sum("youtu.be" in v["url"] for v in r.videos) >= 2, f"{r.kind} {len(r.videos)} videos")),
    ("web-only: videos hai", ["videos hai"], lambda r: (r.kind == "videos" and len(r.videos) >= 5 and "youtu.be" in r.text, f"{r.kind} {len(r.videos)}")),
    ("web-only: share videos", ["videos hai", "mujhe share kro"], lambda r: (r.kind == "links" and r.text.count("http") >= 5, r.text[:80])),
    ("web-only: install video", ["app install kaise kare video"], lambda r: (r.kind == "videos" and "install" in r.videos[0]["title"].lower(), [v["title"] for v in r.videos][:2])),
    ("web-only: hi", ["hi"], lambda r: (r.kind == "out_of_scope" and len(r.text) < 260, f"{r.kind} len={len(r.text)}")),
    ("web-only: pricing", ["iski pricing kya hai"], lambda r: (r.df is None and ("nahi" in r.text.lower() or "₹" in r.text), r.text[:80])),
]


def main():
    rows, failed = [], 0
    cases = [(n, q, c, "sheet") for n, q, c in CASES] + ([(n, q, c, "both") for n, q, c in WEB_CASES] + [(n, q, c, "web") for n, q, c in WEB_ONLY_CASES] if web_schema else [])
    for name, qs, check, setup in cases:
        with_web = setup != "sheet"
        schemas = ({"google_sheet": schema} if setup != "web" else {}) | ({"web_callsaathi": web_schema} if with_web else {})
        conv, tools = Conversation(schemas=schemas, focus="web_callsaathi" if with_web else None), LocalTools(srv)
        try:
            for q in qs:
                r = analyst.answer(conv, q, KEY, MODEL, tools)
            ok, detail = check(r)
        except Exception as e:
            ok, detail, r = False, f"{type(e).__name__}: {e}", None
        failed += not ok
        rows.append((("PASS" if ok else "FAIL"), name, detail, (r.text[:110].replace("\n", " ") if r else "")))
    for status, name, detail, text in rows:
        print(f"{status}  {name:22} {str(detail)[:70]:70}  | {text}")
    print(f"\n{len(rows) - failed}/{len(rows)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
