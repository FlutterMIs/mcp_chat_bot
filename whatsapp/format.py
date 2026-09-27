"""Turn an analyst Reply into short WhatsApp text: WhatsApp markup, Indian number format, numbered lists."""
import re

import pandas as pd

LIST_LIMIT = 10
MONEY_WORDS = ("amount", "sales", "revenue", "value", "price", "net", "total")


def indian(n):
    """1250000 -> 12,50,000 ; 96273311.63 -> 9,62,73,311.63"""
    neg, n = n < 0, abs(float(n))
    whole, frac = f"{n:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head)) if head else ""
    s = (head + "," + tail) if head else tail
    return ("-" if neg else "") + s + ("" if frac == "00" else "." + frac)


DETAIL_LIMIT = 5
DETAIL_COLS = 6
# One column per slot, in this priority: when, who, what, how much, how many, where.
_SLOTS = [("date",), ("customer", "party name", "client", "buyer"), ("item", "product"), ("amount", "total", "value", "net"),
          ("qty", "quantity"), ("city", "category", "state")]


def detail_columns(df):
    """For raw rows on a phone screen: the few columns people read (date, who, what, how much)."""
    cols = [c for c in df.columns if df[c].notna().any() and not (df[c].astype(str).str.strip() == "").all()]
    if len(cols) <= DETAIL_COLS:      # small tables (joins, computed results) are shown whole
        return cols
    keep = []
    for words in _SLOTS:
        hit = next((c for w in words for c in cols if w in str(c).lower() and c not in keep and "alt" not in str(c).lower()), None)
        if hit:
            keep.append(hit)
    keep = keep or cols[:DETAIL_COLS]
    return [c for c in df.columns if c in keep[:DETAIL_COLS]]


def _cell(v, money):
    s = fmt_value(v, money)
    return re.sub(r"^(\d{4}-\d{2}-\d{2})[T ]00:00:00(\.0+)?$", r"\1", s)


def fmt_value(v, money):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    return ("₹" if money else "") + indian(v)


def to_whatsapp(md):
    """Markdown -> WhatsApp markup (*bold*, _italic_), no headings or tables."""
    s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", md or "")
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*]\s+", "• ", s, flags=re.M)
    return s.strip()


def is_money(metric):
    return bool(metric) and any(w in str(metric).lower() for w in MONEY_WORDS)


def result_list(df, metric, limit=LIST_LIMIT):
    """Numbered list for grouped results (label columns + value); bullet rows for detail rows."""
    if df is None or df.empty:
        return "", 0
    money = is_money(metric)
    # A label column with one value in every row (e.g. period "2026" on a customer-wise list) is noise.
    if len(df) > 1 and "value" in df.columns:
        df = df[[c for c in df.columns if c == "value" or df[c].nunique(dropna=False) > 1]]
    rows = df.head(limit).to_dict(orient="records")
    lines = []
    if "value" in df.columns:
        rows = df.rename(columns={"value": metric}).head(limit).to_dict(orient="records") if metric and metric not in df.columns else rows
        for i, r in enumerate(rows, 1):
            r = {("value" if k == metric else k): v for k, v in r.items()}
            label = " · ".join(str(v) for k, v in r.items() if k != "value" and v is not None)
            lines.append(f"{i}. {label} — {fmt_value(r['value'], money)}" if label else fmt_value(r["value"], money))
    else:
        cols = detail_columns(df)
        for r in df.head(min(limit, DETAIL_LIMIT)).to_dict(orient="records"):
            parts = [f"{k}: {_cell(r[k], is_money(k))}" for k in cols if r.get(k) is not None and not (isinstance(r[k], float) and pd.isna(r[k]))]
            lines.append("• " + " | ".join(parts))
        return "\n".join(lines), len(df) - min(len(df), limit, DETAIL_LIMIT)
    return "\n".join(lines), len(df) - len(rows)


