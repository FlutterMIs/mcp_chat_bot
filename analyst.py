"""The one Business Analyst brain shared by every channel (Streamlit web UI, WhatsApp, future API).

A channel builds a Conversation (its sources' schemas + context), picks a Tools transport, and calls
answer(). Nothing here imports Streamlit or WhatsApp code.
"""
from dataclasses import dataclass, field
import re
import time

import pandas as pd

from grounding import _numbers as _answer_numbers, enrich_result, mislabeled_numbers, plain_answer, result_definition, unsupported_claims, unsupported_numbers
from memory import add_mapping, add_plan, add_rule, get_context
from openrouter import OpenRouterAI, compact_schema
from periods import enforce_period, period_metadata, resolve_date_expression
from semantics import AmbiguousMeasure, is_pure_complaint, only_known_words, plan_diff, requested_measures, resolve_measures, validate_plan
import copy
import json

SAFE_METRIC_WORDS = {"amount", "sales amount", "sale amount", "revenue", "sales"}


@dataclass
class Conversation:
    schemas: dict                      # source_id -> schema (as returned by source_schema / register)
    last_plan: dict | None = None
    history: list = field(default_factory=list)   # [{"role": "user"|"assistant", "content": str}]
    recent_plans: list = field(default_factory=list)   # last 5 answered data questions: [{"question", "plan"}]
    focus: str | None = None           # source_id the user connected most recently ("ye kya hai" means this one)
    pending_rule: dict | None = None   # a rule the user taught, waiting for "haan" before it is saved
    state: dict | None = None          # structured analysis state (source, period, grain, metrics…) for follow-ups
    last_result: object = None         # last validated table (for reports / drill-downs)
    pending_choice: dict | None = None # a column choice the bot asked for: {"key", "candidates", "state", "question"}
    forced_table: tuple | None = None  # (source_id, sheet_name) the user picked when two sheets fit a question
    _asked_for: str = ""               # the question a pending clarification belongs to
    last_cards: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)   # {total_key: value} — every total computed so far, for the consistency check


@dataclass
class Reply:
    text: str
    kind: str = "answer"              # answer | text | out_of_scope | clarify | learn | no_source
    df: pd.DataFrame | None = None
    chart: dict | None = None         # {"type","x","y"} hint for any chart renderer
    metric: str | None = None
    plan: dict | None = None
    want_chart: bool = False          # user explicitly asked for a graph
    images: list = field(default_factory=list)   # [{"alt", "url"}] from a web page, for "report images dikhao"
    videos: list = field(default_factory=list)   # [{"title", "url"}] YouTube / mp4 found on a web page
    tool_calls: list = field(default_factory=list)
    trace: list = field(default_factory=list)      # agent steps (Phase 1), shown as a short footer
    value: float | None = None                     # the single figure of a one-number answer (no table is rendered for it)
    cards: list = field(default_factory=list)      # summary cards [{"label","value","note"}]
    options: list = field(default_factory=list)    # clarification choices (rendered as chips / numbered list)
    drillable: str | None = None                   # column whose rows can be clicked for a drill-down ("period")
    files: list = field(default_factory=list)      # generated files [{"name","bytes","mime"}] (reports)
    totals: dict | None = None                     # {"all": {col: v}, "displayed": {col: v}, "cut": bool, "label": "Grand Total"|"Displayed Top N Total", "rows", "groups"}
    pivot: bool = False                            # df is a cross-tab (rows × columns of one measure): table only, no chart
    long_df: pd.DataFrame | None = None            # the long (period/dimension/value) result behind a pivot, for exports/charts


class LocalTools:
    """In-process tool calls on an MCPServer instance (what the Streamlit app uses)."""
    transport = "in-process"

    def __init__(self, server):
        self.server = server

    def call(self, name, args):
        return self.server.call_tool(name, args)


def _tab_names(schema):
    return [s["name"] for s in schema.get("sheets") or []]


def run_data_plan(tools, conv, plan, question=None):
    """Run one data plan through the data tools. Raises ValueError with a readable reason on bad plans."""
    sid = plan.get("source_id")
    if sid not in conv.schemas:
        raise ValueError(f"Source '{sid}' connected nahi hai. Connected sources: {', '.join(conv.schemas)}")
    schema = conv.schemas[sid]
    if question:
        validate_plan(question, plan, schema)   # generic sheet/metric/aggregation/grouping checks; raises to replan
    if schema.get("kind") == "workbook" or schema.get("sheets"):
        tabs = _tab_names(schema)
        if not plan.get("sheet_name"):
            if len(tabs) == 1:
                plan["sheet_name"] = tabs[0]
            else:
                raise ValueError(f"Workbook has several tabs; choose sheet_name from: {', '.join(tabs)}")
        elif plan["sheet_name"] not in tabs:
            raise ValueError(f"Sheet/tab not found: {plan['sheet_name']}. Tabs: {', '.join(tabs)}")
    if plan.get("operation") == "aggregate":
        args = {k: plan.get(k) for k in ["sheet_name", "metric", "aggregation", "group_by", "filters", "limit", "date_column",
                                         "date_grain", "date_from", "date_to", "sort", "top_n"]}
        result = tools.call("aggregate_source", {k: v for k, v in args.items() if v is not None} | {"source_id": sid})
        # HARD validation: a sales-amount question must resolve to an amount-like column, never QTY.
        if result.get("metric") and plan.get("metric") and result["metric"] != plan["metric"]:
            if plan["metric"].strip().lower() in SAFE_METRIC_WORDS and result["metric"].strip().lower() not in SAFE_METRIC_WORDS | {"net sales"}:
                raise ValueError(f"Safety stop: requested sales amount but resolved metric was {result['metric']}. No answer generated.")
        return result
    if plan.get("operation") == "distinct":
        column = (plan.get("columns") or [None])[0]
        if not column:
            raise ValueError("operation distinct needs columns: [one real column]")
        res = tools.call("distinct_values", {"source_id": sid, "column": column, "limit": plan.get("limit") or 50} | ({"sheet_name": plan["sheet_name"]} if plan.get("sheet_name") else {}))
        return {"rows": [{res["column"]: v["value"], "rows": v["rows"]} for v in res["values"]], "distinct_count": res["distinct_count"], "column": res["column"], "source": "tabular"}
    args = {k: plan.get(k) for k in ["sheet_name", "columns", "filters", "sort_by", "sort"]}
    return tools.call("query_source", {k: v for k, v in args.items() if v is not None} | {"source_id": sid, "limit": plan.get("limit") or 500})


_CHART_WORD = re.compile(r"\b(chart|graph|grafh|diagram|plot)\w*", re.I)


def strip_chart_claims(text):
    """The LLM can't know whether a chart gets delivered, so sentences about one are dropped.
    Sentences end at . ! ? । followed by whitespace, so decimals like 58.93% stay intact."""
    out = []
    for line in (text or "").split("\n"):
        parts = re.split(r"(?<=[.!?।])\s+", line)
        # Drop pure claims ("bar graph mein dikhaya gaya hai"); keep sentences that carry numbers.
        out.append(" ".join(p for p in parts if not (_CHART_WORD.search(p) and not re.search(r"\d", p))))
    cleaned = "\n".join(out).strip()
    return cleaned or text


TOTAL_WORDS = re.compile(r"(total|kul|kitn[aie]|sum|overall|poor[ai]|कुल|कितन)", re.I)


def _missing_total(answer, q, plan, result):
    """Asked for a total but got several rows: the answer must state the sum of all rows, not one row."""
    total = result.get("grand_total_all_groups") if plan.get("group_by") else (result.get("derived") or {}).get("total_of_rows")
    if total is None or not TOTAL_WORDS.search(q):
        return None
    if plan.get("group_by") and not re.search(r"(total|kul)\s+(sale|sales|amount|bikri|कुल)", q, re.I):
        return None   # "top 10 customers" alone does not ask for the grand total
    shown = {round(n, 2) for n in _answer_numbers(answer)}
    return None if round(total, 2) in shown or round(total) in shown else total


MONEY_WORDS = ("amount", "sale", "revenue", "value", "price", "rate", "total", "net", "bill", "invoice")


def strip_wrong_currency(text, metric, aggregation=None):
    """₹ only belongs on money. QTY / CLOSING STOCK / counts are plain numbers, whatever the LLM wrote."""
    from semantics import MONEY_WORDS as _MW, tokens as _tk
    money = bool(set(_tk(metric)) & _MW) and (aggregation or "sum") != "count"
    return text if money else re.sub(r"₹\s?(?=\d)", "", text or "")


def count_answer(plan, result):
    """A single count is written by code: LLMs keep calling "33 items" a quantity or a stock."""
    rows = result.get("rows") or []
    if (plan.get("aggregation") or "").lower() != "count" or len(rows) != 1 or not isinstance(rows[0].get("value"), (int, float)):
        return None
    col = str(result.get("metric") or plan.get("metric") or "")
    noun = next((n for k, n in (("customer", "customers"), ("party", "parties"), ("client", "clients"), ("item", "items"), ("product", "products"),
                                ("city", "cities"), ("invoice", "invoices"), ("bill", "bills"), ("order", "orders")) if k in col.lower()), "entries")
    cond = [f"{f.get('column')} mein '{f.get('value')}'" if (f.get("op") or "eq") == "contains" else f"{f.get('column')} = {f.get('value')}"
            for f in plan.get("filters") or []]
    return f"*{int(rows[0]['value']):,}* {noun} hain" + (f" ({'; '.join(cond)})" if cond else "") + "."


def answer_problems(answer, q, plan, result, schema_cols=None):
    """Everything a validator can hold against a draft: unsupported numbers, mislabeled numbers, unbacked claims."""
    problems = []
    bad = unsupported_numbers(answer, result, q, plan)
    if bad:
        problems.append(f"numbers not in the result: {bad[:5]}")
    mis = mislabeled_numbers(answer, result)
    if mis:
        problems.append("numbers attached to the wrong row: " + ", ".join(f"{n} is not the value of '{lab}'" for n, lab in mis[:3]))
    claims = unsupported_claims(answer, result_definition(result, plan, q), schema_cols)
    if claims:
        problems.append("claims not backed by the result (do not say these were included/calculated): " + ", ".join(claims))
    return problems


