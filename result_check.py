"""Final gate before a data answer is shown: does the answer's plan match the connected schema and its own table?

Checked (all in code, no model): source connected · sheet exists · metric / group_by / filter / date columns exist in
that sheet · date range is ordered · the table has no NaN/inf in numeric cells · a scalar answer really carries a number.
Anything wrong → the reply is replaced by a clear message (never a fabricated figure). Text/document answers are not
touched here; the wording checks for those live in grounding.py."""
from __future__ import annotations

import math
import re

import pandas as pd

from mcp_server import resolve_column


def _table(schema, sheet):
    tables = schema.get("sheets") or ([{"name": None, "columns": schema.get("columns", [])}] if schema.get("columns") else [])
    if not tables:
        return None
    if sheet is None and len(tables) == 1:
        return tables[0]
    return next((t for t in tables if t.get("name") == sheet), None)


def _exists(name, columns):
    """The column, resolved like the tools resolve it (exact / normalised / alias), or None."""
    if not name:
        return None
    names = [c["name"] for c in columns]
    if name in names:
        return name
    return resolve_column(pd.DataFrame(columns=names), name)


def problems(reply, schemas):
    """List of human-readable problems; empty = OK. Only for data answers (plan with mode data)."""
    plan = reply.plan or {}
    if reply.kind not in ("answer", "agent") or not plan or plan.get("mode") == "text" or plan.get("operation") in ("agent", "search", None):
        return []
    out = []
    sid = plan.get("source_id")
    schema = schemas.get(sid)
    if schema is None:
        return [f"source '{sid}' is not connected"]
    if schema.get("kind") not in (None, "table", "workbook"):
        return []                                             # document table extractions are not re-checked here
    table = _table(schema, plan.get("sheet_name"))
    if table is None:
        return [f"sheet '{plan.get('sheet_name')}' not found in {sid}"]
    cols = table.get("columns", [])
    metrics = [m.get("column") for m in plan.get("metrics") or []] or ([plan.get("metric")] if plan.get("metric") else [])
    for m in metrics:
        if plan.get("operation") in ("aggregate", "multi_metric") and not _exists(m, cols) and not _exists(reply.metric, cols):
            out.append(f"metric '{m}' is not a column of {table.get('name') or sid}")
    for g in plan.get("group_by") or []:
        if not _exists(g, cols) and str(g).strip().lower() not in ("month", "year", "day", "date", "period"):
            out.append(f"group column '{g}' is not a column of {table.get('name') or sid}")
    for f in plan.get("filters") or []:
        if isinstance(f, dict) and f.get("column") and not _exists(f["column"], cols):
            out.append(f"filter column '{f['column']}' is not a column of {table.get('name') or sid}")
    if plan.get("date_column") and not _exists(plan["date_column"], cols):
        out.append(f"date column '{plan['date_column']}' is not a column of {table.get('name') or sid}")
    a, b = plan.get("date_from"), plan.get("date_to")
    if a and b and re.match(r"^\d{4}-\d{2}-\d{2}", str(a)) and re.match(r"^\d{4}-\d{2}-\d{2}", str(b)) and str(a)[:10] > str(b)[:10]:
        out.append(f"date range is reversed ({a} → {b})")
    # Ranking scope: "top N per month/state" must never show more than N rows per scope value
    df = reply.df
    scope, n = plan.get("rank_scope"), plan.get("top_n")
    if scope and n and df is not None and len(df) and scope in df.columns and not getattr(reply, "pivot", False):
        worst = int(df.groupby(scope).size().max())
        if worst > int(n):
            out.append(f"ranking scope '{scope}' shows {worst} rows for one value, more than top {n}")
    # Totals: the displayed total must equal the sum of the shown rows (the all-data total may differ, that is the point)
    totals = getattr(reply, "totals", None)
    if totals and df is not None and len(df):
        for col, v in (totals.get("displayed") or {}).items():
            src = col if col in df.columns else ("value" if "value" in df.columns else None)
            if src is not None:
                shown = float(pd.to_numeric(df[src], errors="coerce").sum())
                if abs(shown - float(v)) > 0.01 * max(abs(shown), 1):
                    out.append(f"displayed total for '{col}' does not match the table")
        if not totals.get("cut"):
            for col, v in (totals.get("all") or {}).items():
                d = (totals.get("displayed") or {}).get(col)
                if d is not None and abs(float(d) - float(v)) > 0.01 * max(abs(float(v)), 1):
                    out.append(f"grand total for '{col}' differs from the displayed rows although nothing was cut")
    if reply.value is not None and not (isinstance(reply.value, (int, float)) and math.isfinite(float(reply.value))):
        out.append("scalar answer has no finite value")
    df = reply.df
    if df is not None and len(df) and plan.get("operation") in ("aggregate", "multi_metric"):      # raw rows may legitimately have blanks
        for c in df.columns:
            if str(c).startswith("Previous ") or str(c) in ("Change", "Change %"):
                continue                                                  # the first period has no previous value by definition
            if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c]):
                s = pd.to_numeric(df[c], errors="coerce")
                if s.isna().any() or not s.map(lambda v: math.isfinite(float(v))).all():
                    out.append(f"table column '{c}' has empty/undefined numbers")
                    break
    return out


def enforce(reply, schemas, log=None):
    """Replace a failed reply with a clear error; return the reply unchanged otherwise."""
    probs = problems(reply, schemas)
    if not probs:
        return reply
    if log:
        log("result_check_failed", problems=probs[:4])
    from analyst import Reply
    return Reply("⚠️ Ye jawab validate nahi hua, isliye number nahi dikha raha: " + "; ".join(probs[:3]) +
                 ". Sawal ko thoda alag tarah se pucho (sheet/column ka naam likh ke).", kind="clarify", plan=reply.plan)
