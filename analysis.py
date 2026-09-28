"""Deterministic executor for the understanding layer's analysis state.

state = {"source_id", "sheet_name", "date_column", "period": {"from","to","label"}, "grain": month|day|year|None,
         "group_by": [...], "metrics": [requested measures], "filters": [...], "top_n", "sort"}

execute_state() resolves every metric against the schema, runs one aggregate per metric through the (MCP) tools,
joins them on the keys, completes missing periods with zeros, sorts chronologically, names the columns, and returns
a Reply whose text is written by code (so it can only describe what was computed). No LLM involved.
"""
import io
import re
from datetime import date

import pandas as pd

from periods import describe as describe_period, period_metadata
from semantics import AmbiguousMeasure, column_kind, resolve_measures, score_tables, stem, tokens


def total_key(sid, sheet, column, aggregation, date_from, date_to, filters):
    """Identity of one total: same source, column, aggregation, dates and filters must always give the same number."""
    import json
    return json.dumps([sid, sheet, column, aggregation, date_from or "", date_to or "", sorted(json.dumps(f, sort_keys=True, default=str) for f in filters or [])])


def reconcile(conv, key, value, recompute=None, label=""):
    """Consistency check across turns: a scalar for a period must equal the sum of that period's series (and vice versa).
    On a mismatch the number is recomputed once; if it still differs the data changed — say so instead of showing two
    conflicting answers silently. Returns a note (or None) and stores the value."""
    totals = getattr(conv, "totals", None)
    if totals is None:
        return None
    old = totals.get(key)
    note = None
    if old is not None and value is not None and abs(float(old) - float(value)) > 0.005 * max(abs(float(old)), abs(float(value)), 1.0):
        fresh = recompute() if recompute else value
        if abs(float(old) - float(fresh)) > 0.005 * max(abs(float(old)), abs(float(fresh)), 1.0):
            from analyst import _fmt
            note = f"⚠️ Pehle isi period ke liye {_fmt(old, label)} aaya tha; ab data mein {_fmt(fresh, label)} hai — source update hua lagta hai. Latest number diya hai."
        value = fresh
    totals[key] = value
    return note


def table_of_state(schema, sheet_name):
    tables = schema.get("sheets") or []
    if tables:
        return next((t for t in tables if t.get("name") == sheet_name), None)
    return {"name": None, "columns": schema.get("columns", [])} if schema.get("columns") else None


def pick_table(question, schema):
    """The one table a question refers to, or None when several fit equally."""
    tables = schema.get("sheets") or []
    if not tables:
        return None if not schema.get("columns") else None
    if len(tables) == 1:
        return tables[0]["name"]
    scores = score_tables(question, schema)
    best = max(scores.values())
    winners = [n for n, s in scores.items() if s == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


def pick_date_column(columns):
    dates = [c["name"] for c in columns if c.get("role") == "date"]
    return dates[0] if len(dates) == 1 else (None if not dates else dates)


def complete_periods(df, keys, grain, start, end):
    """Every period in [start, end] appears (zeros for missing ones), so 'last 12 months' really has 12 rows."""
    if "period" not in keys or not start or not end or grain not in ("month", "day", "year"):
        return df
    freq = {"month": "M", "day": "D", "year": "Y"}[grain]
    fmt = {"month": "%Y-%m", "day": "%Y-%m-%d", "year": "%Y"}[grain]
    periods = pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq=freq).strftime(fmt)
    other = [k for k in keys if k != "period"]
    if other:
        combos = df[other].drop_duplicates() if len(df) else pd.DataFrame(columns=other)
        full = combos.merge(pd.DataFrame({"period": periods}), how="cross") if len(combos) else pd.DataFrame(columns=keys)
    else:
        full = pd.DataFrame({"period": periods})
    out = full.merge(df, on=keys, how="left")
    for c in out.columns:
        if c not in keys:
            out[c] = out[c].fillna(0)
            if pd.api.types.is_numeric_dtype(out[c]) and (out[c] % 1 == 0).all():
                out[c] = out[c].astype("int64")            # counts stay whole numbers (154, not 154.0)
    return out