def grounded_answer(ai, q, plan, result, conv=None):
    """LLM wording, validated: numbers, number+label binding and inclusion claims must all match the result definition.
    One retry with the exact problems, then a code-written answer."""
    counted = count_answer(plan, result)
    if counted:
        return counted
    schema_cols = None
    if conv is not None and plan.get("source_id") in conv.schemas:
        s = conv.schemas[plan["source_id"]]
        schema_cols = [c for t in (s.get("sheets") or [{"columns": s.get("columns", [])}]) for c in t.get("columns", [])]
    answer = ai.format_result(q, plan, result).get("answer", "")
    rows = result.get("rows") or []
    single = len(rows) == 1 and set(rows[0]) <= {"value", "period"} and isinstance(rows[0].get("value"), (int, float)) and not isinstance(rows[0]["value"], bool)
    if single:
        # The key figure is written by code; the LLM only adds prose, which must be clean or is dropped.
        value = rows[0]["value"]
        headline = f"**{_fmt(value, result.get('metric') or plan.get('metric'))}**"
        prose_ok = not answer_problems(answer, q, plan, result, schema_cols)
        shown = any(abs(n - round(float(value), 2)) < 0.005 * max(abs(value), 1) for n in _answer_numbers(answer))
        if not prose_ok:
            answer = ""
        if not shown or not answer:
            what = f"{result.get('metric') or plan.get('metric') or ''} ({plan.get('aggregation') or 'sum'})".strip()
            answer = f"{headline} — {what}" + (f"\n{answer}" if answer else "")
        return strip_wrong_currency(strip_chart_claims(answer), result.get("metric") or plan.get("metric"), plan.get("aggregation"))
    problems = answer_problems(answer, q, plan, result, schema_cols)
    if problems:
        answer = ai.format_result(q, plan, result, feedback="Rewrite. Problems with the previous draft: " + " | ".join(problems)).get("answer", "")
        if answer_problems(answer, q, plan, result, schema_cols):
            return plain_answer(result, q)
    missing = _missing_total(answer, q, plan, result)
    if missing is not None:
        answer = ai.format_result(q, plan, result, feedback=f"The user asked for the TOTAL. The real overall total is {missing}. "
                                  "State that total first; never call a single row, or the sum of only the listed top rows, the total.").get("answer", "")
        if _missing_total(answer, q, plan, result) is not None:
            return plain_answer(result, q)
    bad = unsupported_numbers(answer, result, q, plan)
    if bad:
        answer = ai.format_result(q, plan, result, feedback=f"These numbers are not in the result: {bad}. Rewrite using only numbers from the result.").get("answer", "")
        bad = unsupported_numbers(answer, result, q, plan)
    if bad or not answer:
        return plain_answer(result, q)
    return strip_wrong_currency(strip_chart_claims(answer), result.get("metric") or plan.get("metric"), plan.get("aggregation"))


CHART_KINDS = {"bar", "barh", "line", "area", "pie", "donut", "scatter"}


def chart_hint(plan, df):
    """{type, x, y} for the result, honouring a chart type the user asked for ("pie chart mein dikhao")."""
    if df is None or df.empty or plan.get("chart") == "none" or len(df) < 2:
        return None
    asked = plan.get("chart") if plan.get("chart") in CHART_KINDS else None
    groups = plan.get("group_by") or []
    if "value" in df.columns:
        if plan.get("date_grain") and "period" in df.columns and df["period"].nunique() >= 2 and not groups:
            return {"type": asked or "line", "x": "period", "y": "value"}
        if groups and groups[0] in df.columns:
            return {"type": asked or ("barh" if plan.get("top_n") else "bar"), "x": groups[0], "y": "value"}
    # Detail rows: only when a chart type was asked for; chart_ui picks the label/value columns.
    return {"type": asked} if asked else None


# Words that ask for a split over time. Without them a period ("last 12 months") is a filter, not a grouping.
BREAKDOWN = re.compile(
    r"(month\s*-?\s*wise|months\s*wise|monthly|per\s+month|har\s+month|each\s+month|every\s+month|month\s+by\s+month|month\s+ke\s+hisaab|"
    r"mah(?:i|ee)n[ae]?\s*(?:ke\s*)?(?:hisaab|hisab|wise|anusar|vaar|war)|har\s+mah(?:i|ee)n|mah(?:i|ee)n[ae]\s+mein\s+kitn|"
    r"day\s*-?\s*wise|date\s*-?\s*wise|daily|per\s+day|har\s+din|din\s+ke\s+hisaab|roz|week\s*-?\s*wise|weekly|hafte|"
    r"quarter|year\s*-?\s*wise|yearly|saal\s*-?\s*wise|har\s+saal|period\s*-?\s*wise|trend|timeline|over\s+time|"
    r"महीने\s*(?:के\s*)?(?:हिसाब|वार)|हर\s+महीने|मासिक|रोज़ाना|दैनिक)", re.I)
COMPARE = re.compile(r"(compare|comparison|\bvs\b|versus|mukabl|muqabl|tulna|badh|ghat|growth|change|difference|antar|farak|fark)", re.I)
YEARS = re.compile(r"\b(?:19|20)\d{2}\b")
SOURCE_Q = re.compile(r"(se le r?a?h[ae] h|se le rha|kahan se|kaha se|kis sheet|kaunsi sheet|konsi sheet|which sheet|which data|source kya|kis data se|kahan ka data|"
                      r"(?:kaise|kese|kise|kis tarah|kis hisaab|kis basis|how)\s+(?:\w+\s+){0,4}?(?:lag\w*ya|nik\w*la|calc\w*|comput\w*|count\w*|liya|liye|bana\w*|aaya|aya|did you|was this|is this)|"
                      r"how did you|how was|calculation kya|hisaab kaise|formula kya|"
                      r"(?:kaun|kon|konsi|kaunsi|which|what)\s*(?:sa|si|se)?\s*(?:date|dates|period|range|time|mahin\w*|month|columns?)\s*(?:range|column)?\s*(?:use|used|liya|liye|lagaya|lagaaya|hui|hua|thi|tha|did you|ka|ki)|"
                      r"date range kya|kis date se|kaunse date|konse date|"
                      r"(?:date|dates|period|range|column|mahin\w*)\s*(?:kaunsi|konsi|kaun si|kon si|kaunsa|konsa|which)?\s*(?:use|liya|liye|li|lagaya|hui|thi|tha)\s*(?:ki|kiya|hui|thi|gayi)?\b)", re.I)
HELP_Q = re.compile(r"(kya\s+(?:kya\s+)?(?:help|madad|kaam)|(?:help|madad)\s+kar\s+sakt|what can you (?:do|help)|how can you help|what do you do|tum kya kar sakte|aap kya kar sakte|capabilit\w*|features kya hain\s*$)", re.I)
DATA_OVERVIEW_Q = re.compile(r"(\b(?:kya|kya kya|kaisa|kaunsa|konsa|which|what|kitna)\s+(?:\w+\s+){0,2}?(?:data|information|info|columns?|fields?|tables?|sheets?)\b|"
                             r"isme kya|is ?me kya|ismein kya|what'?s in|what is in|describe|overview|schema|structure|columns kya|kya kya hai|data kya hai|data batao)", re.I)
DEICTIC = re.compile(r"(\b(ye|yeh|ya|isme|is me|iska|iski|iske|isko|is website|is site|is page|is file|is sheet|this|it|isse)\b|summary|kya h(ai|ia|a)\b|kya karta|kya karti|about)", re.I)
SMALLTALK = re.compile(r"^\s*(hi+|hello+|hey+|hii+|namaste|namaskar|thanks?|thank you|thx|ok+|okay|acha|achha|theek hai|good morning|good night|gm|bye|👍|🙏)[\s!.?]*$", re.I)
IMAGE_Q = re.compile(r"\b(images?|photos?|pictures?|pics?|tasveer|screenshot|logo)\b", re.I)
VIDEO_Q = re.compile(r"\b(videos?|vid|tut\w*|demo\s*video|walk\s*-?through|youtube|reels?|clips?)\b", re.I)
SHARE_Q = re.compile(r"\b(share|bhej|bhejo|bhej do|link|links|url|send)\b", re.I)
COMPLAINT = re.compile(r"(^\s*(?:no|nahi|nhi|nope|na)\b[,!.\s—-]|\bnahi\b[^.?!]*\bchahiye\b|\bnot\s+[a-z]+\s*[,;—-]|\bi asked (?:for )?|maine .* (?:poocha|pucha|maanga|manga)|use (?:the )?other sheet|dusri sheet|"
                       r"galat|wrong|incorrect|not cor+e?c?t|sahi nahi|theek nahi|thik nahi|issue hai|mistake|galti|match nahi|mismatch|"
                       r"data (?:is )?not|गलत|सही नहीं)", re.I)


_THINGS = r"(?:items?|products?|skus?|customers?|clients?|part(?:y|ies)|orders?|bills?|invoices?|entries|rows|cities|city|states?|log|aadmi|saman|maal ke item)"
COUNT_Q = re.compile(rf"(?:\b(?:kitne|kitni|how\s+many|number\s+of|count\s+of|ginti)\s+(?:\w+\s+){{0,2}}?{_THINGS}\b|\b{_THINGS}\s+kitn[ei]\s+(?:hai|hain|he|h)\b)", re.I)


