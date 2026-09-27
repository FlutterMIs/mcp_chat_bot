"""Understanding layer: turn a user message plus the conversation's analysis state into a structured intent.

Everything here is deterministic and data-agnostic. It knows about time phrases ("last 12 months", "August",
"pichle mahine"), follow-up forms ("X bhi add karo", "top 10", "report bana do", "<month> wala dikhao") and
corrections — but nothing about any business domain. Column choices come from `semantics.resolve_measures`.
"""
import re
from datetime import date

from periods import MONTHS, resolve_date_expression
from semantics import COUNT_CUES, GROUP_STEMS, column_kind, ITEM_LIKE, NEGATION, _SYN_OF, content_tokens, entity_of, question_intent, requested_measures, stem, tokens


def parse_period(question, today=None):
    """Relative/absolute time phrases → the canonical period dict from `periods.resolve_date_expression`
    ({"label", "from", "to", "grain", "period_type", "periods", "asks"?}) or None. One resolver for every path."""
    return resolve_date_expression(question, today or date.today())


GRAIN_WORDS = [("day", r"\b(day|days|daily|din|date wise|date-wise|datewise|roz|dainik|har din)\b"),
               ("month", r"\b(month wise|month-wise|monthwise|monthly|mahin\w*|maheen\w*|har month|per month|month by month|masik)\b"),
               ("year", r"\b(year wise|year-wise|yearly|saal wise|har saal|annual)\b")]
FOLLOW_ADD = re.compile(r"\b(bhi|also|too|as well|add|jodo|jod do|include|saath mein|ke saath|plus)\b", re.I)
FOLLOW_TOP = re.compile(r"\b(top|bottom|sabh? ?se (?:zyada|jada|jyada|kam|bad[ae]|chhot[ae]|bekar|achch?h[ae]|kharab)|highest|lowest|best|worst|least|minimum|maximum)\s*(\d{1,3})?\b", re.I)
LOW_WORDS = re.compile(r"\b(kam|lowest|least|minimum|min|bottom|worst|bekar|kharab|chhot[ae]|neech?e|niche|ghatiya|smallest)\b", re.I)
HIGH_WORDS = re.compile(r"\b(zyada|jada|jyada|highest|most|maximum|max|best|bad[ae]|largest|biggest|upar)\b", re.I)
REPORT_Q = re.compile(r"\b(report|summary report|pdf|excel report|report bana|report banao|report generate|export)\b", re.I)
SUPERLATIVE_ONLY = re.compile(r"\b(sabh? ?se|bahut|bohot|bhut|highest|lowest|most|least|best|worst|maximum|minimum)\b", re.I)
DRILL_Q = re.compile(r"\b(details?|detail dikhao|open karo|click|drill|breakup|break up|andar|ke andar|wala dikhao|wale dikhao|expand)\b", re.I)
SHOW_ITEMS = re.compile(r"\b(kaun kaun|kon kon|which|konse|kaunse|kya kya)\b", re.I)
# SERIES → TOTAL: "total kar ke batao", "sab mila ke", "only total", "grand total", "ek number mein"
COLLAPSE = re.compile(r"\b(total|totals|sum|overall|kul|poora|pura|grand total|sab mila ?ke|milake|mila ?kar|jod ?ke|jodke|jod kar|add kar ?ke|combined|ek number|single number|कुल|टोटल)\b", re.I)
ONLY = re.compile(r"\b(only|sirf|bas|just|keval|simple|short|ek line|one line|sirf number|number only)\b", re.I)
# A total question ("kitni hui", "total batao") vs a breakdown request ("dikhao", "table", "trend", "wise")
SCALAR_ASK = re.compile(r"\b(total|totals|sum|kul|overall|kitn[aie]|kitna|how much|कितन\w*|कुल)\b", re.I)
BREAKDOWN_ASK = re.compile(r"\b(dikhao|dikha|dikhana|show|table|colum\w*|list|trend|graph|graf|chart|plot|breakup|break up|wise|har|each|every|per|month by month|monthly|daily|weekly|yearly|split|distribution)\b", re.I)
DATE_ASK = re.compile(r"\b(?:kaun|kon|konsi|kaunsi|which|kis|kab)\s*(?:si|sa|se|sey)?\s*(?:kon\s*se\s*)?(?:date|dates|period|range|tarikh|tareekh|time|din)\b|\bdates?\s+(?:tak|se)\b|\bkab\s+se\s+kab\s+tak\b|\bdate\s+range\b", re.I)