def execute_state(conv, state, tools, question="", log=None):
    from analyst import Reply, _ddmmyyyy, _fmt
    log = log or (lambda *a, **k: None)
    sid = state.get("source_id")
    schema = conv.schemas.get(sid) or {}
    table = table_of_state(schema, state.get("sheet_name"))
    if table is None:
        return Reply("Kaunsi sheet/table se nikalna hai, ye clear nahi hai.", kind="clarify")
    cols = table.get("columns", [])
    choices = dict(state.get("choices") or {})
    from memory import get_context
    for mp in get_context(source_ids=[sid]).get("mappings", []):      # column choices the user made earlier for this source
        if mp.get("sheet") in (None, table.get("name")) and mp.get("term") and mp.get("column"):
            choices.setdefault(mp["term"], mp["column"])
    try:
        measures = resolve_measures(state.get("metrics") or [], cols, choices)
        for req, m in zip(state.get("metrics") or [], measures):
            if req.get("aggregation") in ("avg", "max", "min") and m["aggregation"] == "sum":
                m["aggregation"] = req["aggregation"]
                m["label"] = f"{'Average' if req['aggregation'] == 'avg' else req['aggregation'].title()} {m['column']}" if len(measures) > 1 else m["column"]
    except AmbiguousMeasure as e:
        r = Reply(f"❓ {e}" + (f"\nOptions: " + " · ".join(f"{i}. {c}" for i, c in enumerate(e.candidates, 1)) if e.candidates else ""), kind="clarify", options=list(e.candidates))
        r._pending = {"key": e.key, "candidates": list(e.candidates), "state": dict(state), "question": question, "source_id": sid, "sheet_name": table.get("name")}
        return r
    if not measures:
        return Reply("Kya nikalna hai — amount, quantity, count…? Ek metric batao.", kind="clarify")
    period, grain = state.get("period") or {}, state.get("grain")
    keys = (["period"] if grain else []) + list(state.get("group_by") or [])
    date_col = state.get("date_column")
    if (period or grain) and not date_col and choices.get("date_column") in {c["name"] for c in cols}:
        date_col = state["date_column"] = choices["date_column"]
    if (period or grain) and not date_col:
        dates = [c["name"] for c in cols if c.get("role") == "date"]
        if not dates:
            return Reply("Is table mein koi date column nahi hai — time-wise analysis nahi ho sakta. (Columns: " + ", ".join(c["name"] for c in cols) + ")", kind="clarify")
        r = Reply("❓ Date ke liye kaunsa column lein — " + " ya ".join(dates) + "?\nOptions: " + " · ".join(f"{i}. {c}" for i, c in enumerate(dates, 1)), kind="clarify", options=dates)
        r._pending = {"key": "date_column", "candidates": dates, "state": dict(state), "question": question, "source_id": sid, "sheet_name": table.get("name")}
        return r
    common = {"source_id": sid, "sheet_name": table.get("name"), "filters": state.get("filters") or []}
    if date_col and (period or grain):
        common.update({"date_column": date_col, **({"date_grain": grain} if grain else {}), **({"date_from": period["from"]} if period.get("from") else {}), **({"date_to": period["to"]} if period.get("to") else {})})
    if state.get("group_by"):
        common["group_by"] = list(state["group_by"])
    concise, explain = bool(state.get("concise")), bool(state.get("explain_period"))
    state = {k: v for k, v in state.items() if k not in ("concise", "explain_period")}     # transient flags, not context
    asks = [a for a in (period.get("asks") or []) if a.get("from") or a.get("to")] if not grain and not keys else []
    if len(asks) >= 2 and date_col:
        return _execute_asks(conv, state, tools, question, log, sid, table, measures, common, asks, period, date_col, concise, explain)
    merged, calls = None, []
    for m in measures:
        res = tools.call("aggregate_source", {k: v for k, v in common.items() if v not in (None, [], "")} | {"metric": m["column"], "aggregation": m["aggregation"]})
        calls.append(f"{m['aggregation']}({m['column']})" + (f" by {keys}" if keys else ""))
        df = pd.DataFrame(res.get("rows") or [])
        if df.empty:
            df = pd.DataFrame(columns=keys + ["value"]) if keys else pd.DataFrame([{"value": 0}])
        df = df.rename(columns={"value": m["label"]})[keys + [m["label"]]]
        merged = df if merged is None else (merged.merge(df, on=keys, how="outer") if keys else pd.concat([merged.reset_index(drop=True), df.reset_index(drop=True)], axis=1))
    trivial = [m for m in measures if m["aggregation"] == "count_distinct" and m["column"] in (state.get("group_by") or [])]
    if trivial and len(measures) > len(trivial):
        merged = merged.drop(columns=[m["label"] for m in trivial])
        table_measures = [m for m in measures if m not in trivial]
    else:
        table_measures = measures
    if grain:
        merged = complete_periods(merged, keys, grain, period.get("from"), period.get("to"))
    if state.get("top_n") and keys:
        by = table_measures[0]["label"]
        merged = merged.sort_values(by, ascending=(state.get("sort") == "asc")).head(int(state["top_n"]))
    elif keys:
        merged = merged.sort_values(keys, kind="stable")
    merged = merged.reset_index(drop=True)
    # Summary cards: sums over the whole range for sums; a true overall distinct count for count_distinct.
    cards = []
    for m in measures:
        if m["aggregation"] == "count_distinct" and keys:
            tot = tools.call("aggregate_source", {k: v for k, v in common.items() if v not in (None, [], "") and k not in ("group_by", "date_grain")} | {"metric": m["column"], "aggregation": "count_distinct"})
            val = (tot.get("rows") or [{}])[0].get("value")
            cards.append({"label": m["label"], "value": val, "note": "unique over the whole range"})
        else:
            val = float(pd.to_numeric(merged[m["label"]], errors="coerce").sum()) if len(merged) else 0.0
            cards.append({"label": m["label"], "value": val, "note": "total"})
    labels = ", ".join(m["label"] for m in table_measures)
    span = f"{_ddmmyyyy(period.get('from'))} → {_ddmmyyyy(period.get('to'))}" if period.get("from") or period.get("to") else ""
    how = "; ".join(f"{m['label']} = {'unique count of' if m['aggregation'] == 'count_distinct' else m['aggregation'].upper() + ' of'} {m['column']}" for m in measures)
    if keys and not len(merged):
        text = f"{(grain or ', '.join(keys)).title()}-wise: {labels}" + (f" ({period.get('label') or span})" if span else "") + " — is range mein koi data nahi mila.\nCalculation: " + how + "."
    elif keys:
        text = (f"{(grain or ', '.join(keys)).title()}-wise: {labels}" + (f" ({period.get('label') or span})" if span else "") + f" — {len(merged)} rows.\n" + f"Calculation: {how}.")
        if len(merged) and not state.get("top_n"):
            m0 = table_measures[0]
            best = merged.iloc[int(pd.to_numeric(merged[m0['label']], errors='coerce').fillna(0).idxmax())]
            text += f"\nSabse zyada {m0['label']}: {best[keys[0]]} ({_fmt(float(best[m0['label']]), m0['column'])})."
    else:
        vals = ", ".join(f"{m['label']} {_fmt(float(merged[m['label']].iloc[0]), m['column']) if len(merged) else 0}" for m in measures)
        head = (period.get("label") or "Total").title() if period.get("label") else (span or "Total")
        if period.get("label") and span:
            head += f" ({span})"
        text = f"{head}: {vals}." if concise else f"{head}: {vals}.\nCalculation: {how}."
    if explain and period:
        text += "\n" + describe_period(period, _ddmmyyyy)
    if not keys and len(merged) and len(measures) == 1:
        m0 = measures[0]
        key = total_key(sid, table.get("name"), m0["column"], m0["aggregation"], period.get("from"), period.get("to"), state.get("filters"))
        args = {k: v for k, v in common.items() if v not in (None, [], "")} | {"metric": m0["column"], "aggregation": m0["aggregation"]}
        note = reconcile(conv, key, float(merged[m0["label"]].iloc[0]), lambda: float((tools.call("aggregate_source", args).get("rows") or [{}])[0].get("value") or 0), m0["column"])
        if note:
            text += "\n" + note
    elif "period" in keys and len(measures) == 1 and measures[0]["aggregation"] == "sum" and not state.get("top_n") and len(keys) == 1:
        m0 = measures[0]      # the series total for this period must match the scalar for the same period
        key = total_key(sid, table.get("name"), m0["column"], m0["aggregation"], period.get("from"), period.get("to"), state.get("filters"))
        reconcile(conv, key, float(pd.to_numeric(merged[m0["label"]], errors="coerce").sum()), None, m0["column"])
    plan = {"status": "execute", "mode": "data", "operation": "multi_metric", "source_id": sid, "sheet_name": table.get("name"), "metric": measures[0]["column"],
            "aggregation": measures[0]["aggregation"], "metrics": measures, "group_by": list(state.get("group_by") or []), "date_column": date_col if (period or grain) else None,
            "date_grain": grain, "date_from": period.get("from"), "date_to": period.get("to"), "filters": state.get("filters") or [], "top_n": state.get("top_n"), "sort": state.get("sort"),
            "period": period_metadata(period), "title": f"{(grain or (', '.join(keys) if keys else period.get('label') or 'Total')).title()} — {labels}"}
    conv.state = dict(state, metrics=state.get("metrics") or [], resolved=measures, sheet_name=table.get("name"), date_column=date_col)
    conv.last_plan = plan
    conv.recent_plans = (conv.recent_plans + [{"question": question, "plan": plan}])[-5:]
    log("ai_done", tools=calls, transport=getattr(tools, "transport", "?"), source=sid, sheet=table.get("name"))
    if not keys:
        return Reply(text, kind="answer", df=None, metric=measures[0]["column"], plan=plan, value=float(merged[measures[0]["label"]].iloc[0]) if len(merged) and len(measures) == 1 else None, cards=cards)
    chart = {"type": "line" if "period" in keys else "bar", "x": keys[0], "y": table_measures[0]["label"]} if len(merged) >= 2 else None
    from pivot import describe, pivot_table, should_pivot
    if should_pivot(keys, table_measures, merged, question) and not state.get("top_n"):
        wide = pivot_table(merged, keys, table_measures[0]["label"])
        text = describe(keys, table_measures[0]["label"], len(wide) - 1, len(wide.columns) - 2) + (f" Period: {period.get('label') or span}." if span else "") + f"\nCalculation: {how}."
        if explain and period:
            text += "\n" + describe_period(period, _ddmmyyyy)
        return Reply(text, kind="answer", df=wide, chart=None, metric=table_measures[0]["column"], plan=plan, cards=cards, pivot=True, long_df=merged)
    return Reply(text, kind="answer", df=merged, chart=chart, metric=table_measures[0]["column"], plan=plan, cards=cards, drillable="period" if "period" in keys else None)