def _count_guard(plan, question):
    """"telescopic items kitne hain" asks for a count; LLMs often sum a stock/amount column instead."""
    if not COUNT_Q.search(question) or (plan.get("aggregation") or "sum") == "count":
        return plan
    name_col = next((f.get("column") for f in plan.get("filters") or [] if (f.get("op") or "eq") in ("contains", "eq", "in")), None)
    name_col = name_col or (plan.get("group_by") or [None])[0]
    if not name_col:
        return plan
    plan.update(aggregation="count", metric=name_col, sort=None, top_n=None)
    plan["group_by"] = [g for g in plan.get("group_by") or [] if g != name_col]
    return plan


NUMERIC_Q = re.compile(r"(total|sum|kul|kitn|count|how many|how much|average|avg|highest|lowest|max|min|sabse|top\s*\d*|rank|price|rate|kimat|keemat|daam|₹|\d)", re.I)


def route_documents(plan, question, schemas):
    """Websites and documents are read as text. Their HTML tables are only queried for number questions
    ("sabse sasta plan", "total"); "call recording kaise hoti hai" must be answered from the page text."""
    if plan.get("status") != "execute" or plan.get("mode") == "text":
        return plan
    kind = (schemas.get(plan.get("source_id")) or {}).get("kind")
    if kind in (None, "table", "workbook"):
        return plan
    if plan.get("operation") != "aggregate" and not NUMERIC_Q.search(question):
        return {"status": "execute", "mode": "text", "source_id": plan["source_id"], "operation": "search"}
    return plan


def sanitize_plan(plan, question, previous, resolve_dates=True):
    """Deterministic guard on LLM plans: a time period alone must never split a total into months.
    Keep date_grain only if the user asked for a breakdown/comparison, or this is a follow-up of a plan that had one."""
    if plan.get("status") != "execute" or plan.get("mode") == "text":
        return plan
    # The LLM may read the language; the dates come from the one resolver ("last month" = the previous calendar month,
    # "last 2 months" = the 2 calendar months ending today — never today-60 days). LLM dates never bypass this.
    if resolve_dates and plan.get("operation") in ("aggregate", "rows", "query", "compare") and resolve_date_expression(question):
        plan = enforce_period(plan, question)
        if not plan.get("date_column"):
            plan["date_column"] = _only_date_column(plan, previous)
    if plan.get("operation") != "aggregate":
        return plan
    plan = _count_guard(plan, question)
    grain = plan.get("date_grain")
    if grain in (None, "null", "none", ""):
        plan["date_grain"] = None
        return plan
    asked = BREAKDOWN.search(question) or COMPARE.search(question) or len(set(YEARS.findall(question))) >= 2
    inherited = bool(previous) and previous.get("date_grain") == grain
    if not asked and not inherited:
        plan["date_grain"] = None
        # grouping by the raw date column as well would split the same way
        plan["group_by"] = [g for g in plan.get("group_by") or [] if g != plan.get("date_column")]
    return plan


def _only_date_column(plan, previous):
    """When the planner filtered by a period but named no date column: the previous plan's, else None (validated later)."""
    return (previous or {}).get("date_column") if (previous or {}).get("source_id") == plan.get("source_id") else None


def seed_state(conv, plan, question):
    """After a planner-made aggregate answer, keep the analysis context so follow-ups ("total kar ke batao", "August ka?",
    "customer wise") modify this result instead of starting from zero. Only for plain column measures (sum/avg/max/min)."""
    from analysis import table_of_state
    from semantics import measure_key
    if (plan or {}).get("operation") != "aggregate" or plan.get("mode") == "text":
        return
    schema = conv.schemas.get(plan.get("source_id")) or {}
    table = table_of_state(schema, plan.get("sheet_name")) or {}
    col = next((c for c in table.get("columns", []) if c["name"] == plan.get("metric")), None)
    agg = (plan.get("aggregation") or "sum").lower()
    if col is None or col.get("role") == "dimension" or agg not in ("sum", "avg", "max", "min"):
        return
    from semantics import column_kind
    kind = "monetary" if column_kind(col) == "monetary" else "quantity"
    measure = {"kind": kind, "entity": None, "column_hint": col["name"], **({"aggregation": agg} if agg != "sum" else {})}
    p = resolve_date_expression(question) or ({"from": plan.get("date_from"), "to": plan.get("date_to"), "label": None} if plan.get("date_from") or plan.get("date_to") else {})
    conv.state = {"source_id": plan.get("source_id"), "sheet_name": plan.get("sheet_name") or table.get("name"), "date_column": plan.get("date_column"),
                  "period": p, "grain": plan.get("date_grain") or None, "group_by": list(plan.get("group_by") or []), "metrics": [measure],
                  "filters": list(plan.get("filters") or []), "top_n": plan.get("top_n"), "sort": plan.get("sort"),
                  "choices": {measure_key(measure): col["name"]}}


