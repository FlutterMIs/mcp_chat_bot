"""Understanding layer: turn a user message plus the conversation's analysis state into a structured intent.

Everything here is deterministic and data-agnostic. It knows about time phrases ("last 12 months", "August",
"pichle mahine"), follow-up forms ("X bhi add karo", "top 10", "report bana do", "<month> wala dikhao") and
corrections — but nothing about any business domain. Column choices come from `semantics.resolve_measures`.
"""
import calendar
import re
from datetime import date, timedelta

from semantics import COUNT_CUES, GROUP_STEMS, column_kind, ITEM_LIKE, NEGATION, _SYN_OF, content_tokens, entity_of, question_intent, requested_measures, stem, tokens

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS.update({"sept": 9, "janvari": 1, "farvari": 2, "faravari": 2, "march": 3, "aprail": 4, "mai": 5, "joon": 6, "julai": 7, "agast": 8, "sitambar": 9,
               "aktoobar": 10, "navambar": 11, "disambar": 12, "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
               "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "अक्टूबर": 10, "नवंबर": 11, "दिसंबर": 12})
_MONTH_RE = "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))


def _month_bounds(y, m):
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def parse_period(question, today=None):
    """Relative/absolute time phrases → {"label", "from", "to", "grain"} (ISO dates) or None.
    'last 12 months' = the 12 full calendar months ending with the current one (zero months included later)."""
    today = today or date.today()
    q = " " + str(question or "").lower() + " "
    m = re.search(r"\b(?:last|pichl[ae]|pichhl[ae]|previous|past|gaye|beete)\s+(\d{1,2})\s*(month|months|mahin\w*|maheen\w*|day|days|din|week|weeks|hafte|year|years|saal)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith(("month", "mah")):
            y, mo = today.year, today.month
            start_y, start_m = (y * 12 + mo - 1 - (n - 1)) // 12, (y * 12 + mo - 1 - (n - 1)) % 12 + 1
            return {"label": f"last {n} months", "from": date(start_y, start_m, 1).isoformat(), "to": today.isoformat(), "grain": "month", "periods": n}
        if unit.startswith(("day", "din")):
            return {"label": f"last {n} days", "from": (today - timedelta(days=n - 1)).isoformat(), "to": today.isoformat(), "grain": "day", "periods": n}
        if unit.startswith(("week", "haft")):
            return {"label": f"last {n} weeks", "from": (today - timedelta(days=7 * n - 1)).isoformat(), "to": today.isoformat(), "grain": "day"}
        return {"label": f"last {n} years", "from": date(today.year - n + 1, 1, 1).isoformat(), "to": today.isoformat(), "grain": "year", "periods": n}
    if re.search(r"\b(last|pichl[ae]|pichhl[ae]|previous|gaya|gaye)\s+(month|mahin\w*|maheen\w*)\b", q):
        y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        a, b = _month_bounds(y, mo)
        return {"label": "last month", "from": a.isoformat(), "to": b.isoformat(), "grain": None}
    if re.search(r"\b(this|current|is|iss|chalu|abhi ka|running)\s+(month|mahin\w*|maheen\w*)\b|\bcurrent month\b|\bmtd\b", q):
        a, _ = _month_bounds(today.year, today.month)
        return {"label": "this month", "from": a.isoformat(), "to": today.isoformat(), "grain": None}
    if re.search(r"\b(last|pichl[ae]|pichhl[ae]|previous)\s+(year|saal)\b", q):
        return {"label": "last year", "from": date(today.year - 1, 1, 1).isoformat(), "to": date(today.year - 1, 12, 31).isoformat(), "grain": None}
    if re.search(r"\b(this|current|is|iss)\s+(year|saal)\b|\bytd\b", q):
        return {"label": "this year", "from": date(today.year, 1, 1).isoformat(), "to": today.isoformat(), "grain": None}
    m = re.search(r"(?<!\d)(20\d{2})-(\d{2})(?!\d)", q)
    if m and 1 <= int(m.group(2)) <= 12:
        y, mo = int(m.group(1)), int(m.group(2))
        a, b = _month_bounds(y, mo)
        return {"label": f"{calendar.month_name[mo]} {y}", "from": a.isoformat(), "to": min(b, today).isoformat() if (y, mo) == (today.year, today.month) else b.isoformat(), "grain": None}
    m = re.search(r"\b(" + _MONTH_RE + r")\b\.?\s*(\d{4})?", q)
    if m and m.group(1) in MONTHS:
        mo = MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (today.year if mo <= today.month else today.year - 1)
        a, b = _month_bounds(y, mo)
        return {"label": f"{calendar.month_name[mo]} {y}", "from": a.isoformat(), "to": min(b, today).isoformat() if (y, mo) == (today.year, today.month) else b.isoformat(), "grain": None}
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", q)
    if m and not re.search(r"\d{4}-\d{2}", q):
        y = int(m.group(1))
        return {"label": str(y), "from": date(y, 1, 1).isoformat(), "to": (today if y == today.year else date(y, 12, 31)).isoformat(), "grain": None}
    return None


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


def wants_grain(question):
    q = str(question or "").lower()
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
    grain = wants_grain(q)
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
    intent = {"question": q, "metrics": measures, "new_metrics": list(measures), "period": period, "grain": grain, "group_by": [dim] if dim else [], "top_n": top_n,
              "sort": ("asc" if low else "desc") if top else None, "direction_said": bool(low or HIGH_WORDS.search(q)),
              "additive": additive, "correction": correction, "report": bool(REPORT_Q.search(q)) and not measures,
              "detail": bool(DRILL_Q.search(q) or SHOW_ITEMS.search(q)), "kind": None}
    if intent["report"] and state:
        intent["kind"] = "report"
        return intent
    # Follow-up merge: keep the previous source/period/grain/metrics unless the message changes them.
    if state and (additive or correction or short or (top_n and not dim and not period) or not (measures or period or grain or dim)):
        merged = {k: state.get(k) for k in ("source_id", "sheet_name", "date_column", "period", "grain", "group_by", "metrics", "filters", "top_n", "sort")}
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