def wants_grain(question, period=None):
    q = re.sub(r"\s+", " ", str(question or "").lower())
    if period and period.get("date_expression"):
        q = q.replace(period["date_expression"], " ")         # "pichle do mahine ki sale" names a period, not a breakdown
    for g, pat in GRAIN_WORDS:
        if re.search(pat, q):
            return g
    return None


def dimension_matches(question, columns):
    """(full, partial): dimension columns fully named in the question ('sales person wise') vs only touched by one
    word ('sales wale' → SALES PERSON). A partial match is a hint, never a certainty."""
    toks = [stem(t) for t in tokens(question)]
    qt = {stem(t) for t in content_tokens(question)}
    def counted(ct):   # "customer count" / "kitne students": that entity is counted, not grouped by
        return any(t in ct and any(x in COUNT_CUES for x in toks[max(0, i - 2):i + 3]) for i, t in enumerate(toks))
    full, partial = [], []
    named = [(c["name"], {stem(t) for t in tokens(c["name"])}) for c in columns if {stem(t) for t in tokens(c["name"])} <= qt]   # columns named in full
    for c in columns:
        if c.get("role") != "dimension":
            continue
        ct = {stem(t) for t in tokens(c["name"])} - {"name", "no", "id", "code"}
        if not ct or counted(ct):
            continue
        if any(ct < n for name, n in named if name != c["name"]):
            continue                                        # "VOUCHER DATE se" names the date column, not VOUCHER NAME
        if ct <= qt:
            full.append(c["name"])
        elif len(ct) > 1 and len(ct & qt) >= 1 and any(len(t) > 3 for t in ct & qt):
            partial.append(c["name"])
    if not full and not partial and not counted(qt & ITEM_LIKE):
        for c in columns:   # item-like words → the item-ish dimension even if the column is called "Product" / "SKU"
            if c.get("role") == "dimension" and (qt & ITEM_LIKE) and ({stem(t) for t in tokens(c["name"])} & ITEM_LIKE):
                full.append(c["name"])
    return full, partial


def mentioned_dimension(question, columns, measures=()):
    """A dimension column named in the question ('customer wise', 'items dikhao', 'city ke hisaab se').
    A partial name ('sales wale') counts only with a grouping word ('sales wise') — otherwise it is left open.
    An item word that is itself the requested measure ('total items sold') is not a grouping."""
    full, partial = dimension_matches(question, columns)
    toks = [stem(t) for t in tokens(question)]
    grouping = bool(set(toks) & GROUP_STEMS)
    if full and not grouping and any((m.get("entity") in ITEM_LIKE or m.get("entity") == "item" or m.get("kind") == "quantity") for m in measures):
        full = [d for d in full if not ({stem(t) for t in tokens(d)} & ITEM_LIKE)]
    if full:
        return full[0]
    for d in partial:      # "sales wise" → SALES PERSON; "month wise total sales" → the grouping word belongs to month, not sales
        mt = {stem(t) for t in tokens(d)}
        if any(toks[i] in mt and ((i + 1 < len(toks) and toks[i + 1] in GROUP_STEMS) or (i > 0 and toks[i - 1] in GROUP_STEMS)) for i in range(len(toks))):
            return d
    if grouping:           # "customer wise" on a table whose column is PARTY NAME: the entity synonyms decide
        from semantics import dimension_for_entity
        for i, t in enumerate(toks):
            ent = _SYN_OF.get(t)
            if ent and ((i + 1 < len(toks) and toks[i + 1] in GROUP_STEMS) or (i > 0 and toks[i - 1] in GROUP_STEMS)):
                d = dimension_for_entity(ent, columns)
                if d is not None:
                    return d["name"]
    return None