def answer(conv, question, api_key, model, tools, log=None):
    """Answer one user message. Updates conv.last_plan / conv.history. Raises on LLM/network failure."""
    log = log or (lambda *a, **k: None)
    if not api_key:
        raise ValueError("OpenRouter API key add karo.")
    if not conv.schemas:
        return _done(conv, question, Reply("Main sirf tumhare connected data se jawab deta hoon. Pehle Google Sheet, file ya web URL connect karo, phir pucho.", kind="no_source"))
    focus = conv.focus if conv.focus in conv.schemas else None
    if conv.pending_rule:
        # The user taught a rule last turn; save it only on an explicit yes, and only for that source.
        pending, conv.pending_rule = conv.pending_rule, None
        if re.match(r"^\s*(haan|ha|han|yes|y|ok|okay|save|theek|thik|sahi|kar do|kardo|हाँ|हां)\b", question, re.I):
            add_rule(pending["text"], pending["source_id"])
            return _done(conv, question, Reply(f"✅ Rule yaad rakh li (*{pending['source_name']}* ke liye): {pending['text']}", kind="learn"))
        if re.match(r"^\s*(nahi|na|no|nope|cancel|rehne do|mat|नहीं)\b", question, re.I):
            return _done(conv, question, Reply("Theek hai, rule save nahi kiya.", kind="learn"))
    ctx = {"just_connected_source": ({"source_id": focus, "name": conv.schemas[focus].get("name"), "kind": conv.schemas[focus].get("kind")} if focus else None),
           "source_schemas": [compact_schema(s) for s in conv.schemas.values()], "memory": get_context(source_ids=list(conv.schemas)),
           "previous_plan": conv.last_plan, "earlier_plans": conv.recent_plans[:-1][-4:], "recent_conversation": [f"{x['role']}: {x['content']}" for x in conv.history[-8:]]}
    ai = OpenRouterAI(api_key, model)
    t0 = time.time()
    merged = {"sheets": [t for s in conv.schemas.values() for t in (s.get("sheets") or ([{"name": s.get("name"), "columns": s.get("columns")}] if s.get("columns") else []))]}
    if conv.last_plan and (COMPLAINT.search(question) or SOURCE_Q.search(question)):
        if is_pure_complaint(question, merged) or SOURCE_Q.search(question):
            # "ye galat hai" / "kahan se liya": re-run the previous plan and show exactly how the number was made.
            log("complaint_verify")
            return _done(conv, question, verify_previous(conv, tools, ai))
        # "galat hai, amount chahiye": a correction — the planner must change something in the previous plan.
        ctx["correction_of"] = conv.last_plan
        log("correction")
    if conv.pending_choice:
        resolved = resolve_choice(conv, question, api_key, model, tools, log)
        if resolved is not None:
            return resolved
    try:
        understood = understood_reply(conv, question, tools, log)
    except ValueError as e:
        understood = no_data_reply(conv, tools, e, log)       # a typed value that exists nowhere → one clear line, never a raw tool error
    if understood is not None:
        return _done(conv, question, understood)
    names = [re.split(r"\s[–|-]\s", str(s.get("name") or sid))[0][:40] for sid, s in conv.schemas.items()]
    from semantics import question_intent as _qi
    _i = _qi(question)
    if HELP_Q.search(question) and len(question.split()) <= 12:
        return _done(conv, question, Reply(help_text(conv), kind="help"))
    if DATA_OVERVIEW_Q.search(question) and not _i["aggregation"] and not _i["time"] and not _i["superlative"] and len(question.split()) <= 12:
        # "isme kya data hai" / "which columns" → a code-built overview of what is connected (no LLM, nothing invented).
        return _done(conv, question, Reply(data_overview(conv), kind="overview"))
    if SMALLTALK.match(question):
        # Greetings get a short, friendly line from code — no LLM lecture.
        example = "is website ke features kya hain?" if any(s.get("kind") not in (None, "table", "workbook") for s in conv.schemas.values()) else "last 12 months ki total sale kitni hai?"
        return _done(conv, question, Reply(f"Namaste 🙂 Aap *{', '.join(names[:3])}* ke baare mein kuch bhi poochiye — jaise \"{example}\"", kind="out_of_scope"))
    docs_connected = any(s.get("kind") not in (None, "table", "workbook") for s in conv.schemas.values())
    if VIDEO_Q.search(question) and docs_connected and not NUMERIC_Q.search(question):
        return _done(conv, question, video_reply(conv, question, tools, focus))
    if IMAGE_Q.search(question) and docs_connected and not NUMERIC_Q.search(question):
        return _done(conv, question, image_reply(conv, question, tools, focus))
    last_media = next((h.get("media") for h in reversed(conv.history) if h["role"] == "assistant"), None)
    if SHARE_Q.search(question) and last_media and len(question.split()) <= 8:
        # "mujhe share kro" right after a list of videos/images: send those links.
        links = "\n".join(f"{i}. {m['title']}: {m['url']}" for i, m in enumerate(last_media, 1))
        return _done(conv, question, Reply(f"Ye rahe links:\n{links}", kind="links"))
    from agent import classify, run_agent   # local import: agent.py imports Reply/chart_hint from this module
    all_cols = [c for s in conv.schemas.values() for t in (s.get("sheets") or [{"columns": s.get("columns", [])}]) for c in t.get("columns", [])]
    wanted = requested_measures(question, all_cols)
    if classify(question, conv.schemas) == "complex":
        # Spans two sheets ("… sale amount … aur abhi kitna closing stock hai") or documents: the bounded agent joins them.
        log("route_agent")
        return _done(conv, question, run_agent(conv, question, api_key, model, tools, log=log, requested=wanted if len(wanted) >= 2 else None))
    if len(wanted) >= 2 and not ctx.get("correction_of"):
        # Several metrics: the planner only supplies source/dates/grouping (1 cheap call); everything else is code.
        log("route_multi_metric", measures=[w["kind"] + (":" + w["entity"] if w["entity"] else "") for w in wanted])
        try:
            return _done(conv, question, multi_metric_answer(conv, question, wanted, ai, ctx, tools, log))
        except ValueError as e:
            return _done(conv, question, no_data_reply(conv, tools, e, log) or Reply(f"⚠️ Data se ye jawab nahi nikal paaya: {str(e)[:200]}", kind="clarify"))

    def make_plan():
        raw = ai.plan(question, ctx)
        p = route_documents(sanitize_plan(copy.deepcopy(raw), question, conv.last_plan), question, conv.schemas)
        return p, json.dumps(p, sort_keys=True, default=str) == json.dumps(raw, sort_keys=True, default=str)

    plan, untouched = make_plan()
    calls = []
    for attempt in range(3):
        status = plan.get("status")
        log("ai_route", status=status, mode=plan.get("mode"), operation=plan.get("operation"), attempt=attempt)
        if status == "learn":
            sid = plan.get("source_id") if plan.get("source_id") in conv.schemas else (focus or next(iter(conv.schemas)))
            sname = re.split(r"\s[–|-]\s", str(conv.schemas[sid].get("name") or sid))[0]
            if plan.get("rule"):
                conv.pending_rule = {"text": plan["rule"], "source_id": sid, "source_name": sname}
                return _done(conv, question, Reply(f"Ye rule sirf *{sname}* ke liye yaad rakhun?\n“{plan['rule']}”\n\n*haan* bolo to save karunga, *nahi* to chhod dunga.", kind="confirm_rule"))
            if plan.get("term") and plan.get("column"):
                add_mapping(sid, plan.get("sheet_name"), plan["term"], plan["column"])
                return _done(conv, question, Reply(f"✅ Yaad rakh li: “{plan['term']}” = column *{plan['column']}* (*{sname}*).", kind="learn"))
        docs = [s for s in ([focus] if focus else []) + list(conv.schemas) if conv.schemas[s].get("kind") not in (None, "table", "workbook")]
        if status == "out_of_scope" and docs and attempt == 0 and not SMALLTALK.match(question):
            # A website/document is connected: never refuse without reading it. The page text decides.
            if IMAGE_Q.search(question):
                return _done(conv, question, Reply("Main website/document ka text aur tables padh sakta hoon, uski images nahi dekh sakta. "
                                                   "Text mein jo likha hai uske baare mein pucho — jaise features, pricing, contact.", kind="out_of_scope"))
            log("doc_search_before_refusal", source=docs[0])
            plan = {"status": "execute", "mode": "text", "source_id": docs[0], "operation": "search", "fallback_refusal": plan.get("message")}
            status = "execute"
        if status == "out_of_scope":
            again = next((h.get("kind") for h in reversed(conv.history) if h["role"] == "assistant"), None) == "out_of_scope"
            msg = ("Main sirf aapke connected data (sheet, files, website) ke sawalon ka jawab deta hoon 🙂 Examples ke liye /help bhejo."
                   if again else plan.get("message") or "Ye sawal tumhare connected data se answer nahi ho sakta.")
            return _done(conv, question, Reply(msg, kind="out_of_scope"))
        if status == "clarify":
            return _done(conv, question, Reply(plan.get("message", "Thoda aur specify karo."), kind="clarify", plan=plan))
        try:
            if ctx.get("correction_of") and status == "execute" and not plan_diff(ctx["correction_of"], plan):
                raise ValueError("The user corrected the previous answer but the new plan is identical to previous_plan. "
                                 "Change the field the correction refers to (metric, sheet, group_by, filters, dates, aggregation).")
            if plan.get("mode") == "text":
                sid = plan.get("source_id")
                if sid not in conv.schemas:
                    raise ValueError(f"Source '{sid}' connected nahi hai. Connected sources: {', '.join(conv.schemas)}")
                retrieved = tools.call("search_source", {"source_id": sid, "query": question})
                calls.append("search_source")
                if retrieved.get("no_match"):
                    # RAG found no relevant passage: say so without calling the model (no guessing, no spend).
                    name = str(retrieved.get("name") or "connected document").split(" – ")[0].split(" | ")[0]
                    return _done(conv, question, Reply(f"Is baare mein *{name}* mein kuch nahi mila. Document mein jo likha hai uske baare mein poochiye.",
                                                       kind="out_of_scope" if plan.get("fallback_refusal") is not None else "text", plan=plan))
                got = ai.answer_text(question, retrieved)
                text = got.get("answer", "")
                if got.get("found") is not False and retrieved.get("citations"):
                    from mcp_server import citation_footer
                    foot = citation_footer(retrieved["citations"])
                    text = (text.rstrip() + "\n\n" + foot) if foot else text
                if got.get("found") is False:
                    # Not on the page: one clear line from code (no "retrieved source" jargon, no guessing).
                    name = str(retrieved.get("name") or "connected page").split(" – ")[0].split(" | ")[0]
                    return _done(conv, question, Reply(f"Ye jaankari *{name}* pe nahi mili. Page par jo likha hai uske baare mein poochiye — jaise features ya contact.",
                                                       kind="out_of_scope" if plan.get("fallback_refusal") is not None else "text", plan=plan))
                if unsupported_numbers(text, retrieved, question, plan):
                    text = "Ye jawab document/page se verify nahi ho paaya, isliye main guess nahi karunga. Sawal thoda specific karke pucho."
                reply = Reply(text, kind="text", plan=plan)
            else:
                result = enrich_result(run_data_plan(tools, conv, plan, question), plan)
                calls.append({"aggregate": "aggregate_source", "distinct": "distinct_values"}.get(plan.get("operation"), "query_source"))
                df = pd.DataFrame(result.get("rows", []))
                single = len(df) == 1 and list(df.columns) == ["value"]
                reply = Reply(grounded_answer(ai, question, plan, result, conv), df=None if single else df, chart=None if single else chart_hint(plan, df),
                              metric=result.get("metric") or plan.get("metric"), plan=plan, want_chart=bool(plan.get("want_chart")),
                              value=(float(df["value"].iloc[0]) if single and pd.api.types.is_number(df["value"].iloc[0]) and not isinstance(df["value"].iloc[0], bool) else None))
                attach_totals(reply, plan, result)
                apply_pivot(reply, plan, question)
                attach_entity_images(reply, question)
            conv.last_plan = plan
            conv.recent_plans = (conv.recent_plans + [{"question": question, "plan": plan}])[-5:]
            add_plan(question, plan, validated=untouched and attempt == 0 and plan.get("mode") != "text")
            reply.tool_calls = calls
            if plan.get("mode") != "text":
                seed_state(conv, plan, question)
                conv.last_result, conv.last_cards = reply.df, []
                if reply.value is not None and plan.get("operation") == "aggregate" and not plan.get("group_by") and not plan.get("date_grain"):
                    from analysis import reconcile, total_key
                    note = reconcile(conv, total_key(plan.get("source_id"), plan.get("sheet_name"), result.get("metric") or plan.get("metric"), plan.get("aggregation") or "sum",
                                                     plan.get("date_from"), plan.get("date_to"), plan.get("filters")), reply.value, None, result.get("metric") or plan.get("metric"))
                    if note:
                        reply.text += "\n" + note
            log("ai_done", tools=calls, transport=getattr(tools, "transport", "?"), source=plan.get("source_id"),
                sheet=plan.get("sheet_name"), ms=int((time.time() - t0) * 1000))
            return _done(conv, question, reply)
        except (ValueError, KeyError) as e:
            from entities import AmbiguousValue
            if isinstance(e, AmbiguousValue):
                # "Pranjli ji" fits several real names about equally: ask, never pick one silently.
                conv._asked_for = question
                opts = [{"label": v, "choice": None, "rewrite": _replace_value(question, e.typed, v)} for v in e.candidates]
                return _done(conv, question, _ask(conv, log, "entity:" + str(e.column), f"“{e.typed}” se aapka matlab kaun sa {e.column} hai?", opts))
            log("plan_failed", attempt=attempt, error=str(e)[:300])
            if attempt == 2:
                # The direct pipeline could not produce a valid plan: let the bounded agent inspect the data itself.
                log("route_agent", reason="direct_failed")
                return _done(conv, question, run_agent(conv, question, api_key, model, tools, log=log))
            # Self-correction: show the planner its own error (it lists the real columns/values) and re-plan.
            ctx["failed_attempt"] = {"plan": plan, "error": str(e)}
            plan, untouched = make_plan()
    raise ValueError("Plan nahi ban paaya.")