def _execute_asks(conv, state, tools, question, log, sid, table, measures, common, asks, period, date_col, concise, explain):
    """A compound period request ("August + September ka total aur August ka total"): one total per asked period,
    every number computed by the tools, no table, one card per ask."""
    from analyst import Reply, _ddmmyyyy, _fmt
    cards, lines, calls = [], [], []
    for a in asks:
        args = {k: v for k, v in common.items() if v not in (None, [], "") and k not in ("date_from", "date_to")} | {"date_column": date_col, "date_from": a["from"], "date_to": a["to"]}
        vals = []
        for m in measures:
            res = tools.call("aggregate_source", args | {"metric": m["column"], "aggregation": m["aggregation"]})
            calls.append(f"{m['aggregation']}({m['column']}) {a['from']}→{a['to']}")
            v = float((res.get("rows") or [{}])[0].get("value") or 0)
            vals.append((m, v))
            cards.append({"label": a["label"] if len(measures) == 1 else f"{a['label']} — {m['label']}", "value": v, "note": f"{_ddmmyyyy(a['from'])} → {_ddmmyyyy(a['to'])}"})
            key = total_key(sid, table.get("name"), m["column"], m["aggregation"], a["from"], a["to"], state.get("filters"))
            reconcile(conv, key, v, None, m["column"])
        lines.append(f"• {a['label']} ({_ddmmyyyy(a['from'])} → {_ddmmyyyy(a['to'])}): " + ", ".join(f"{m['label']} {_fmt(v, m['column'])}" if len(measures) > 1 else _fmt(v, m["column"]) for m, v in vals))
    how = "; ".join(f"{m['label']} = {'unique count of' if m['aggregation'] == 'count_distinct' else m['aggregation'].upper() + ' of'} {m['column']}" for m in measures)
    text = "\n".join(lines) + ("" if concise else f"\nCalculation: {how}.")
    if explain:
        text += "\n" + describe_period(period, _ddmmyyyy)
    plan = {"status": "execute", "mode": "data", "operation": "multi_metric", "source_id": sid, "sheet_name": table.get("name"), "metric": measures[0]["column"],
            "aggregation": measures[0]["aggregation"], "metrics": measures, "group_by": [], "date_column": date_col, "date_grain": None,
            "date_from": period.get("from"), "date_to": period.get("to"), "filters": state.get("filters") or [], "top_n": None, "sort": None,
            "period": period_metadata(period), "asks": asks, "title": " / ".join(a["label"] for a in asks) + f" — {', '.join(m['label'] for m in measures)}"}
    conv.state = dict(state, metrics=state.get("metrics") or [], resolved=measures, sheet_name=table.get("name"), date_column=date_col)
    conv.last_plan = plan
    conv.recent_plans = (conv.recent_plans + [{"question": question, "plan": plan}])[-5:]
    log("ai_done", tools=calls, transport=getattr(tools, "transport", "?"), source=sid, sheet=table.get("name"))
    return Reply(text, kind="answer", df=None, metric=measures[0]["column"], plan=plan, value=None, cards=cards)