def understand(question, columns, state=None, today=None):
    """Structured intent for one message, merged with the previous analysis state for follow-ups.

    Returns {"kind": "analysis"|"report"|"drill"|None, "period", "grain", "group_by", "metrics", "top_n", "sort",
             "additive", "correction", "detail"} — resolved later against the schema by the executor."""
    state = state or {}
    q = str(question or "")
    measures = requested_measures(q, columns)
    if not measures and state and FOLLOW_ADD.search(q):
        # "items bhi" / "customer bhi add karo": a bare entity word in an additive follow-up is a metric request
        ent = next((entity_of(t) for t in tokens(q) if stem(t) in _SYN_OF or stem(t) in ITEM_LIKE), None)
        if ent:
            measures = [{"kind": "count", "entity": ent, "column_hint": None}]
    period = parse_period(q, today)
    grain = wants_grain(q, period)
    dim = mentioned_dimension(q, columns, measures)
    tops = list(FOLLOW_TOP.finditer(q))
    loose = re.search(r"(?<![\d-])(\d{1,3})(?![\d-])", q) if tops and not any(t.group(2) for t in tops) and not parse_period(q, today) else None
    top = next((t for t in tops if t.group(2)), None) or next((t for t in tops if re.match(r"(top|bottom)", t.group(0), re.I)), None) or (tops[0] if loose else None)
    top_n = int(top.group(2)) if top and top.group(2) else (int(loose.group(1)) if loose else (10 if top else None))
    low = bool(LOW_WORDS.search(q))
    if not top_n and dim and SUPERLATIVE_ONLY.search(q) and (low or HIGH_WORDS.search(q)):
        top_n, top = 1, True                                                # "sabse kam sale wala customer" = that one customer
    agg = question_intent(q)["aggregation"]
    if agg in ("avg", "max", "min") and not top_n:
        measures = [dict(m, aggregation=agg) for m in measures]     # "average marks", "highest amount" (a single MAX, not a ranking)
    if re.search(r"\b(distinct|different|unique|alag|alag-alag|types?|kinds?|variety|prakar|kitne tarah)\b", q, re.I):
        measures = [dict(m, distinct=True) if m["kind"] == "count" else m for m in measures]
    additive = bool(FOLLOW_ADD.search(q)) and bool(state)
    correction = bool(re.search(r"\b(nahi|nhi|no|not|galat|wrong|instead|ki jagah|nahi chahiye)\b", q, re.I)) and bool(state)
    short = len(tokens(q)) <= 6
    # "last 2 months ki sale kitni hui?" is one total, not a month-wise table: the period's default breakdown is dropped
    # for a single-measure total question without any breakdown word. "last 12 months sales dikhao" stays a series.
    explain = bool(DATE_ASK.search(q))
    if period and period.get("grain") and not grain and not dim and len(measures) <= 1 and (SCALAR_ASK.search(q) or explain) and not BREAKDOWN_ASK.search(q) and not top_n:
        period = dict(period, grain=None)
    # SERIES → TOTAL follow-up: no new period/grain/dimension/top-N, just "total kar ke batao" / "only total".
    same_metric = not measures or [{k: v for k, v in m.items() if k != "column_hint"} for m in measures] == [{k: v for k, v in m.items() if k not in ("column_hint", "aggregation")} for m in (state.get("metrics") or [])]
    collapse = bool(state) and not additive and bool(COLLAPSE.search(q)) and not grain and not dim and not period and not top_n and same_metric
    concise = collapse and bool(ONLY.search(q))
    intent = {"question": q, "metrics": measures, "new_metrics": list(measures), "period": period, "grain": grain, "group_by": [dim] if dim else [], "top_n": top_n,
              "sort": ("asc" if low else "desc") if top else None, "direction_said": bool(low or HIGH_WORDS.search(q)),
              "additive": additive, "correction": correction, "report": bool(REPORT_Q.search(q)) and not measures,
              "detail": bool(DRILL_Q.search(q) or SHOW_ITEMS.search(q)), "kind": None,
              "collapse": collapse, "concise": concise, "explain_period": explain}
    if intent["report"] and state:
        intent["kind"] = "report"
        return intent
    # Follow-up merge: keep the previous source/period/grain/metrics unless the message changes them.
    if state and (additive or correction or short or collapse or (top_n and not dim and not period) or not (measures or period or grain or dim)):
        merged = {k: state.get(k) for k in ("source_id", "sheet_name", "date_column", "period", "grain", "group_by", "metrics", "filters", "top_n", "sort")}
        if collapse:
            # keep source, period, filters and metrics; change only the operation: one total, no split, no ranking
            merged["grain"], merged["group_by"], merged["top_n"], merged["sort"] = None, [], None, None
            if merged.get("period"):
                merged["period"] = dict(merged["period"], grain=None)
        if period:
            merged["period"] = period
            if period.get("grain"):
                merged["grain"] = grain or period["grain"]                # "last 12 months" carries its own breakdown
            elif not grain and not dim and (short or intent["detail"]):
                merged["grain"] = None                     # "August" → drill into that month, no monthly split
                intent["kind"] = "drill"
        if grain:
            merged["grain"] = grain
        if dim:
            merged["group_by"] = [dim]
            if not top_n:
                merged["top_n"], merged["sort"] = None, None       # "customer wise" after "top 5 …" means all customers
            if not grain and not period:
                merged["grain"] = None if intent["detail"] or short else merged.get("grain")
        if measures:
            if additive:
                merged["metrics"] = list(state.get("metrics") or []) + [m for m in measures if m not in (state.get("metrics") or [])]
            elif correction:
                merged["metrics"] = measures                # "nahi, total items chahiye" replaces the metric reading
            else:
                merged["metrics"] = measures
        if top_n:
            merged["top_n"] = top_n
            merged["sort"] = intent["sort"] if intent["direction_said"] else (state.get("sort") or intent["sort"] or "desc")   # "top 5 only" keeps "kam"
            if not merged.get("group_by") and not dim:
                merged["group_by"] = state.get("group_by") or []
        intent.update({k: v for k, v in merged.items() if k in intent or k in ("source_id", "sheet_name", "date_column", "filters")})
        intent["metrics"], intent["period"], intent["grain"], intent["group_by"] = merged["metrics"] or [], merged["period"], merged["grain"], merged["group_by"] or []
        intent["top_n"], intent["sort"] = merged.get("top_n"), merged.get("sort")
        intent["kind"] = intent["kind"] or "analysis"
        return intent
    if measures or grain or dim or period:
        intent["kind"] = "analysis"
    return intent


