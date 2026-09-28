"""Response policy: one canonical FinalResponse per user message, decided in code.

The analyst (planner, agent, state executor) returns a Reply full of everything it knows — text, table, chart hint,
cards, single value, trace, plan. Rendering all of it for every answer is what makes simple questions look like
dashboards. `finalize()` reads the *shape* of the result and keeps only what helps:

    scalar    one number ("total sales kitni hai?")          → answer + one metric, no table, no chart
    series    period rows ("last 12 months …")                → answer + chart (chronological) + table (+ cards when 2+ measures)
    ranking   grouped/top-N rows ("top 5 customers")          → answer + bar chart + table
    breakdown grouped rows, many groups                       → answer + table (chart only if asked)
    detail    raw rows ("August details")                     → answer + table (capped), no chart, no metrics
    text      chat / clarification / help / report            → answer (+ chips, files)

Internal material (tool trace, plan, tool calls) goes to `debug` and is rendered only in debug mode. Both channels
(Streamlit and WhatsApp) render from this object and nothing else.
"""
from dataclasses import dataclass, field

import pandas as pd

DETAIL_MAX_ROWS = 100          # the UI shows at most this many raw rows; the rest is available as CSV/Excel
CHART_MAX_GROUPS = 40          # bars beyond this are unreadable → table only unless a chart was asked for


@dataclass
class FinalResponse:
    answer: str
    kind: str = "answer"                       # Reply.kind (answer, clarify, text, report, no_source, …)
    shape: str = "text"                        # scalar | series | ranking | breakdown | detail | text
    metrics: list = field(default_factory=list)   # [{"label", "value", "note"}] — at most a handful, never a duplicate of the table
    table: pd.DataFrame | None = None
    table_note: str | None = None              # "Showing 100 of 1,250 rows"
    chart: dict | None = None                  # {"type", "x", "y"} — only when the shape benefits from one
    source_info: dict = field(default_factory=dict)   # {"source", "sheet", "calculation", "date_column", "date_range"}
    drilldown: str | None = None               # column whose rows can be clicked for details
    options: list = field(default_factory=list)   # clarification chips
    files: list = field(default_factory=list)
    images: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    debug: dict = field(default_factory=dict)  # {"trace", "plan", "tool_calls"} — never rendered unless debug mode
    value: float | None = None                 # the one number of a scalar answer (raw, unformatted)
    date_range: str | None = None              # "2026-08-01 → 2026-09-27" — the resolved period the result covers
    period: dict | None = None                 # canonical period metadata (expression, reference date, periods)

    @property
    def answer_type(self):
        return self.shape

    @property
    def date_info(self):
        return {"date_range": self.date_range, **(self.period or {})} if (self.date_range or self.period) else None

    @property
    def tables(self):
        return [self.table] if self.table is not None else []

    @property
    def charts(self):
        return [self.chart] if self.chart else []

    @property
    def drilldown_options(self):
        return [self.drilldown] if self.drilldown else []


def _numeric_cols(df):
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def _label_cols(df):
    return [c for c in df.columns if c not in _numeric_cols(df)]


def shape_of(reply):
    """The kind of result this is, from the data itself plus the plan when there is one."""
    df = reply.df
    if reply.kind != "answer" or df is None or df.empty:
        return "scalar" if (reply.kind == "answer" and (reply.value is not None or reply.cards)) else "text"
    if len(df) == 1 and (len(df.columns) == 1 or reply.value is not None):
        return "scalar"                      # a stray `value` next to a real table does not shrink the table
    if "period" in df.columns and _numeric_cols(df):
        return "series"
    op = ((reply.plan or {}).get("operation") or "").lower()
    labels, nums = _label_cols(df), _numeric_cols(df)
    aggregated = op in ("aggregate", "multi_metric", "compare") or bool((reply.plan or {}).get("group_by")) or bool((reply.plan or {}).get("top_n"))
    if not aggregated and op in ("query", "rows", "search", "detail", "select"):
        return "detail"
    if nums and labels and len(df) <= CHART_MAX_GROUPS and not df[labels[0]].duplicated().any() and len(labels) <= 2:
        return "ranking" if (aggregated or len(df) <= 15) else "breakdown"
    if nums and labels and aggregated:
        return "breakdown"
    return "detail"