def _date(d):
    """2026-01-01 -> 01-01-2026 (how Indian users read dates)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    return f"{m[3]}-{m[2]}-{m[1]}" if m else str(d)


def context_footer(plan):
    """One italic line showing exactly which data answered: sheet · metric · filters · dates."""
    if not plan or plan.get("mode") == "text" or plan.get("status") != "execute":
        return ""
    parts = [plan.get("sheet_name")]
    if plan.get("operation") == "aggregate":
        agg = (plan.get("aggregation") or "sum").lower()
        parts.append(f"{plan.get('metric')}" + ("" if agg == "sum" else f" ({agg})"))
        if plan.get("group_by"):
            parts.append(", ".join(plan["group_by"]) + " wise")
    for f in plan.get("filters") or []:
        op = {"eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "contains": "~", "in": "∈", "not_in": "∉"}.get(f.get("op") or "eq", "=")
        val = ", ".join(map(str, f["value"])) if isinstance(f.get("value"), list) else f.get("value")
        parts.append(f"{f.get('column')} {op} {val}")
    if plan.get("date_from") or plan.get("date_to"):
        parts.append(f"{_date(plan.get('date_from')) or '…'} → {_date(plan.get('date_to')) or '…'}")
    parts = [str(p) for p in parts if p]
    return f"_📁 {' · '.join(parts)}_" if parts else ""


def suggestions(plan, n_rows):
    """Up to two short next questions, so the chat feels like a conversation."""
    if not plan or plan.get("status") != "execute" or plan.get("mode") == "text" or plan.get("want_chart"):
        return ""
    if plan.get("operation") == "aggregate" and not plan.get("group_by") and not plan.get("date_grain") in ("month", "day"):
        tips = ["month wise dikhao"] if plan.get("date_column") else []
    elif plan.get("operation") == "aggregate" and n_rows >= 2:
        tips = ["iska graph bana do"] + ([] if plan.get("top_n") else ["sirf top 5"])
    else:
        return ""
    if not tips:
        return ""
    return "💡 Aage pucho: " + " · ".join(f"\"{t}\"" for t in tips)


def _drop_enumeration(text, df, metric):
    """The list below already has every row; if the LLM text repeats them, keep a one-line headline instead."""
    labels = [c for c in df.columns if c != "value" and df[c].nunique() > 1]
    if "value" not in df.columns or not labels:
        return text
    names = df[labels[0]].astype(str).head(LIST_LIMIT)
    if sum(1 for n in names if n and n in text) < 3:
        return text
    money = is_money(metric)
    vals = pd.to_numeric(df["value"], errors="coerce")
    top = df.loc[vals.idxmax()]
    line = f"🏆 Sabse upar: *{top[labels[0]]}* — {fmt_value(float(vals.max()), money)}"
    return line + (f" · Inka kul: {fmt_value(float(vals.sum()), money)}" if len(df) > 1 else "")


def compose(reply, heard=None):
    """Final WhatsApp text for a Reply. Returns (text, rest) — rest is the part of the list that did not fit."""
    text = to_whatsapp(reply.text)
    title = (reply.plan or {}).get("title")
    has_rows = reply.df is not None and not reply.df.empty
    head = (f"🎤 _\"{heard}\"_\n\n" if heard else "") + (f"📊 *{title}*\n\n" if title and has_rows else "")
    listing, more = ("", 0)
    if has_rows and len(reply.df) >= 3:
        listing, more = result_list(reply.df, reply.metric)
        text = _drop_enumeration(text, reply.df, reply.metric)
    body = head + text + (f"\n\n{listing}" if listing else "")
    if more:
        body += f"\n…aur {more} rows."
    trace = f"_🔎 {' → '.join(t.split('(')[0] for t in reply.trace[:6])}_" if getattr(reply, "trace", None) else ""
    tail = [t for t in (trace or context_footer(reply.plan), suggestions(reply.plan, len(reply.df) if has_rows else 0)) if t]
    if tail:
        body += "\n\n" + "\n".join(tail)
    return body[:4000], more


def rest_as_text(df, metric, start, limit=60):
    """Rows after the first LIST_LIMIT, as text — the fallback when a CSV can't be sent."""
    part = df.iloc[start:start + limit]
    lines, _ = result_list(part.reset_index(drop=True), metric, limit=limit)
    lines = "\n".join(f"{start + i + 1}.{l.split('.', 1)[1]}" if l[:1].isdigit() else l for i, l in enumerate(lines.splitlines()))
    left = len(df) - start - len(part)
    return lines + (f"\n…aur {left} rows (poori list web app se download karo)." if left > 0 else "")


HELP = ("🤖 *Business Analyst*\n\n"
        "Apne business data ke baare mein normal language mein pucho — text ya voice note:\n"
        "• 2026 mein total sales kitni hui?\n• customer wise dikhao\n• top 5 ka graph bana do\n• 2025 aur 2026 compare karo\n\n"
        "Graph apne aap aata hai; type chahiye to bolo: \"pie chart mein\", \"line graph\", \"bar chart\".\n\n"
        "Data jodne ke liye: Excel/CSV/PDF bhejo, ya Google Sheet / website ka link bhejo.\n\n"
        "*Commands*\n/help — ye message\n/status — kaunsa data connected hai\n/link <code> — web app ke workspace se jodo (Settings → Generate link code)\n/reset — is chat ki baat-cheet bhool jao (data safe rehta hai)")
