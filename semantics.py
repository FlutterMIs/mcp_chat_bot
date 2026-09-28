"""Generic, data-agnostic semantics: what a question asks for, what a column measures, and whether a plan fits.

Nothing here knows a business domain. It maps words → intent (sum/avg/max, ranking, grouping, money vs quantity)
and column names + data profile → measure kind. `validate_plan` raises ValueError with a message the planner can
act on, so wrong sheet / wrong metric / wrong aggregation plans never run silently.
"""
import re

# ---------- lexicons (English + Hinglish). Semantic mappings, not business columns. ----------
MONEY_WORDS = {"amount", "amt", "revenue", "sales", "sale", "turnover", "value", "price", "cost", "fee", "fees", "salary", "wage",
               "wages", "budget", "expense", "expenses", "spend", "income", "profit", "margin", "payment", "paid", "invoice",
               "bill", "billing", "rupee", "rupees", "rs", "inr", "usd", "dollar", "paisa", "paise", "kimat", "keemat", "daam",
               "kharcha", "tankhwah", "kamai", "aamdani", "bikri", "raqam", "rakam", "₹", "$"}
QUANTITY_WORDS = {"qty", "quantity", "quantities", "units", "unit", "pieces", "pcs", "nos", "stock", "stocks", "hours", "hrs",
                  "days", "marks", "score", "scores", "attendance", "headcount", "visits", "clicks", "views", "weight",
                  "kg", "litre", "liters", "volume", "sankhya", "ginti", "tadaad"}
RATE_WORDS = {"rate", "ratio", "percent", "percentage", "pct", "%", "growth", "yield", "cgpa", "gpa", "average"}
IDENT_WORDS = {"id", "code", "no", "number", "num", "sr", "sno", "index", "pin", "mobile", "phone", "gst", "gstin", "pan",
               "aadhar", "aadhaar", "zip", "pincode", "ref", "voucher"}

AGG_WORDS = {
    "avg": {"average", "avg", "mean", "ausat", "aausat", "औसत"},
    "count": {"count", "how many", "number of", "kitne", "kitni", "ginti", "sankhya"},
    "max": {"highest", "maximum", "max", "largest", "biggest", "top", "best", "sabse zyada", "sabse jada", "sabse jyada",
            "sabse bada", "sabse badi", "sabse upar", "sab se zyada", "sabse acha", "sabse accha", "सबसे ज़्यादा", "सबसे ज्यादा"},
    "min": {"lowest", "minimum", "min", "smallest", "least", "bottom", "worst", "sabse kam", "sabse chota", "sabse choti",
            "sabse neeche", "sabse kharab", "सबसे कम"},
    "sum": {"total", "sum", "overall", "kul", "poora", "pura", "कुल", "sab milake", "sab mila ke"},
}
GROUP_WORDS = {"wise", "per", "by", "each", "every", "har", "ke hisaab", "ke hisab", "hisaab se", "hisab se", "anusar", "vaar",
               "war", "breakdown", "split", "distribution", "grouped", "group"}
TIME_UNIT = r"(?:months?|mahin\w*|maheen\w*|days?|din|weeks?|hafte|years?|saal|quarters?|hours?|hrs|minutes?|sec\w*)"
RANK_N = re.compile(r"\b(?:top|bottom|first|last|pehle|aakhri|niche|neeche)\s*(\d{1,3})\b(?!\s*" + TIME_UNIT + r")|\b(\d{1,3})\s*(?:sabse|top|bottom)\b", re.I)
SUPERLATIVE = re.compile(r"\b(highest|lowest|maximum|minimum|max|min|largest|smallest|biggest|best|worst|top|bottom|"
                         r"sabse|sab se|most|least|kaun\s*sa\s*sabse|which\s+one\s+is\s+(?:the\s+)?(?:highest|lowest))\b", re.I)
TIME_WORDS = re.compile(r"\b(today|yesterday|this|last|previous|current|is|pichhl\w*|pichl\w*|iss?|abhi|current|month|year|week|"
                        r"quarter|mahin\w*|saal|hafte|din|\d{4}|jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|"
                        r"nov\w*|dec\w*|ytd|mtd|fy)\b", re.I)
STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are", "was", "were", "ka", "ki", "ke", "ko", "se",
        "mein", "me", "hai", "hain", "h", "kya", "kaun", "kon", "batao", "bta", "btao", "do", "dikhao", "dikha", "bhai", "bro",
        "please", "plz", "mujhe", "muje", "chahiye", "chaiye", "with", "show", "give", "tell", "what", "which", "who", "how",
        "much", "many", "data", "details", "detail", "list", "report", "info", "about", "ke", "wala", "wali", "wale", "sab",
        "all", "total", "sum", "kul"}


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9₹$%]+", str(text or "").lower()) if t]


def stem(t):
    """Tiny plural stemmer so 'sales'~'sale', 'salaries'~'salary', 'items'~'item' match across question and schema."""
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def stems(text):
    return {stem(t) for t in tokens(text)}


def content_tokens(text):
    return [t for t in tokens(text) if t not in STOP and len(t) > 1]


def _has_phrase(q, phrases):
    return any(re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", q) for p in phrases)


# ---------- question intent ----------
def question_intent(question):
    """Structured reading of a question. Keys: aggregation, superlative, top_n, direction, grouping, measure_kind,
    time, comparison, average, count."""
    q = " " + str(question or "").lower() + " "
    toks = set(tokens(q))
    agg = None
    for kind, words in (("avg", AGG_WORDS["avg"]), ("count", AGG_WORDS["count"]), ("max", AGG_WORDS["max"]),
                        ("min", AGG_WORDS["min"]), ("sum", AGG_WORDS["sum"])):
        if _has_phrase(q, words):
            agg = agg or kind
    n = None
    m = RANK_N.search(q)
    if m:
        n = int(m.group(1) or m.group(2))
    direction = "desc" if _has_phrase(q, AGG_WORDS["max"]) else ("asc" if _has_phrase(q, AGG_WORDS["min"]) else None)
    # "kitne ka / kitna ka / kitni ki" = "for how much money" in Hinglish
    money = bool(toks & MONEY_WORDS) or bool(re.search(r"\bkitn[aei]\s+k[aei]\b", q))
    quantity = bool(toks & QUANTITY_WORDS)
    kind = "monetary" if money and not quantity else ("quantity" if quantity and not money else None)
    return {
        "aggregation": agg,
        "superlative": bool(SUPERLATIVE.search(q)) or n is not None,
        "top_n": n,
        "direction": direction,
        "grouping": _has_phrase(q, GROUP_WORDS),
        "measure_kind": kind,
        "time": bool(TIME_WORDS.search(q)),
        "comparison": bool(re.search(r"\b(compare|comparison|vs|versus|mukabl\w*|muqabl\w*|tulna|difference|farak|fark|antar|growth|badh\w*|ghat\w*|change)\b", q)),
        "average": _has_phrase(q, AGG_WORDS["avg"]),
        "count": _has_phrase(q, AGG_WORDS["count"]),
    }


# ---------- column semantics ----------
def column_kind(col):
    """'monetary' | 'quantity' | 'rate' | 'identifier' | 'measure' (neutral) | 'dimension' | 'date' from a column_info dict."""
    name_toks = set(tokens(col.get("name", "")))
    role = col.get("role")
    if role == "date":
        return "date"
    if role != "metric":
        # numeric-looking identifiers are often stored as text; dimensions stay dimensions
        return "dimension"
    if name_toks & IDENT_WORDS:
        # "Bill No", "Invoice ID", "Order Code" are identifiers even though "bill"/"order" sound like money.
        if not (name_toks & (MONEY_WORDS | QUANTITY_WORDS)) or (col.get("integer_like") and (col.get("distinct_ratio") or 0) > 0.9):
            return "identifier"
    if name_toks & RATE_WORDS and not (name_toks & MONEY_WORDS):
        return "rate"
    if name_toks & MONEY_WORDS:
        return "monetary"
    if name_toks & QUANTITY_WORDS:
        return "quantity"
    # No telling name: a unique, integer, consecutive sequence (1,2,3… / 101,102,…) is a row id, not a measure
    n, lo, hi = col.get("numeric_non_null") or 0, col.get("min"), col.get("max")
    if n >= 2 and col.get("integer_like") and (col.get("distinct_ratio") or 0) > 0.98 and lo is not None and hi is not None and (hi - lo + 1) == n:
        return "identifier"
    return "measure"


def annotate_schema(schema):
    """Add 'kind' to every column of a source schema (in place) and return it."""
    tables = schema.get("sheets") or ([{"name": None, "columns": schema.get("columns", [])}] if schema.get("columns") else [])
    for t in tables:
        for c in t.get("columns", []):
            c["kind"] = column_kind(c)
    return schema


def table_of(schema, sheet_name):
    tables = schema.get("sheets") or []
    if tables:
        return next((t for t in tables if t.get("name") == sheet_name), None)
    return {"name": None, "columns": schema.get("columns", [])} if schema.get("columns") else None


def named_columns(question, columns):
    """Columns the question names explicitly (all name tokens present, or a single-token name present)."""
    qt = set(tokens(question))
    out = []
    for c in columns:
        nt = [t for t in tokens(c["name"]) if t not in {"name", "no", "id"}] or tokens(c["name"])
        if nt and all(t in qt for t in nt):
            out.append(c)
    return out


def score_tables(question, schema):
    """How well each table fits the question: table-name tokens count double, column-name tokens once."""
    qt = {stem(t) for t in content_tokens(question)}
    scores = {}
    for t in schema.get("sheets") or []:
        s = 2 * len(qt & stems(t.get("name", "")))
        for c in t.get("columns", []):
            s += len(qt & stems(c["name"]))
        scores[t["name"]] = s
    return scores


# ---------- plan validation ----------
def validate_plan(question, plan, schema):
    """Raise ValueError when the plan contradicts the question in a way code can see. Returns the plan otherwise."""
    if plan.get("status") != "execute" or plan.get("mode") == "text":
        return plan
    schema = annotate_schema(dict(schema))
    intent = question_intent(question)
    table = table_of(schema, plan.get("sheet_name"))
    if table is None:
        return plan
    cols = table.get("columns", [])
    by_name = {c["name"]: c for c in cols}
    metric = plan.get("metric")
    agg = (plan.get("aggregation") or "sum").lower() if plan.get("operation") == "aggregate" else None

    # 1. sheet choice: a table that scores 0 while another matches the words is almost always wrong
    scores = score_tables(question, schema)
    if len(scores) > 1:
        mine, best = scores.get(plan.get("sheet_name"), 0), max(scores.values())
        if mine == 0 and best >= 1:
            better = [n for n, s in scores.items() if s == best]
            raise ValueError(f'Sheet "{plan.get("sheet_name")}" does not match the question words; "{better[0]}" does. Use sheet_name "{better[0]}".')

    # 2. metric vs measure kind and vs explicitly named columns
    if agg in ("sum", "avg", "min", "max") and metric in by_name:
        ck = by_name[metric].get("kind")
        qk = intent["measure_kind"]
        if ck == "identifier":
            raise ValueError(f'"{metric}" is an identifier column, not a measure. Numeric measure columns: {[c["name"] for c in cols if c.get("kind") in ("monetary", "quantity", "measure", "rate")]}.')
        if qk and ck in ("monetary", "quantity") and ck != qk:
            same = [c["name"] for c in cols if c.get("kind") == qk]
            elsewhere = {t["name"]: [c["name"] for c in t["columns"] if c.get("kind") == qk] for t in schema.get("sheets") or [] if t["name"] != table["name"]}
            elsewhere = {k: v for k, v in elsewhere.items() if v}
            hint = (f' Use one of these {qk} columns in this sheet as metric: {same}.' if same else
                    (f' This sheet has no {qk} column. Return the same plan with sheet_name and metric from: {elsewhere} (do not answer out_of_scope).' if elsewhere
                     else f' No {qk} column exists in this source — say so instead of substituting.'))
            raise ValueError(f'The question asks for a {qk} figure but "{metric}" is a {ck} column.' + hint)
        # "units nahi, revenue chahiye" / "not quantity, amount": a negated column can never be the metric
        qtoks = [stem(t) for t in tokens(question)]
        def negated(c):
            ct = {stem(t) for t in tokens(c["name"])}
            return any(t in ct and ((i + 1 < len(qtoks) and qtoks[i + 1] in NEGATION) or (i > 0 and qtoks[i - 1] in {"not", "no", "without", "bina", "except"})) for i, t in enumerate(qtoks))
        if metric in by_name and negated(by_name[metric]):
            raise ValueError(f'The question rules out "{metric}" ("{metric} nahi / not {metric}"); use the other measure the user names.')
        named = [c for c in named_columns(question, cols) if c.get("kind") in ("monetary", "quantity", "measure", "rate") and not negated(c)]
        if len(named) == 1 and named[0]["name"] != metric and not intent["count"]:
            raise ValueError(f'The question names the column "{named[0]["name"]}" but the plan uses "{metric}". Use metric "{named[0]["name"]}".')

    # 3. aggregation vs intent
    if agg:
        if intent["average"] and agg != "avg":
            raise ValueError('The question asks for an average: use aggregation "avg".')
        if intent["superlative"] and agg in ("sum", "avg") and not plan.get("group_by") and not plan.get("sort") and not plan.get("top_n"):
            raise ValueError('The question asks for the highest/lowest: either group_by the entity with sort desc/asc and top_n, '
                             'or use operation "rows" with sort_by the metric — a plain total is not an answer.')
        if intent["superlative"] and plan.get("group_by"):
            plan["sort"] = plan.get("sort") or (intent["direction"] or "desc")
            plan["top_n"] = plan.get("top_n") or intent["top_n"] or (1 if re.search(r"\b(which|kaun|kon|kis)\b", question.lower()) else 10)

    # 4. grouping: a named dimension with grouping words must be the group_by
    if intent["grouping"] and plan.get("operation") == "aggregate":
        qtoks = [stem(t) for t in tokens(question)]
        def counted(c):   # "customer count" / "distinct customers": the dimension is what gets counted, not the grouping
            ct = {stem(t) for t in tokens(c["name"])} - {"name", "no", "id"}
            return any(t in ct and any(x in COUNT_CUES for x in qtoks[max(0, i - 2):i + 3]) for i, t in enumerate(qtoks))
        dims = [c for c in named_columns(question, cols) if c.get("kind") == "dimension" and c["name"] != metric and not counted(c)]
        if len(dims) == 1 and dims[0]["name"] not in (plan.get("group_by") or []) and not (plan.get("date_grain") and intent["time"]):
            raise ValueError(f'The question groups by "{dims[0]["name"]}" (named in the question) but group_by is {plan.get("group_by")}. Use group_by ["{dims[0]["name"]}"].')
    return plan


COUNT_PHRASE = re.compile(r"\b(count|counts|number of|no\.? of|sankhya|ginti|distinct|unique|kitne|kitni|how many)\b", re.I)


NEGATION = {"nahi", "nhi", "na", "not", "no", "mat", "without", "bina", "except", "chhod", "hatao", "hata", "remove"}
GROUP_STEMS = {stem(w) for w in ("wise", "per", "by", "ke", "ka", "ki", "hisaab", "hisab", "anusar", "vaar", "war", "each", "every", "har")}
COUNT_CUES = {"count", "sankhya", "ginti", "distinct", "unique", "number", "kitne", "kitni", "many"}      # not "name(s)": "customer name bata" lists, it does not count


def fuzzy_token(t, vocab):
    """Typo tolerance for words of 4+ letters: 'amiunt'→'amount', 'cunt'→'count', 'itmes'→'item' (edit similarity ≥ 0.8)."""
    import difflib
    if t in vocab or len(t) < 4:
        return t
    m = difflib.get_close_matches(t, [v for v in vocab if v[:1] == t[:1]], n=1, cutoff=0.8)   # typos rarely change the first letter
    return m[0] if m else t


def requested_measures(question, columns):
    """Which measures a question asks for, in schema terms. Data-agnostic: money/quantity lexicons + count-of-<dimension>.
    Returns a list of {"kind": "monetary"|"quantity"|"count", "entity": <dimension stem or None>, "column_hint": ...}.
    Typo-tolerant; a measure followed/preceded by a negation ("units nahi", "not quantity") is not requested."""
    dims = {stem(t): c["name"] for c in columns for t in tokens(c["name"]) if c.get("role") == "dimension" and t not in {"name", "no", "id"}}
    money_s, qty_s = {stem(w) for w in MONEY_WORDS}, {stem(w) for w in QUANTITY_WORDS}
    vocab = list(money_s | qty_s | set(dims) | COUNT_CUES | set(_SYN_OF))
    raw = [stem(t) for t in tokens(question)]
    toks = [fuzzy_token(t, vocab) if t not in NEGATION else t for t in raw]
    out, seen = [], set()
    for i, t in enumerate(toks):
        item = None
        if (i + 1 < len(toks) and toks[i + 1] in NEGATION) or (i > 0 and toks[i - 1] in {"not", "no", "without", "bina", "except"}):
            continue
        if t in money_s:
            item = ("monetary", None)
        elif t in qty_s:
            item = ("quantity", None)
        elif t in ITEM_LIKE and any(x in SOLD_CUES for x in toks[max(0, i - 2):i + 3]):
            item = ("quantity", None)                                  # "items sold" / "units bike" = the quantity column
        elif (t in dims or t in _SYN_OF) and not (i + 1 < len(toks) and toks[i + 1] in GROUP_STEMS) and (
                any(x in COUNT_CUES for x in toks[max(0, i - 3):i + 3]) or any(x in {"total", "sum", "kul"} for x in toks[max(0, i - 1):i + 2])):
            item = ("count", entity_of(t) if t in _SYN_OF else t)     # "customer count" / "total items" — but not "customer wise"
        if item and item not in seen:
            seen.add(item)
            hint = dims.get(item[1]) if item[1] else None
            if item[0] in ("monetary", "quantity"):
                # "closing stock batao" names the column in full: that column, not a sibling of the same kind
                hint = next((c["name"] for c in named_columns(question, columns) if column_kind(c) == item[0]), None)
            out.append({"kind": item[0], "entity": item[1], "column_hint": hint})
    return out


ITEM_LIKE = {"item", "product", "unit", "piece", "pc", "good", "maal", "saman", "sku", "article", "qty", "quantity"}
SOLD_CUES = {"sold", "sell", "sale", "bika", "bike", "biki", "bech", "becha", "beche", "nikla", "nikle", "nikli", "dispatched", "shipped"}
# What users call an entity vs what a column may be called. Concepts, not business columns.
ENTITY_SYNONYMS = {
    "customer": {"customer", "party", "client", "buyer", "account", "consumer", "dealer", "distributor", "retailer", "grahak", "graahak"},
    "item": ITEM_LIKE - {"qty", "quantity"},
    "employee": {"employee", "staff", "worker", "agent", "salesman", "salesmen", "salesperson", "salespersons", "seller", "sellers", "saler", "salers",
                 "executive", "karmchari", "person", "rep", "representative"},
    "student": {"student", "pupil", "learner", "vidyarthi"},
    "vendor": {"vendor", "supplier"},
    "city": {"city", "town", "location", "place", "shahar", "shehar"},
    "state": {"state", "rajya", "pradesh", "region", "zone"},
    "category": {"category", "categories", "group", "segment", "type", "kism", "shreni"},
    "order": {"order", "invoice", "bill", "voucher", "transaction", "transactions", "txn", "entry", "entries", "record", "records", "row", "rows"},
}
_SYN_OF = {stem(w): k for k, ws in ENTITY_SYNONYMS.items() for w in ws}


def entity_of(token):
    """Canonical entity for a word ('party' → 'customer'), or the word itself."""
    return _SYN_OF.get(stem(token), stem(token))


def dimension_for_entity(entity, cols):
    """The dimension column that represents an entity: direct name match first, then synonyms."""
    syn = {stem(w) for w in ENTITY_SYNONYMS.get(entity, set())} | {stem(entity)}
    dims = [c for c in cols if c.get("kind", column_kind(c)) == "dimension"]
    exact = [c for c in dims if entity in {stem(t) for t in tokens(c["name"])}]
    if exact:
        return exact[0]
    hits = [c for c in dims if {stem(t) for t in tokens(c["name"])} & syn]
    return hits[0] if hits else None


class AmbiguousMeasure(ValueError):
    """A column choice the user must make. `key` identifies the measure ('quantity', 'monetary', 'count:customer'),
    `candidates` are the real column names to choose from."""
    def __init__(self, msg, candidates=None, key=None):
        super().__init__(msg)
        self.candidates, self.key = candidates or [], key


def measure_key(m):
    return m["kind"] + (f":{m['entity']}" if m.get("entity") else "")


KNOWN_WORDS = None


def only_known_words(question, columns, sheet_names=None):
    """True when every content word of the question is vocabulary this module understands (measure words,
    aggregation/count/group cues, entity synonyms, column names) — i.e. nothing that could be a filter value."""
    global KNOWN_WORDS
    if KNOWN_WORDS is None:
        KNOWN_WORDS = {stem(w) for ws in (MONEY_WORDS, QUANTITY_WORDS, COUNT_CUES, ITEM_LIKE, NEGATION, GROUP_STEMS, STOP) for w in ws}
        KNOWN_WORDS |= {stem(w) for ws in AGG_WORDS.values() for w in ws} | {stem(w) for ws in ENTITY_SYNONYMS.values() for w in ws}
        KNOWN_WORDS |= {stem(w) for w in ("total", "overall", "sold", "sell", "bika", "bikri", "batao", "bata", "dikhao", "dikha", "do", "chahiye", "kitna", "kitni", "kitne",
                                           "bhai", "bro", "yaar", "please", "plz", "mujhe", "mera", "meri", "hai", "hain", "kya", "kaun", "kon", "abhi", "ab", "tak", "all", "sab", "sabhi", "pura", "poora", "grand",
                                           "sheet", "tab", "table", "se", "wali", "wala", "wale", "me", "mein",
                                           # Hinglish filler that carries no data meaning
                                           "hum", "ham", "tum", "aap", "bta", "btao", "sakte", "sakta", "sakti", "isme", "usme", "inme", "inke", "iske", "uske", "unke", "jiske", "jinke",
                                           "jisne", "jinhone", "hua", "hue", "hui", "kis", "kise", "kisko", "kisne", "ko", "ye", "yeh", "wo", "woh", "jo", "bahut", "bohot", "bhut", "thoda",
                                           "zara", "ek", "fir", "phir", "aur", "and", "or", "only", "bas", "sirf", "kon", "kaun", "kya", "kyu", "kyun", "bhi", "abhi", "ab", "tha", "the",
                                           "thi", "hoga", "hogi", "honge", "rha", "rhi", "raha", "rahi", "rahe", "karo", "kar", "karta", "karti", "karte", "kiya", "kiye", "de", "dena", "dedo",
                                           "batana", "bataye", "batao", "dikhana", "chahiye", "chaiye", "mila", "milega", "nikal", "nikalo", "top", "bottom", "best", "worst", "wise", "sabse", "sabh", "sab",
                                           "kam", "zyada", "jada", "jyada", "highest", "lowest", "least", "most", "maximum", "minimum", "max", "min", "bekar", "kharab", "achha", "accha",
                                           "make", "it", "change", "badlo", "badal", "instead", "same", "again", "dobara", "wapas", "details", "detail", "dikha", "list", "show", "give",
                                           "tell", "want", "need", "please", "ok", "okay", "haan", "nahi", "nhi", "ki", "ka", "ke", "wala", "vale", "hi", "to", "toh", "na",
                                           "add", "jodo", "jod", "include", "plus", "saath", "sath", "too", "also", "as", "well", "remove", "hatao", "hata", "without", "bina", "sirf",
                                           "vs", "versus", "cross", "crosstab", "pivot", "matrix", "by",
                                           "sir", "ji", "esa", "aisa", "kuchh", "kuch", "chaiye", "chahiye", "moka", "mauka", "usak", "uska", "usme", "usmein", "mujhe", "fir", "phir",
                                           "no", "number", "one", "compare", "comparison", "difference", "farak", "fark", "antar", "share", "percentage", "percent", "hissa", "contribution")}
        import calendar
        KNOWN_WORDS |= {stem(w) for w in ("month", "months", "mahina", "mahine", "maheena", "maheene", "monthly", "day", "days", "din", "daily", "week", "weeks", "hafta", "hafte", "weekly",
                                           "year", "years", "saal", "yearly", "annual", "date", "dates", "datewise", "today", "aaj", "yesterday", "kal", "last", "this", "current", "previous",
                                           "past", "pichle", "pichla", "pichhle", "gaye", "beete", "is", "iss", "chalu", "running", "mtd", "ytd", "period", "range", "between", "from", "till",
                                           "tak", "quarter", "fy", "till", "upto", "since", "se", "wise", "trend", "graph", "graf", "chart", "plot", "visual", "report", "summary", "sept")}
        KNOWN_WORDS |= {stem(m.lower()) for m in list(calendar.month_name) + list(calendar.month_abbr) if m}
        KNOWN_WORDS |= {stem(w) for w in ("janvari", "farvari", "faravari", "aprail", "mai", "joon", "julai", "agast", "sitambar", "aktoobar", "navambar", "disambar")}
        KNOWN_WORDS |= {stem(w) for w in ("si", "sa", "konsi", "kaunsi", "konsa", "kaunsa", "kab", "kitne", "column", "columns", "colume", "col", "field", "fields", "moment",
                                           "bad", "baad", "pehle", "pahle", "usse", "isse", "jaise", "waise", "matlab", "yani", "yaani", "means", "mtlb", "for", "of", "the", "in", "on",
                                           "uska", "uski", "iska", "iski", "unka", "unki", "sabka", "sabki", "poori", "puri", "sari", "saari", "kuch", "koi", "har", "each", "every",
                                           "karke", "karo", "kr", "krke", "ho", "hoon", "hu", "dedo", "bolo", "bol", "batade", "bta", "number", "numbers", "figure", "amount")}
        from periods import NUM_WORDS
        KNOWN_WORDS |= {stem(w) for w in NUM_WORDS}
    from periods import resolve_date_expression
    p = resolve_date_expression(question)
    period_words = set(tokens(p["date_expression"])) if p else set()          # "last two months" / "August September" are understood by the resolver
    col_words = {stem(t) for c in columns for t in tokens(c["name"])} | {stem(t) for n in (sheet_names or []) for t in tokens(n)}
    return all(stem(t) in KNOWN_WORDS or stem(t) in col_words or t.isdigit() or t in period_words for t in content_tokens(question))


def knowledge_words(question, columns):
    """Content words that are neither analysis vocabulary, nor column/sheet names, nor a period: the part of a question
    that could only be answered from documents ("return policy", "warranty", "SOP")."""
    only_known_words("", columns)                                 # builds KNOWN_WORDS (month names and time words included)
    col_words = {stem(t) for c in columns for t in tokens(c["name"])}
    return [t for t in content_tokens(question) if stem(t) not in KNOWN_WORDS and stem(t) not in col_words and not t.isdigit()]


def value_words(question, columns):
    """Words of the question that look like data values rather than vocabulary — a filter the planner must handle:
    a token seen among a dimension column's sample values, a code with digits and letters, or a capitalised name
    mid-sentence ("Acme ka last 2 months sale"). Typos and unknown Hinglish filler do not count."""
    only_known_words("", columns)                                 # builds KNOWN_WORDS
    samples = {t for c in columns if c.get("role") == "dimension" for v in c.get("sample_values") or [] for t in tokens(v) if len(t) > 2}
    col_words = {stem(t) for c in columns for t in tokens(c["name"])}
    words = re.findall(r"[A-Za-z0-9₹$%][\w'-]*", str(question or ""))
    out = []
    for i, w in enumerate(words):
        t = w.lower()
        if stem(t) in KNOWN_WORDS or stem(t) in col_words or t in STOP or t.isdigit():
            continue
        if t in samples or (re.search(r"\d", t) and re.search(r"[a-z]", t)) or (i > 0 and w[:1].isupper() and not w.isupper()):
            out.append(w)
    return out


SECONDARY = {"alt", "alternate", "alternative", "secondary", "old", "prev", "previous", "backup", "dup", "duplicate"}


def primary_only(cands):
    """Drop columns whose name marks them as secondary (ALT_QTY, Old Amount) when a primary one exists."""
    prim = [c for c in cands if not (set(tokens(c["name"])) & SECONDARY)]
    return prim if 0 < len(prim) < len(cands) else cands


def resolve_measures(measures, columns, choices=None):
    """`choices` maps measure_key → column chosen earlier by the user (remembered per source)."""
    choices = choices or {}
    """Map requested measures to concrete (column, aggregation, label) using the table's columns only.
    - monetary / quantity → the single column of that kind (several → ask, none → say so)
    - count of an item-like entity → SUM of the quantity column when one exists (items sold), else distinct count
    - count of any other entity → count_distinct of that dimension column
    Raises AmbiguousMeasure with a user-facing message instead of guessing."""
    cols = [dict(c, kind=column_kind(c)) for c in columns]
    def by_kind(k):
        return [c for c in cols if c["kind"] == k]
    out = []
    seen = set()
    for m in measures:
        kind, ent = m["kind"], m.get("entity")
        chosen = choices.get(measure_key(m))
        if chosen and any(c["name"] == chosen for c in cols):
            c = next(c for c in cols if c["name"] == chosen)
            agg = "count_distinct" if c["role"] == "dimension" else "sum"
            if agg == "sum":      # a quantity/amount column chosen by the user
                k = "monetary" if c["kind"] == "monetary" else "quantity"
                out.append({"column": c["name"], "aggregation": "sum", "label": ("Sales Amount" if k == "monetary" else "Items Count") if len(measures) > 1 else c["name"], "kind": k})
            else:
                out.append({"column": c["name"], "aggregation": agg, "label": f"{str(ent).title() if ent else c['name']} Count", "kind": "count"})
            continue
        if kind in ("monetary", "quantity"):
            cands = primary_only(by_kind(kind))
            if len(cands) > 1 and m.get("column_hint") in [c["name"] for c in cands]:
                cands = [c for c in cands if c["name"] == m["column_hint"]]           # the column the user spelled out
            if len(cands) == 1:
                c = cands[0]
                out.append({"column": c["name"], "aggregation": "sum", "label": ("Sales Amount" if kind == "monetary" else "Items Count") if len(measures) > 1 else c["name"], "kind": kind})
            elif not cands:
                raise AmbiguousMeasure(f"Is data mein koi {'amount/paise' if kind == 'monetary' else 'quantity'} wala column nahi hai — kaunsa column lena hai? Columns: {[c['name'] for c in cols if c['role'] == 'metric']}")
            else:
                raise AmbiguousMeasure(f"{'Amount' if kind == 'monetary' else 'Quantity'} ke liye ek se zyada columns hain — kaunsa lena hai?", [c["name"] for c in cands], measure_key(m))
        else:
            dim = dimension_for_entity(ent, cols)
            if ent in ITEM_LIKE | {"item"} and by_kind("quantity") and not m.get("distinct"):
                q = primary_only(by_kind("quantity"))
                if len(q) > 1:
                    raise AmbiguousMeasure("Items count ke liye quantity columns ek se zyada hain — kaunsa lena hai?", [c["name"] for c in q], measure_key(m))
                out.append({"column": q[0]["name"], "aggregation": "sum", "label": "Items Count" if len(measures) > 1 else q[0]["name"], "kind": "quantity"})
            elif dim is not None:
                word = "Transaction" if ent == "order" else (str(ent).title() if ent else dim["name"])
                out.append({"column": dim["name"], "aggregation": "count_distinct", "label": f"{word} Count", "kind": "count"})
            elif ent == "order":
                # "transaction count" with no invoice/voucher id column: the number of rows (counted on the most complete column)
                col = next((c for c in cols if c["role"] == "date"), cols[0] if cols else None)
                if col is None:
                    raise AmbiguousMeasure("Is table mein koi column nahi hai.")
                out.append({"column": col["name"], "aggregation": "count", "label": "Transaction Count", "kind": "count"})
            else:
                raise AmbiguousMeasure(f"'{ent}' ke liye koi column nahi mila. Available: {[c['name'] for c in cols]}")
    deduped = []
    for m in out:               # "items count" + "quantity" both → SUM(Qty): keep one column
        key = (m["column"], m["aggregation"])
        if key not in seen:
            seen.add(key)
            deduped.append(m)
    return deduped


def is_pure_complaint(question, schema):
    """'ye galat hai' → re-check the previous answer. 'galat hai, amount chahiye' → a correction that must be re-planned."""
    qt = set(content_tokens(question))
    if len(tokens(question)) > 12:
        return False
    col_toks = set()
    for t in (schema or {}).get("sheets") or []:
        col_toks |= {tok for c in t.get("columns", []) for tok in tokens(c["name"])}
        col_toks |= set(tokens(t.get("name", "")))
    for c in (schema or {}).get("columns") or []:
        col_toks |= set(tokens(c["name"]))
    signal = MONEY_WORDS | QUANTITY_WORDS | RATE_WORDS | set().union(*AGG_WORDS.values()) | GROUP_WORDS | col_toks | ITEM_LIKE | set(_SYN_OF) | {"total", "kul", "sum", "sheet", "table", "column"}
    qs = {stem(t) for t in qt}
    return not (qt & signal) and not (qs & {stem(w) for w in signal}) and not re.search(r"\d", question)


def plan_diff(old, new):
    """Fields that changed between two plans (for verifying that a correction was applied)."""
    keys = ["source_id", "sheet_name", "operation", "metric", "aggregation", "group_by", "filters", "date_column", "date_grain",
            "date_from", "date_to", "sort", "top_n", "columns", "sort_by", "mode"]
    return {k: (old.get(k), new.get(k)) for k in keys if (old or {}).get(k) != (new or {}).get(k)}