TOTAL_CUES = re.compile(r"\b(total|sum|sold|sale|sell|bik[aei]|bech[aei]|quantity|qty|units?|pieces?|pcs|volume|kitna maal|nikl[aei])\b", re.I)
ROWS_CUES = re.compile(r"\b(individual|transactions?|rows?|entries|entry|records?|bills?|invoices?|vouchers?|alag alag bill|no grouping)\b", re.I)


def ambiguity(question, columns, intent, state=None, known=()):
    """The one thing that must be settled before any execution, or None when the request is CLEAR.

    Returns {"key", "question", "options": [{"label", "choice": [key, column] | None, "rewrite": str | None}]}.
    `known` lists choice keys the user already settled for this source (state or memory), which are never re-asked.
    Rules are generic: they look at the request's shape and the table's column roles, never at business names."""
    state = state or {}
    q = str(question or "")
    dims = [c for c in columns if c.get("role") == "dimension"]
    qtys = [c["name"] for c in columns if column_kind(c) == "quantity"]
    # B. "items count": SUM of a quantity column, or the number of distinct item-like values? Both exist → ask.
    for m in intent.get("new_metrics", intent.get("metrics")) or []:       # only what THIS message asks for, not inherited state
        ent = m.get("entity")
        if m.get("kind") == "count" and ent and (ent in ITEM_LIKE or ent == "item") and "count:item" not in known and not m.get("distinct") and not TOTAL_CUES.search(q):
            item_dims = [c["name"] for c in dims if {stem(t) for t in tokens(c["name"])} & ITEM_LIKE]
            if qtys and item_dims:
                return {"key": "count:item", "question": "Items count se kya matlab — total quantity (kitna maal bika), ya alag-alag item types ki ginti?",
                        "options": [{"label": f"Total quantity — SUM of {qtys[0]}", "choice": ["count:item", qtys[0]], "rewrite": None},
                                    {"label": f"Different item types — distinct {item_dims[0]}", "choice": ["count:item", item_dims[0]], "rewrite": None}]}
    # A. A ranking ("top 5 sales wale") with no dimension actually named: per which column, or individual rows?
    if intent.get("top_n") and not intent.get("group_by") and not state.get("group_by") and not ROWS_CUES.search(q):
        full, partial = dimension_matches(q, columns)
        cands = (partial or [c["name"] for c in dims])[:3]
        if cands:
            opts = [{"label": f"{d} wise total", "choice": None, "rewrite": f"{q} ({d} wise total)"} for d in cands]
            opts.append({"label": "Individual transactions (rows)", "choice": None, "rewrite": f"{q} (individual rows, no grouping)"})
            return {"key": "rank:dimension", "question": "Top " + str(intent["top_n"]) + " kis hisaab se — " + " / ".join(cands) + " wise total, ya individual transactions?", "options": opts}
    return None