def explain_state(conv):
    """'kaunsi date use ki?' / 'kaise nikala?' for a state-driven answer — from the state, no LLM."""
    from analyst import Reply, _ddmmyyyy
    st = conv.state or {}
    lines = ["🔍 Ye aise nikla:", f"• Sheet/table: {st.get('sheet_name') or st.get('source_id')}"]
    for m in st.get("resolved") or []:
        lines.append(f"• {m['label']}: {'unique count of' if m['aggregation'] == 'count_distinct' else m['aggregation'].upper() + ' of'} column *{m['column']}*")
    p = st.get("period") or {}
    if st.get("date_column"):
        lines.append(f"• Date column: {st['date_column']}")
    if p.get("from") or p.get("to"):
        lines.append(f"• Date range: {_ddmmyyyy(p.get('from'))} → {_ddmmyyyy(p.get('to'))}" + (f" ({p.get('label')})" if p.get("label") else ""))
    if st.get("grain"):
        lines.append(f"• Breakdown: {st['grain']} wise" + (", zero wale periods bhi shamil" if st.get("grain") else ""))
    if st.get("group_by"):
        lines.append(f"• Grouped by: {', '.join(st['group_by'])}")
    for f in st.get("filters") or []:
        lines.append(f"• Filter: {f.get('column')} {f.get('op') or '='} {f.get('value')}")
    return Reply("\n".join(lines), kind="answer", plan=conv.last_plan)


def build_report(conv, last_df, cards=None):
    """Excel report from the validated result: Summary sheet (title, source, range, calculation, totals) + Data sheet.
    Returns (filename, bytes). A chart PNG is added by the caller when available."""
    st = conv.state or {}
    plan = conv.last_plan or {}
    title = plan.get("title") or "Report"
    p = st.get("period") or {}
    rows = [["Report", title], ["Generated", date.today().isoformat()], ["Source", st.get("sheet_name") or st.get("source_id") or plan.get("source_id")],
            ["Date column", st.get("date_column") or plan.get("date_column") or "-"], ["Date range", f"{p.get('from') or plan.get('date_from') or '-'} → {p.get('to') or plan.get('date_to') or '-'}"]]
    for m in st.get("resolved") or plan.get("metrics") or []:
        rows.append([m["label"], f"{m['aggregation']} of {m['column']}"])
    for c in cards or []:
        rows.append([f"Total {c['label']}", c["value"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(rows, columns=["Field", "Value"]).to_excel(w, index=False, sheet_name="Summary")
        (last_df if last_df is not None else pd.DataFrame()).to_excel(w, index=False, sheet_name="Data")
    name = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:50] or "report"
    return f"{name}.xlsx", buf.getvalue()