def _chart_for(reply, shape, wanted):
    from chart_ui import chartable, default_chart
    df = reply.df
    if df is None or not chartable(df):
        return None
    if shape == "series":
        c = default_chart(df, reply.chart, reply.metric)
        return dict(c, x="period", type=(reply.chart or {}).get("type") or "line") if c else None
    if shape == "ranking" or (shape == "breakdown" and wanted):
        c = default_chart(df, reply.chart, reply.metric)
        return c if c and df[c["x"]].nunique() <= (CHART_MAX_GROUPS if not wanted else 200) else None
    if shape == "detail" and wanted:
        return default_chart(df, reply.chart, reply.metric)
    return None


def _metrics_for(reply, shape):
    """Summary cards without repeating what the table already shows."""
    if shape == "scalar":
        if reply.value is None and reply.cards:                      # a one-row multi-measure total: one card per measure
            seen, out = set(), []
            for c in reply.cards:
                if c["label"] not in seen:
                    seen.add(c["label"]); out.append(c)
            return out[:4]
        label = (reply.plan or {}).get("title") or reply.metric or (reply.df.columns[0] if reply.df is not None and len(reply.df.columns) else "Result")
        val = reply.value if reply.value is not None else reply.df.iloc[0, 0]
        return [{"label": str(label), "value": val, "note": None}]
    if shape in ("series", "ranking", "breakdown"):
        cards = list(reply.cards or [])
        if len(_numeric_cols(reply.df)) >= 2 and cards:
            seen, out = set(), []
            for c in cards:                       # one card per measure
                if c["label"] not in seen:
                    seen.add(c["label"]); out.append(c)
            return out[:4]
        return []
    return []


def _source_info(reply):
    p = reply.plan or {}
    info = {k: p.get(k) for k in ("source_id", "sheet_name", "metric", "aggregation", "date_column") if p.get(k)}
    if p.get("date_from") or p.get("date_to"):
        info["date_range"] = f"{p.get('date_from') or '…'} → {p.get('date_to') or '…'}"
    return info


def finalize(reply, question="", chart_asked=False, debug=False):
    """Reply → the one FinalResponse the UI renders."""
    import re
    wanted = bool(reply.want_chart or chart_asked or re.search(r"\b(graph|graf|chart|plot|visual)\w*", question or "", re.I))
    shape = "breakdown" if getattr(reply, "pivot", False) and reply.df is not None else shape_of(reply)
    table, note = None, None
    if shape in ("series", "ranking", "breakdown"):
        table = reply.df
    elif shape == "detail" and reply.df is not None:
        table = reply.df.head(DETAIL_MAX_ROWS)
        if len(reply.df) > DETAIL_MAX_ROWS:
            note = f"Showing {DETAIL_MAX_ROWS} of {len(reply.df):,} rows — CSV/Excel mein pura data hai."
    elif reply.kind == "report" and reply.df is not None:
        table = reply.df
    pivot = bool(getattr(reply, "pivot", False))
    fr = FinalResponse(answer=reply.text, kind=reply.kind, shape=shape, metrics=_metrics_for(reply, shape), table=table, table_note=note,
                       chart=None if pivot else _chart_for(reply, shape, wanted), source_info=_source_info(reply),
                       drilldown=reply.drillable if shape in ("series", "ranking", "breakdown") else None,
                       options=list(reply.options or []), files=list(reply.files or []), images=list(reply.images or []), videos=list(reply.videos or []))
    fr.value = reply.value if shape == "scalar" else None
    fr.date_range = fr.source_info.get("date_range")
    fr.period = (reply.plan or {}).get("period")
    if debug:
        fr.debug = {"trace": list(reply.trace or []), "plan": reply.plan, "tool_calls": reply.tool_calls}
    return fr


def display_table(df):
    """The table as people read it: Indian grouping, ₹ on money columns, whole counts. The raw frame stays for
    charts and downloads, so no value is altered — only its text."""
    if df is None or df.empty:
        return df
    from analyst import _fmt
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_numeric_dtype(out[c]) and not pd.api.types.is_bool_dtype(out[c]) and c != "period":
            out[c] = out[c].map(lambda v: "" if pd.isna(v) else _fmt(float(v), str(c)))
    return out