def data_overview(conv):
    """What is connected: tables, row counts, columns by kind with a few real sample values."""
    from semantics import annotate_schema
    lines = []
    for sid, s in conv.schemas.items():
        s = annotate_schema(dict(s))
        name = re.split(r"\s[–|-]\s", str(s.get("name") or sid))[0]
        tables = s.get("sheets") or ([{"name": None, "row_count": s.get("row_count"), "columns": s.get("columns", [])}] if s.get("columns") else [])
        if not tables:
            extra = []
            if s.get("text_length"): extra.append(f"{s['text_length']:,} characters text")
            if s.get("image_count"): extra.append(f"{s['image_count']} images")
            if s.get("video_count"): extra.append(f"{s['video_count']} videos")
            lines.append(f"🌐 *{name}* — " + ", ".join(extra) + ". Iske baare mein kuch bhi pucho.")
            continue
        lines.append(f"📊 *{name}*")
        for t in tables:
            cols = t.get("columns", [])
            kinds = {}
            for c in cols:
                kinds.setdefault(c.get("kind"), []).append(c)
            head = f"• *{t['name']}*" if t.get("name") else "•"
            head += f" — {t.get('row_count'):,} rows, {len(cols)} columns" if t.get("row_count") is not None else f" — {len(cols)} columns"
            lines.append(head)
            for kind, label in (("date", "Date"), ("monetary", "Amounts"), ("quantity", "Quantities"), ("measure", "Numbers"), ("rate", "Rates")):
                if kinds.get(kind):
                    lines.append(f"   {label}: " + ", ".join(c['name'] for c in kinds[kind][:8]))
            dims = kinds.get("dimension", [])
            if dims:
                shown = []
                for c in dims[:6]:
                    ex = ", ".join(str(v) for v in (c.get("sample_values") or [])[:2])
                    shown.append(f"{c['name']}" + (f" (e.g. {ex})" if ex else ""))
                lines.append("   Categories: " + "; ".join(shown) + (f" +{len(dims) - 6} more" if len(dims) > 6 else ""))
    lines.append("\nPucho jaise: \"total batao\", \"month wise dikhao\", \"top 5 <category> by <amount>\", ya kisi date range ka data.")
    return "\n".join(lines)


def multi_metric_answer(conv, question, wanted, ai, ctx, tools, log):
    """Month/dimension-wise table with every requested metric: one column per measure, joined and chronological."""
    import pandas as pd
    raw = ai.plan(question, ctx)
    base = sanitize_plan(copy.deepcopy(raw), question, conv.last_plan)
    # A multi-column table "with a date column" is a time breakdown even without the words "month wise".
    time_words = re.search(r"\b(date|dates|month|months|mahin\w*|maheen\w*|day|days|din|week|hafte|year|saal|quarter|period)\b", question, re.I)
    if not base.get("date_grain") and not base.get("group_by") and time_words and base.get("status") == "execute":
        date_col = base.get("date_column") or raw.get("date_column")
        if not date_col:
            sid0 = base.get("source_id")
            tab = next((t for t in (conv.schemas.get(sid0) or {}).get("sheets") or [] if t["name"] == base.get("sheet_name")), None) or {"columns": (conv.schemas.get(sid0) or {}).get("columns", [])}
            dates = [c["name"] for c in tab.get("columns", []) if c.get("role") == "date"]
            date_col = dates[0] if len(dates) == 1 else None
        if date_col:
            base["date_column"], base["date_grain"] = date_col, raw.get("date_grain") if raw.get("date_grain") in ("day", "month", "year") else ("day" if re.search(r"\b(day|days|din|daily|date wise|date-wise)\b", question, re.I) else "month")
    if base.get("status") != "execute" or base.get("mode") == "text":
        return Reply(base.get("message") or "Ye sawal is data se nahi ban paya.", kind=base.get("status") or "clarify")
    sid = base.get("source_id")
    if sid not in conv.schemas:
        return Reply(f"Source '{sid}' connected nahi hai.", kind="clarify")
    schema = conv.schemas[sid]
    tabs = _tab_names(schema)
    sheet = base.get("sheet_name") or (tabs[0] if len(tabs) == 1 else None)
    table = next((t for t in schema.get("sheets") or [] if t["name"] == sheet), None) or {"columns": schema.get("columns", [])}
    try:
        measures = resolve_measures(wanted, table.get("columns", []))
    except AmbiguousMeasure as e:
        return Reply(f"❓ {e}", kind="clarify")
    keys = (["period"] if base.get("date_grain") else []) + list(base.get("group_by") or [])
    if not keys:
        return Reply("Kis hisaab se chahiye — month wise, ya kisi column ke hisaab se? (jaise 'month wise' / 'customer wise')", kind="clarify")
    common = {k: base.get(k) for k in ("sheet_name", "filters", "date_column", "date_grain", "date_from", "date_to", "group_by") if base.get(k) is not None}
    common["sheet_name"] = sheet
    from analysis import execute_state
    state = {"source_id": sid, "sheet_name": sheet, "date_column": base.get("date_column"), "grain": base.get("date_grain"),
             "period": {"from": base.get("date_from"), "to": base.get("date_to"), "label": None} if (base.get("date_from") or base.get("date_to")) else {},
             "group_by": list(base.get("group_by") or []), "metrics": wanted, "filters": list(base.get("filters") or []), "top_n": None, "sort": None}
    from understanding import FOLLOW_TOP
    if FOLLOW_TOP.search(question):                   # only a real "top 10 / sabse zyada" request limits and re-sorts the rows
        state["top_n"], state["sort"] = base.get("top_n"), base.get("sort")
    from understanding import parse_period
    p = parse_period(question)
    if p:                      # deterministic period beats the planner's guess ("last 12 months" = 12 calendar months)
        state["period"] = p
        state["grain"] = state["grain"] or p.get("grain")
    return _remember(conv, execute_state(conv, state, tools, question, log))
    merged, definition_cols, calls = None, [], []
    for m in measures:
        res = tools.call("aggregate_source", {**common, "source_id": sid, "metric": m["column"], "aggregation": m["aggregation"]})
        calls.append(f"{m['aggregation']}({m['column']}) by {keys}")
        df = pd.DataFrame(res.get("rows") or [])
        if df.empty:
            df = pd.DataFrame(columns=keys + ["value"])
        df = df.rename(columns={"value": m["label"]})
        merged = df if merged is None else merged.merge(df, on=keys, how="outer")
        definition_cols.append({"name": m["label"], "type": "count" if m["aggregation"].startswith("count") else "metric", "aggregation": m["aggregation"], "source_column": m["column"]})
    merged = merged.sort_values(keys, kind="stable").reset_index(drop=True)
    missing = [m["label"] for m in measures if m["label"] not in merged.columns]
    if missing:
        return Reply(f"Ye metrics nahi nikal paya: {missing}.", kind="clarify")
    # Code-written answer: describes exactly the columns that exist.
    labels = ", ".join(m["label"] for m in measures)
    span = f" ({_ddmmyyyy(base.get('date_from'))} → {_ddmmyyyy(base.get('date_to'))})" if base.get("date_from") or base.get("date_to") else ""
    grain = {"month": "month", "day": "date", "year": "year"}.get(base.get("date_grain"), ", ".join(base.get("group_by") or []))
    top = merged.iloc[merged[measures[0]["label"]].astype(float).idxmax()] if len(merged) else None
    text = f"{grain.title()}-wise table ({len(merged)} rows){span} — columns: {', '.join(keys)}, {labels}."
    if top is not None and keys:
        text += f" Sabse zyada {measures[0]['label']}: {top[keys[0]]} ({_fmt(float(top[measures[0]['label']]), measures[0]['column'])})."
    plan = {**base, "operation": "multi_metric", "metric": measures[0]["column"], "metrics": measures, "title": f"{grain.title()} wise {labels}", "want_chart": bool(base.get("want_chart"))}
    conv.last_plan = plan
    conv.recent_plans = (conv.recent_plans + [{"question": question, "plan": plan}])[-5:]
    log("ai_done", tools=calls, transport=getattr(tools, "transport", "?"), source=sid, sheet=sheet)
    chart = {"type": "line" if "period" in keys else "bar", "x": keys[0], "y": measures[0]["label"]} if len(merged) >= 2 else None
    return Reply(text, kind="answer", df=merged, chart=chart, metric=measures[0]["column"], plan=plan, want_chart=bool(base.get("want_chart")), trace=calls)


def understood_reply(conv, question, tools, log):
    """Deterministic path: understanding layer + state executor. Returns a Reply, or None to fall through to the planner.
    Used when the request is a follow-up on an existing analysis (add a metric, a month, top N, a report, details)
    or a fresh analysis whose table, date column and metrics are unambiguous from the schema alone."""
    from analysis import build_report, execute_state, pick_date_column, pick_table, table_of_state
    from understanding import understand
    if not conv.schemas:
        return None
    state = conv.state or {}
    pick = None
    if state:
        schema = conv.schemas.get(state.get("source_id")) or {}
        table = table_of_state(schema, state.get("sheet_name")) or {}
        cols = table.get("columns", [])
        if _names_other_table(question, cols, conv):
            state = {}                      # "closing stock kitna hai?" mid-conversation: that column lives in another sheet → fresh question
            log("route_switch_table")
            from semantics import value_words as _vw
            every = [c for s in conv.schemas.values() for t in (s.get("sheets") or [{"columns": s.get("columns", [])}]) for c in t.get("columns", [])]
            if _vw(question, every):
                log("route_planner", reason="switched table + value words — a filter the planner must apply")
                return None
    if not state:
        pick, rivals = _likely_table(conv, question)
        cols = list((pick[2] if pick else {}).get("columns", []))
        if rivals:
            # Two sheets fit the question about equally ("items" lives in both): asking beats guessing the sheet.
            conv._asked_for = question
            return _ask(conv, log, "table", "Ye kis sheet se chahiye — " + " ya ".join(n for _, n, _ in rivals) + "?",
                        [{"label": f"{n} sheet", "table": [sid, n]} for sid, n, _ in rivals])
    # Entity words ("jsp trader ki …", "Pranjli ji ki …") are matched against the REAL values of the table's dimension
    # columns before anything else: one clear match becomes a filter, a tie is asked, nothing close → the planner.
    from entity_filter import find_entity_filter, merge_filters, strip_span
    from entities import AmbiguousValue
    sheet_names = [t.get("name") for sch in conv.schemas.values() for t in sch.get("sheets") or [] if t.get("name")]
    ent_sid = state.get("source_id") if state else (pick[0] if pick else None)
    ent_table = table if state else (pick[2] if pick else None)
    entity, q_intent = None, question
    from understanding import SHOW_ITEMS
    from entity_filter import unknown_spans
    if ent_sid and ent_table and unknown_spans(question, cols, sheet_names) and not SHOW_ITEMS.search(question):   # "kon kon … karta hai" lists rows: the planner's job
        try:
            entity = find_entity_filter(question, ent_sid, ent_table, tools, sheet_names)
        except AmbiguousValue as e:
            conv._asked_for = question
            opts = [{"label": v, "choice": None, "rewrite": _replace_value(question, e.typed, re.sub(r"\s*\([^()]*\)$", "", v) if e.column == "naam" else v)} for v in e.candidates]
            return _ask(conv, log, "entity:" + str(e.column), f"“{e.typed}” se aapka matlab kaun sa {e.column} hai?", opts)
        if entity:
            q_intent = strip_span(question, entity["typed"])
            log("entity_filter", column=entity["column"], value=entity["value"], typed=entity["typed"])
        else:
            from entity_filter import filter_like_spans
            missing = filter_like_spans(question, cols, sheet_names)
            if missing:
                # "mobile ki sale" when nothing is called mobile anywhere: say so — never answer without the filter
                raise ValueError(f'Value "{missing[0]}" not found in column "{ent_table.get("name") or ent_sid}"')
    intent = understand(q_intent, cols, state)
    if entity:
        intent["entity_filter"] = entity
    gate = ambiguity_gate(conv, q_intent, cols, intent, state, log, pick if not state else None)
    if gate is not None:
        return gate
    if intent["kind"] == "report" and state and conv.last_result is not None:
        name, data = build_report(conv, conv.last_result, conv.last_cards)
        log("report_built", rows=len(conv.last_result))
        return Reply(f"📄 Report taiyar hai: *{name}* — summary (source, date range, calculation, totals) + data sheet.", kind="report",
                     files=[{"name": name, "bytes": data, "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}], df=conv.last_result, plan=conv.last_plan)
    metric_swap = bool(intent["metrics"]) and intent["metrics"] != (state.get("metrics") or []) and only_known_words(q_intent, cols)
    filters = merge_filters(state.get("filters"), entity, additive=bool(intent.get("additive")) or bool(re.search(r"\b(usme|unme|isme|inme|us ?mein|in ?mein)\b", question, re.I))) if entity else list(state.get("filters") or [])
    if state and intent.get("collapse"):
        # SERIES → TOTAL ("total kar ke batao", "only total"): same source, period, filters and metric; one number, no split.
        new_state = {**state, "period": intent["period"] or {}, "grain": None, "group_by": [], "metrics": intent["metrics"] or state.get("metrics") or [],
                     "top_n": None, "sort": None, "concise": intent.get("concise"), "explain_period": intent.get("explain_period"), "filters": filters}
        log("route_state", kind="collapse", metrics=len(new_state["metrics"]), grain=None, group_by=[])
        return _remember(conv, execute_state(conv, new_state, tools, question, log))
    if state and intent["kind"] in ("analysis", "drill") and not only_known_words(q_intent, cols) and not intent.get("explain_period"):
        log("route_planner", reason="unknown words — may be a filter value")     # "Code-1032 ka item kaun leta hai": filters are the planner's job
        return None
    if intent["kind"] in ("analysis", "drill") and state and (intent["additive"] or intent["correction"] or intent["kind"] == "drill" or intent.get("period") or intent.get("grain") or intent.get("group_by") or intent.get("top_n") or metric_swap or entity):
        grain = intent["grain"] or (((intent["period"] or {}).get("grain")) if not intent["group_by"] else None)     # "last 12 months" carries month grain
        new_state = {**state, "period": intent["period"] or {}, "grain": grain, "group_by": intent["group_by"], "metrics": intent["metrics"] or state.get("metrics") or [],
                     "top_n": intent.get("top_n"), "sort": intent.get("sort"), "explain_period": intent.get("explain_period"),
                     "rank_scope": intent.get("rank_scope"), "share": intent.get("share"), "mom": intent.get("mom"), "filters": filters}
        log("route_state", kind=intent["kind"], metrics=len(new_state["metrics"]), grain=new_state["grain"], group_by=new_state["group_by"], rank_scope=new_state["rank_scope"], confidence=intent.get("confidence"))
        return _remember(conv, execute_state(conv, new_state, tools, question, log))
    from semantics import value_words
    if intent["kind"] == "analysis" and not state and intent["metrics"] and value_words(q_intent, cols) and not entity:
        log("route_planner", reason="value words — a filter the planner must apply")     # "Acme ka last 2 months sale"
        return None
    if intent["kind"] == "analysis" and not state and intent["metrics"] and (intent["period"] or intent["grain"] or intent["group_by"] or _settled_choice(conv, intent) or only_known_words(q_intent, cols, sheet_names)):
        # A plain total ("total items sold") runs here too, but only when every word is understood — an unknown
        # word may be a filter value, and filters are the planner's job.
        # Fresh analysis: only when the table and date column are unambiguous — otherwise the planner decides.
        if pick is None:
            return None
        sid, sheet, table = pick
        date_col = pick_date_column(table.get("columns", []))
        if (intent["period"] or intent["grain"]) and isinstance(date_col, list):
            known, _ = _known_choices(conv, None, sid=sid, sheet=sheet)
            if known.get("date_column") in date_col:
                date_col = known["date_column"]
            else:
                conv._asked_for = question
                return _ask(conv, log, "date_column", "Date ke liye kaunsa column lein — " + " ya ".join(date_col) + "?",
                            [{"label": d, "choice": ["date_column", d]} for d in date_col], sid=sid, sheet=sheet)
        if (intent["period"] or intent["grain"]) and not isinstance(date_col, str):
            return None
        new_state = {"source_id": sid, "sheet_name": sheet, "date_column": date_col if isinstance(date_col, str) else None, "period": intent["period"] or {},
                     "grain": intent["grain"] or ((intent["period"] or {}).get("grain") if not intent["group_by"] else None), "group_by": intent["group_by"],
                     "metrics": intent["metrics"], "filters": filters, "top_n": intent.get("top_n"), "sort": intent.get("sort"), "explain_period": intent.get("explain_period"),
                     "rank_scope": intent.get("rank_scope"), "share": intent.get("share"), "mom": intent.get("mom")}
        log("route_state", kind="fresh", metrics=len(new_state["metrics"]), grain=new_state["grain"], group_by=new_state["group_by"], rank_scope=new_state["rank_scope"], confidence=intent.get("confidence"))
        return _remember(conv, execute_state(conv, new_state, tools, question, log))
    return None


def _names_other_table(question, cols, conv):
    """True when the question spells out a column that the current table lacks but another connected table has."""
    from semantics import content_tokens, stem, tokens
    from semantics import _SYN_OF, entity_of
    qt = {stem(t) for t in content_tokens(question)}
    here = {c["name"] for c in cols}
    here_tokens = {stem(x) for c in cols for x in tokens(c["name"])}
    current = (conv.state or {}).get("sheet_name")
    for schema in conv.schemas.values():
        # "September ki sales kitni hai?" while the state is on INVENTORY: another tab is named outright → fresh question there
        for t in schema.get("sheets") or []:
            st = {stem(x) for x in tokens(t.get("name") or "")}
            if t.get("name") and t.get("name") != current and st and st <= qt and not ({stem(x) for x in tokens(current or "")} & qt):
                return True
        for t in schema.get("sheets") or [{"columns": schema.get("columns", [])}]:
            for c in t.get("columns", []):
                if c["name"] in here:
                    continue
                ct = {stem(x) for x in tokens(c["name"])}
                core = ct - {"name", "no", "id", "code"}
                if len(ct) >= 2 and ct <= qt:
                    return True                                     # "closing stock" spelled out
                if len(core) == 1 and core <= qt and not (core & here_tokens) and c.get("role") == "dimension":
                    return True                                     # "customer kon kon hai" while on a sheet with no customer column
    return False


def _likely_table(conv, question):
    """The one table a fresh question refers to, across every connected source.
    Returns (pick, rivals): pick = (source_id, sheet_name, table) or None; rivals = [(source_id, label, table), ...] when
    two tables fit about equally well — a real ambiguity the user must settle, never a guess."""
    from semantics import content_tokens, stem, stems
    if conv.forced_table:
        sid, name = conv.forced_table
        schema = conv.schemas.get(sid) or {}
        tables = schema.get("sheets") or [{"name": None, "columns": schema.get("columns", [])}]
        t = next((t for t in tables if t.get("name") == name), None)
        return ((sid, name, t) if t else None), []
    from semantics import _SYN_OF
    qt = {stem(t) for t in content_tokens(question)}
    q_ents = {_SYN_OF[t] for t in qt if t in _SYN_OF}                       # "seller" → employee, "party" → customer
    ranked = []
    for sid, schema in conv.schemas.items():
        if schema.get("kind") not in (None, "table", "workbook"):
            continue
        tables = schema.get("sheets") or ([{"name": None, "columns": schema.get("columns", [])}] if schema.get("columns") else [])
        src_bonus = 2 * len(qt & stems(str(schema.get("name") or ""))) if len(tables) > 1 else 0   # "Sales 2026" names the source, not one of its tabs
        for t in tables:
            label = t.get("name") or str(schema.get("name") or sid)
            tab_sc = 2 * len(qt & stems(label)) + sum(len(qt & stems(c["name"])) for c in t.get("columns", []))
            tab_sc += sum(1 for c in t.get("columns", []) if c.get("role") == "dimension" and {_SYN_OF.get(x) for x in stems(c["name"])} & q_ents)   # entity words name a column
            ranked.append((tab_sc + src_bonus, sid, t.get("name"), label, t, tab_sc))
    ranked.sort(key=lambda x: -x[0])
    if not ranked:
        return None, []
    if len(ranked) == 1:
        sc, sid, name, label, t, _ = ranked[0]
        return (sid, name, t), []
    best, best_sid, best_tab = ranked[0][0], ranked[0][1], ranked[0][5]
    # tabs of the same source compete on their own words only (the shared source name cannot break their tie)
    # A table that matches strictly fewer of the question's words (1 vs 2: "Delhi ke top 2 party by amount" → INVENTORY has
    # only "amount", SALES has "amount" + the party column) is not a rival; equal scores are.
    close = [(sid, label, t) for sc, sid, name, label, t, tab in ranked[:4] if sc > 0 and ((sid != best_sid and (sc == best or (sc >= 2 and sc * 2 >= best)))
                                                                                        or (sid == best_sid and tab > 0 and (tab == best_tab or (tab >= 2 and tab * 2 >= best_tab))))]
    if best_tab == 0 and ranked[0][0] > 0 and len([x for x in ranked if x[1] == best_sid]) > 1:
        close = [(sid, label, t) for sc, sid, name, label, t, tab in ranked[:4] if sid == best_sid]                     # only the source matched: its tabs tie
    if len(close) > 1:
        return None, close
    if best == 0:
        return None, []
    sc, sid, name, label, t, _ = ranked[0]
    return (sid, name, t), []


def _known_choices(conv, state, sid=None, sheet=None):
    """Choices already settled for the table in play (this analysis, or remembered from earlier): {key: column}."""
    sid = sid or (state or {}).get("source_id") or (next(iter(conv.schemas)) if len(conv.schemas) == 1 else None)
    sheet = sheet or (state or {}).get("sheet_name")
    known = dict((state or {}).get("choices") or {})
    if sid:
        for m in get_context(source_ids=[sid]).get("mappings", []):
            if m.get("term") and m.get("sheet") in (None, sheet):
                known.setdefault(m["term"], m.get("column"))
    return known, sid


def _settled_choice(conv, intent):
    """A bare 'items count' whose meaning the user already settled can run without a period/grouping."""
    known, _ = _known_choices(conv, None)
    return any(m.get("kind") == "count" and "count:item" in known for m in intent.get("metrics") or [])


def _ask(conv, log, key, question_text, options, sid=None, sheet=None):
    """Park a clarification on the conversation and return it as chips. options: [{"label", "choice"|"table"|"rewrite"}]."""
    labels = [o["label"] for o in options]
    conv.pending_choice = {"key": key, "candidates": labels, "options": {o["label"]: o for o in options}, "question": conv._asked_for or "",
                           "state": None, "source_id": sid, "sheet_name": sheet, "forced_table": list(conv.forced_table) if conv.forced_table else None}
    log("ambiguous", key=key, options=labels)
    return Reply("❓ " + question_text + "\nOptions: " + " · ".join(f"{i}. {l}" for i, l in enumerate(labels, 1)), kind="clarify", options=labels)


def ambiguity_gate(conv, question, cols, intent, state, log, pick=None):
    """CLEAR → None. AMBIGUOUS → a clarify Reply with chips, and the choice parked on the conversation.
    Nothing is executed and no model is called until the user picks."""
    from understanding import ambiguity
    if intent.get("kind") not in ("analysis", "drill") and not intent.get("top_n"):
        return None
    known, sid = _known_choices(conv, state, sid=(pick or (None,))[0], sheet=(pick or (None, None))[1])
    amb = ambiguity(question, cols, intent, state, set(known))
    if not amb:
        return None
    conv._asked_for = question
    return _ask(conv, log, amb["key"], amb["question"], amb["options"], sid=sid, sheet=(pick or (None, None))[1] if pick else (state or {}).get("sheet_name"))


def resolve_choice(conv, question, api_key, model, tools, log):
    """The user's reply to a clarification. Returns None when it does not name an option (then it is a new message)."""
    from analysis import execute_state
    pc = conv.pending_choice
    chosen = _pick_option(question, pc["candidates"])
    if not chosen:
        return None
    conv.pending_choice = None
    opt = (pc.get("options") or {}).get(chosen) or {"choice": [pc.get("key"), chosen], "rewrite": None}
    log("choice_resolved", key=pc.get("key"), chosen=chosen)
    conv.forced_table = tuple(pc["forced_table"]) if pc.get("forced_table") else None     # a sheet settled earlier in this chain stays settled
    if opt.get("table"):
        conv.forced_table = tuple(opt["table"])
        return answer(conv, pc["question"], api_key, model, tools, log)
    if opt.get("choice"):
        key, col = opt["choice"]
        sid = pc.get("source_id") or (pc.get("state") or {}).get("source_id") or ""
        add_mapping(sid, pc.get("sheet_name") or (pc.get("state") or {}).get("sheet_name"), key, col)     # remembered for this source
        if conv.state:
            conv.state["choices"] = {**(conv.state.get("choices") or {}), key: col}
    if pc.get("state") and not opt.get("rewrite"):                # a column question raised mid-execution: rerun that exact analysis
        st = dict(pc["state"])
        st["choices"] = {**(st.get("choices") or {}), opt["choice"][0]: opt["choice"][1]}
        return _done(conv, question, _remember(conv, execute_state(conv, st, tools, pc.get("question") or question, log)))
    return answer(conv, opt.get("rewrite") or pc["question"], api_key, model, tools, log)      # the original request, now unambiguous


def _remember(conv, reply):
    if reply.kind == "clarify" and reply.options and getattr(reply, "_pending", None):
        conv.pending_choice = reply._pending
    else:
        conv.last_result = reply.df
        conv.last_cards = reply.cards
    return reply


def _pick_option(text, candidates):
    """'QTY se bro' / 'qty' / '1' / 'pehla' → the candidate it names."""
    from semantics import stem, tokens
    _tk = lambda s: [stem(x) for x in tokens(s)]
    t = str(text or "").strip().lower()
    toks = set(_tk(t))
    for i, c in enumerate(candidates, 1):
        if t == c.lower() or str(i) in toks or (i == 1 and toks & {"pehla", "first"}) or (i == 2 and toks & {"dusra", "doosra", "second"}):
            return c
    content = toks - {"se", "wala", "wale", "wali", "bro", "bhai", "karo", "kar", "do", "ka", "ki", "ke", "hai", "chahiye", "the", "one", "please", "plz", "yaar", "ok"}
    part = [c for c in candidates if content and content <= set(_tk(c))]      # "sales person" names "SALES PERSON wise total"
    if len(part) == 1:
        return part[0]
    hits = [c for c in candidates if set(_tk(c)) and set(_tk(c)) <= toks]
    if hits:                                       # "alt qty wala" names both Qty and Alt Qty → the more specific one
        hits.sort(key=lambda c: -len(set(_tk(c))))
        if len(hits) == 1 or len(set(_tk(hits[0]))) > len(set(_tk(hits[1]))):
            return hits[0]
    hits = [c for c in candidates if c.lower().replace("_", " ") in t.replace("_", " ")]
    return hits[0] if len(hits) == 1 else None


def attach_totals(reply, plan, result):
    """Grouped planner results: all-data total (grand_total_all_groups, computed before any top-N cut) vs the sum of the shown rows."""
    df = reply.df
    if df is None or df.empty or "value" not in df.columns or (plan or {}).get("operation") != "aggregate" or (plan.get("aggregation") or "sum") not in ("sum", "count"):
        return reply
    shown = float(pd.to_numeric(df["value"], errors="coerce").sum())
    grand = result.get("grand_total_all_groups")
    cut = bool(result.get("groups_total") and result["groups_total"] > len(df))
    label = reply.metric or plan.get("metric") or "value"
    reply.totals = {"all": {label: float(grand) if grand is not None else shown}, "displayed": {label: shown}, "cut": cut, "rows": int(len(df)),
                    "groups": int(result.get("groups_total") or len(df)), "label": (f"Displayed Top {plan['top_n']} Total" if cut and plan.get("top_n") else ("Displayed Total" if cut else "Grand Total"))}
    return reply


def apply_pivot(reply, plan, question=""):
    """A two-key aggregate (period × dimension, or dimension × dimension) with one measure reads best as a cross-tab.
    The long result stays on reply.long_df; the numbers are only re-arranged."""
    from pivot import describe, pivot_table, should_pivot
    if reply.df is None or (plan or {}).get("operation") != "aggregate" or "value" not in reply.df.columns:
        return reply
    keys = (["period"] if plan.get("date_grain") else []) + [g for g in plan.get("group_by") or [] if g in reply.df.columns]
    if not should_pivot(keys, [plan.get("metric")], reply.df, question):
        return reply
    wide = pivot_table(reply.df, keys, "value")
    reply.long_df, reply.df, reply.pivot, reply.chart, reply.drillable = reply.df, wide, True, None, None
    reply.text = (reply.text or "").rstrip() + "\n" + describe(keys, reply.metric or plan.get("metric") or "value", len(wide) - 1, len(wide.columns) - 2)
    return reply


IMAGE_COL = re.compile(r"(^|[^a-z])(image|img|photo|picture|pic|thumbnail|thumb|logo|icon)(_?url|_?link|s)?($|[^a-z])", re.I)


def image_columns(df):
    """Columns whose name says image/photo/thumbnail AND whose values are real http(s) links — never guessed."""
    out = []
    for c in df.columns:
        if IMAGE_COL.search(str(c)):
            vals = df[c].dropna().astype(str).str.strip()
            if len(vals) and vals.str.match(r"^https?://", case=False).mean() >= 0.5:
                out.append(c)
    return out


def attach_entity_images(reply, question, limit=12):
    """Entity/image result mode: rows with an image column, when the user asks for images/photos. Real URLs only."""
    if reply.df is None or reply.df.empty or not IMAGE_Q.search(question or "") or (reply.plan or {}).get("operation") not in ("rows", "query", None):
        return reply
    cols = image_columns(reply.df)
    if not cols:
        return reply
    col = cols[0]
    labels = [c for c in reply.df.columns if c not in cols and not pd.api.types.is_numeric_dtype(reply.df[c])]
    images = []
    for _, r in reply.df.head(limit).iterrows():
        url = str(r[col]).strip()
        if re.match(r"^https?://", url, re.I):
            images.append({"alt": " · ".join(str(r[l]) for l in labels[:2] if str(r.get(l, "")).strip()) or url, "url": url})
    reply.images = images
    return reply


NO_VALUE = re.compile(r'(?:No rows contain|Value) "(?P<value>[^"]+)" (?:in|not found in) column "(?P<column>[^"]+)"')


def no_data_reply(conv, tools, error, log=None):
    """A filter value that is not in the data ("mobile" when no item is called that): say so plainly, list the closest
    real values (from the tools, never invented), and never answer from another value or period."""
    m = NO_VALUE.search(str(error))
    if not m:
        raise error
    value, column = m.group("value"), m.group("column")
    from entities import match_entity
    close = []
    for sid, schema in conv.schemas.items():
        for t in schema.get("sheets") or [{"name": None, "columns": schema.get("columns", [])}]:
            for c in t.get("columns", []):
                if c.get("role") != "dimension" or (c.get("distinct_count") or 0) > 5000:
                    continue
                try:
                    vals = [v["value"] for v in tools.call("distinct_values", {"source_id": sid, "column": c["name"], "limit": 5000} | ({"sheet_name": t["name"]} if t.get("name") else {})).get("values") or []]
                except Exception:
                    continue
                mm = match_entity(vals, value, cutoff=0.6)
                for cand in mm["candidates"][:3]:
                    close.append((cand["score"], f"{cand['value']} ({c['name']})"))
    close = [x for _, x in sorted(set(close), key=lambda x: -x[0])][:5]
    if log:
        log("no_data_value", value=value, column=column, suggestions=len(close))
    text = f"“{value}” naam ki koi entry *{column}* mein nahi mili, isliye is par koi number nahi hai."
    text += ("\nMilte-julte naam: " + " · ".join(close) + " — inme se koi chahiye to wahi naam likh do.") if close else "\nSahi naam (jaise data mein likha hai) ke saath dobara pucho."
    return Reply(text, kind="no_data")


def _replace_value(question, typed, real):
    """The question with the typed entity replaced by the real one ('Pranjli ji ki sales' → 'Pranjali Ji ki sales')."""
    out = re.sub(re.escape(str(typed)), real, question, count=1, flags=re.I)
    if out == question:
        core = re.sub(r"\b(ji|sahab|sir|madam)\b", "", str(typed), flags=re.I).strip()
        out = re.sub(re.escape(core), real, question, count=1, flags=re.I) if core else question
    return out if out != question else f"{question} ({real})"


def _ddmmyyyy(d):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    return f"{m[3]}-{m[2]}-{m[1]}" if m else (str(d) if d else "")


def help_text(conv):
    names = [re.split(r"\s[–|-]\s", str(s.get("name") or sid))[0] for sid, s in conv.schemas.items()]
    return ("Main aapke connected data ka analyst hoon" + (f" — abhi *{', '.join(names[:3])}* juda hai" if names else "") + ". Main kar sakta hoon:\n"
            "• Totals, averages, counts — kisi bhi period (this month, last month, 2026, date range) ke liye\n"
            "• Month/day/year wise breakdown, top/bottom N, category/customer/employee wise\n"
            "• Filters (kisi party/item/city ka), comparisons (2025 vs 2026, growth)\n"
            "• Ek saath kai metrics (amount + quantity + count) aur do tables/sheets ko milaana\n"
            "• Graph (bar/line/pie), CSV/Excel download, WhatsApp par bhi\n"
            "• Website/document connect karke uske baare mein sawal\n"
            "• \"ye kaise nikala?\" puchne par poora hisaab (sheet, column, date range)\n\n"
            "Naya data jodne ke liye sidebar se Google Sheet / file / website / database connect karo.")


def _fmt(v, metric):
    """96273311.63 -> ₹9,62,73,311.63 (Indian grouping; ₹ only for money columns)."""
    whole, frac = f"{abs(float(v)):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head)) if head else ""
    from semantics import MONEY_WORDS, RATE_WORDS, tokens
    tk = set(tokens(metric))
    money = bool(tk & MONEY_WORDS) and not (tk & RATE_WORDS)
    return ("-" if v < 0 else "") + ("₹" if money else "") + (head + "," if head else "") + tail + ("" if frac == "00" else "." + frac)


def video_reply(conv, question, tools, focus=None):
    order = ([focus] if focus else []) + [s for s in conv.schemas if s != focus]
    for sid in order:
        if not (conv.schemas[sid].get("video_count") or 0):
            continue
        found = tools.call("find_videos", {"source_id": sid, "query": question, "limit": 8})
        if found.get("videos"):
            name = re.split(r"\s[–|-]\s", str(found.get("name") or sid))[0]
            links = "\n".join(f"{i}. **{v['title']}** — {v['url']}" for i, v in enumerate(found["videos"], 1))
            return Reply(f"*{name}* website pe ye videos hain:\n\n{links}", kind="videos", videos=found["videos"])
    return Reply("Is website pe koi video nahi mila.", kind="out_of_scope")


def image_reply(conv, question, tools, focus=None):
    """Images that are really on the connected page (alt text + link). The bot can't look inside them, so it says so."""
    order = ([focus] if focus else []) + [s for s in conv.schemas if s != focus]
    for sid in order:
        if not (conv.schemas[sid].get("image_count") or 0):
            continue
        found = tools.call("find_images", {"source_id": sid, "query": question, "limit": 6})
        if found.get("images"):
            name = re.split(r"\s[–|-]\s", str(found.get("name") or sid))[0]
            more = f" (kul {found['total']} milti-julti images; aur chahiye to specific naam likho)" if found["total"] > len(found["images"]) else ""
            return Reply(f"*{name}* website pe ye images hain{more}:", kind="images", images=found["images"])
    return Reply("Is website/document pe aisi koi image nahi mili. Main images ke andar ka content nahi padh sakta, sirf unke naam aur text.", kind="out_of_scope")


def verify_previous(conv, tools, ai):
    """Re-run the previous data plan and explain it. If the guards change the plan, the old answer was wrong."""
    import copy
    old = conv.last_plan
    if (old or {}).get("operation") == "multi_metric" and conv.state:
        from analysis import explain_state
        return explain_state(conv)
    question = next((p["question"] for p in reversed(conv.recent_plans) if p["plan"] == old), "")
    fixed = sanitize_plan(copy.deepcopy(old), question, None, resolve_dates=False)     # explain the dates that were used, as they were
    if old.get("mode") == "text" or old.get("operation") not in ("aggregate", "rows"):
        return Reply("Maine pichhla jawab dobara dekha. Batao kya galat laga — kaunsa number ya kaunsa naam — main wahi check karta hoon.", kind="clarify")
    result = enrich_result(run_data_plan(tools, conv, fixed), fixed)
    changed = fixed.get("date_grain") != old.get("date_grain") or fixed.get("group_by") != old.get("group_by")
    rows = result.get("rows") or []
    grand = result.get("grand_total_all_groups") or (result.get("derived") or {}).get("total_of_rows")
    value = rows[0].get("value") if len(rows) == 1 else grand
    scope = [f"• Sheet: {fixed.get('sheet_name') or fixed.get('source_id')}", f"• Column: {fixed.get('metric')} ({fixed.get('aggregation') or 'sum'})"]
    if fixed.get("date_from") or fixed.get("date_to") or fixed.get("date_column"):
        scope.append(f"• Date column: {fixed.get('date_column') or '—'}")
        scope.append(f"• Date range: {_ddmmyyyy(fixed.get('date_from')) or '…'} → {_ddmmyyyy(fixed.get('date_to')) or '…'}" + (f" ({fixed['date_grain']} wise)" if fixed.get("date_grain") else ""))
    for f in fixed.get("filters") or []:
        scope.append(f"• Filter: {f.get('column')} {f.get('op') or '='} {f.get('value')}")
    if fixed.get("group_by"):
        scope.append(f"• Group: {', '.join(fixed['group_by'])}" + (f" (top {fixed['top_n']})" if fixed.get("top_n") else ""))
    scope.append(f"• Rows counted: {result.get('rows_matched', len(rows))}")
    num = _fmt(value, fixed.get("metric")) if isinstance(value, (int, float)) else str(value)
    head = ("❗ Aap sahi ho — pichhla jawab galat tha (data ko mahine-wise tod ke ek hi hissa bataya gaya tha). Sahi:" if changed
            else "🔍 Maine data dobara check kiya. Ye number aise nikla:")
    text = f"{head}\n\n**{num}**\n\n" + "\n".join(scope)
    if not changed:
        text += "\n\nAapke hisaab se number alag hai to batao kya chahiye — koi party/item/city filter, alag period, ya alag column."
    conv.last_plan = fixed
    df = pd.DataFrame(rows) if len(rows) > 1 else None
    return Reply(text, kind="answer", df=df, chart=chart_hint(fixed, df), metric=result.get("metric") or fixed.get("metric"), plan=fixed)


def _done(conv, question, reply):
    if reply.kind in ("answer", "agent") and reply.plan and reply.plan.get("mode") != "text":
        from result_check import enforce
        reply = enforce(reply, conv.schemas)       # last gate: plan columns/sheet vs schema, finite numbers — or a clear error
    media = ([{"title": v["title"], "url": v["url"]} for v in reply.videos] or [{"title": i["alt"], "url": i["url"]} for i in reply.images]) or None
    conv.history += [{"role": "user", "content": question}, {"role": "assistant", "content": reply.text, "kind": reply.kind, **({"media": media} if media else {})}]
    conv.history = conv.history[-20:]
    if not conv.pending_choice:
        conv.forced_table = None                # the chosen sheet now lives in conv.state (or the question is done)
    return reply

