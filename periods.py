"""The one date-period resolver. Every path (understanding layer, LLM planner, agent steps, charts, answers) gets its
dates from here, so "last 2 months" can only ever mean one thing.

    resolve_date_expression("last 2 months", date(2026, 9, 27)) ->
    {"date_expression": "last 2 months", "reference_date": "2026-09-27", "period_type": "last_n_calendar_months",
     "label": "last 2 months", "from": "2026-08-01", "to": "2026-09-27", "grain": "month", "n": 2,
     "periods": [{"label": "August 2026", "start": "2026-08-01", "end": "2026-08-31"},
                 {"label": "September 2026", "start": "2026-09-01", "end": "2026-09-27"}]}

Business rules (documented in README):
  previous month        "last month" / "pichle mahine" / "previous month"  -> the full previous calendar month
  current month to date "this month" / "is mahine" / "MTD"                   -> 1st of this month -> today
  last N months         "last 2 months" / "pichle do mahine"                 -> the N calendar months ending with the current one
                                                                                (never today - N*30 days)
  a named month         "August"                                            -> that calendar month (this year if it has started,
                                                                                else last year); the current month is cut at today
  several named months  "August September", "Aug aur Sep", "August se September tak"
                        -> each calendar month plus their combined range (see `asks`)
The LLM may interpret language; the dates always come from here. Hindi/Hinglish, number words ("two", "do", "teen")
and the usual voice-transcription slips ("moment" for month) are understood.
"""
import calendar
import re
from datetime import date, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS.update({"sept": 9, "janvari": 1, "farvari": 2, "faravari": 2, "march": 3, "aprail": 4, "mai": 5, "joon": 6, "julai": 7, "agast": 8, "sitambar": 9,
               "aktoobar": 10, "navambar": 11, "disambar": 12, "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
               "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "नवंबर": 11, "दिसंबर": 12})
_MONTH_RE = "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))

NUM_WORDS = {"one": 1, "ek": 1, "two": 2, "do": 2, "three": 3, "teen": 3, "tin": 3, "four": 4, "char": 4, "chaar": 4, "five": 5, "paanch": 5, "panch": 5,
             "six": 6, "chhe": 6, "che": 6, "chah": 6, "seven": 7, "saat": 7, "sat": 7, "eight": 8, "aath": 8, "ath": 8, "nine": 9, "nau": 9, "ten": 10, "das": 10, "dus": 10,
             "eleven": 11, "gyarah": 11, "twelve": 12, "barah": 12, "baarah": 12, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "छह": 6, "बारह": 12}
_NUM = r"(\d{1,2}|" + "|".join(sorted(map(re.escape, NUM_WORDS), key=len, reverse=True)) + r")"
_LAST = r"(?:last|pichl[ae]|pichhl[ae]|pichle|previous|past|gaye|gaya|beete|bite|पिछले|पिछला|पिछली|last few)"
_THIS = r"(?:this|current|is|iss|chalu|abhi ka|abhi wala|running|इस|ये|yeh?)"
_MONTH_U = r"(?:months?|mnths?|monts?|moment|mahin\w*|maheen\w*|mahina|mahino|महीन\w*|माह)"
_DAY_U = r"(?:days?|din|दिन)"
_WEEK_U = r"(?:weeks?|hafte|hafta|haftey|सप्ताह|हफ्ते)"
_YEAR_U = r"(?:years?|saal|sal|वर्ष|साल)"
_CONJ = re.compile(r"\s*(?:\baur\b|\band\b|\bor\b|&|\+|,|\bplus\b|\btatha\b|\bevam\b|\bऔर\b)\s*", re.I)
_RANGE_JOIN = re.compile(r"\b(?:se|to|till|tak|through|thru|-|–|—|से)\b", re.I)


def _bounds(y, m):
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _iso(d):
    return d.isoformat()


def _month_periods(y0, m0, y1, m1, today):
    """Calendar months from (y0, m0) to (y1, m1) inclusive; the current month is cut at today."""
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        a, b = _bounds(y, m)
        if (y, m) == (today.year, today.month):
            b = min(b, today)
        out.append({"label": f"{calendar.month_name[m]} {y}", "start": _iso(a), "end": _iso(b)})
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


def _num(s):
    return int(s) if s.isdigit() else NUM_WORDS[s]


def _result(expression, today, period_type, label, start, end, grain=None, n=None, periods=None, asks=None):
    out = {"date_expression": expression.strip(), "reference_date": _iso(today), "period_type": period_type, "label": label,
           "from": _iso(start), "to": _iso(end), "grain": grain, "n": n,
           "periods": periods or [{"label": label, "start": _iso(start), "end": _iso(end)}]}
    if asks:
        out["asks"] = asks
    return out


def resolve_date_expression(text, reference_date=None):
    """The period a message talks about, or None when it names no time. See the module docstring for the rules."""
    today = reference_date or date.today()
    q = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    named = _named_months(q, today)
    if named and named.get("asks"):
        return named                      # "last 2 month yani August September ki aur August ki": the named months say exactly what is wanted
    m = re.search(r"\b" + _LAST + r"\s+" + _NUM + r"\s*(" + _MONTH_U + "|" + _DAY_U + "|" + _WEEK_U + "|" + _YEAR_U + r")\b", q)
    if m:
        n, unit = _num(m.group(1)), m.group(2)
        if re.fullmatch(_MONTH_U, unit):
            idx = today.year * 12 + today.month - 1 - (n - 1)
            y0, m0 = idx // 12, idx % 12 + 1
            return _result(m.group(0), today, "last_n_calendar_months", f"last {n} months", date(y0, m0, 1), today, grain="month", n=n,
                           periods=_month_periods(y0, m0, today.year, today.month, today))
        if re.fullmatch(_DAY_U, unit):
            return _result(m.group(0), today, "last_n_days", f"last {n} days", today - timedelta(days=n - 1), today, grain="day", n=n)
        if re.fullmatch(_WEEK_U, unit):
            return _result(m.group(0), today, "last_n_weeks", f"last {n} weeks", today - timedelta(days=7 * n - 1), today, grain="day", n=n)
        years = [{"label": str(y), "start": _iso(date(y, 1, 1)), "end": _iso(min(date(y, 12, 31), today))} for y in range(today.year - n + 1, today.year + 1)]
        return _result(m.group(0), today, "last_n_years", f"last {n} years", date(today.year - n + 1, 1, 1), today, grain="year", n=n, periods=years)
    m = re.search(r"(?<!\bnext )(?<!\bagle )(?<!\baane wale )\b" + _NUM + r"\s*" + _MONTH_U + r"\b", q)
    if m and not re.search(r"\b(top|bottom|best|worst|sabse)\b", q[:m.start()]):
        n = _num(m.group(1))                                  # "do mahine ka total" / "3 months ki sale": the last N calendar months
        idx = today.year * 12 + today.month - 1 - (n - 1)
        y0, m0 = idx // 12, idx % 12 + 1
        return _result(m.group(0), today, "last_n_calendar_months", f"last {n} months", date(y0, m0, 1), today, grain="month", n=n,
                       periods=_month_periods(y0, m0, today.year, today.month, today))
    m = re.search(r"\b" + _LAST + r"\s+" + _MONTH_U + r"\b", q)
    if m:
        y, mo = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        a, b = _bounds(y, mo)
        return _result(m.group(0), today, "previous_month", "last month", a, b, periods=[{"label": f"{calendar.month_name[mo]} {y}", "start": _iso(a), "end": _iso(b)}])
    m = re.search(r"\b" + _THIS + r"\s+" + _MONTH_U + r"\b|\bcurrent month\b|\bmtd\b|\bmonth to date\b", q)
    if m:
        a, _ = _bounds(today.year, today.month)
        return _result(m.group(0), today, "current_month_to_date", "this month", a, today,
                       periods=[{"label": f"{calendar.month_name[today.month]} {today.year}", "start": _iso(a), "end": _iso(today)}])
    m = re.search(r"\b" + _LAST + r"\s+" + _YEAR_U + r"\b", q)
    if m:
        return _result(m.group(0), today, "previous_year", "last year", date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    m = re.search(r"\b" + _THIS + r"\s+" + _YEAR_U + r"\b|\bytd\b|\byear to date\b", q)
    if m:
        return _result(m.group(0), today, "current_year_to_date", "this year", date(today.year, 1, 1), today)
    m = re.search(r"\b(today|aaj|आज)\b", q)
    if m and not re.search(r"\b(till|tak|upto|up to|se)\s+(today|aaj)\b|\b(today|aaj)\s+tak\b", q):
        return _result(m.group(0), today, "day", "today", today, today)
    m = re.search(r"\b(yesterday|kal ka|kal ki|kal ke|kal wala)\b", q)
    if m:
        d = today - timedelta(days=1)
        return _result(m.group(0), today, "day", "yesterday", d, d)
    m = re.search(r"(?<!\d)(20\d{2})-(\d{2})(?!\d)", q)
    if m and 1 <= int(m.group(2)) <= 12:
        y, mo = int(m.group(1)), int(m.group(2))
        a, b = _bounds(y, mo)
        b = min(b, today) if (y, mo) == (today.year, today.month) else b
        return _result(m.group(0), today, "calendar_month", f"{calendar.month_name[mo]} {y}", a, b)
    if named:
        return named
    m = re.search(r"(?<!\d)(20\d{2})(?!\d)", q)
    if m and not re.search(r"\d{4}-\d{2}", q):
        y = int(m.group(1))
        return _result(m.group(0), today, "calendar_year", str(y), date(y, 1, 1), today if y == today.year else date(y, 12, 31))
    return None


def _named_months(q, today):
    """'August', 'August 2025', 'August September', 'Aug aur Sep', 'August se September tak',
    'August September ka total aur August ka total' (a compound request → several `asks`)."""
    pat = re.compile(r"\b(" + _MONTH_RE + r")\b\.?\s*(\d{4})?", re.I)
    mentions = [(m.start(), MONTHS[m.group(1).lower()], int(m.group(2)) if m.group(2) else None) for m in pat.finditer(q) if m.group(1).lower() in MONTHS]
    if not mentions:
        return None

    def year_of(mo, y):
        return y if y else (today.year if mo <= today.month else today.year - 1)

    def span(ms):     # ms: [(mo, y)] → (start, end, label) covering all of them, month by month
        keys = sorted({(year_of(mo, y), mo) for mo, y in ms})
        (y0, m0), (y1, m1) = keys[0], keys[-1]
        pers = _month_periods(y0, m0, y1, m1, today)
        if len(keys) == 1:
            label = pers[0]["label"]
        else:
            same_year = y0 == y1
            names = [calendar.month_name[mo] for _, mo in keys]
            label = (" + ".join(names) + f" {y0}") if same_year else " + ".join(f"{calendar.month_name[mo]} {y}" for y, mo in keys)
        return pers[0]["start"], pers[-1]["end"], label, pers

    # Split the sentence into conjunction segments; months inside one segment form one combined period.
    segments, pos = [], 0
    for cm in _CONJ.finditer(q):
        segments.append((pos, cm.start()))
        pos = cm.end()
    segments.append((pos, len(q)))
    groups = []
    for a, b in segments:
        ms = [(mo, y) for p, mo, y in mentions if a <= p < b]
        if ms:
            groups.append(ms)
    # "August and September" / "Aug, Sep": single months joined by a conjunction are also asked together.
    asks = []
    for g in groups:
        s, e, label, _ = span(g)
        if (s, e) not in [(x["from"], x["to"]) for x in asks]:
            asks.append({"label": label, "from": s, "to": e})
    distinct = sorted({(year_of(mo, y), mo) for g in groups for mo, y in g})
    s, e, label, pers = span([(mo, y) for g in groups for mo, y in g])
    if len(distinct) >= 2 and (s, e) not in [(x["from"], x["to"]) for x in asks]:
        asks.append({"label": label, "from": s, "to": e})
    expression = q[mentions[0][0]:].strip()[:60]
    if len(distinct) == 1:
        return _result(expression, today, "calendar_month", label, date.fromisoformat(s), date.fromisoformat(e), periods=pers)
    return _result(expression, today, "explicit_months", label, date.fromisoformat(s), date.fromisoformat(e), periods=pers, asks=asks if len(asks) >= 2 else None)


def period_metadata(period):
    """The period block every analytical result carries (see README §Period metadata)."""
    if not period:
        return None
    return {k: period.get(k) for k in ("date_expression", "reference_date", "period_type", "label", "from", "to", "periods", "asks") if period.get(k) is not None}


def describe(period, ddmmyyyy=None):
    """'Last 2 months: August 2026 (01-08-2026 → 31-08-2026), September 2026 (01-09-2026 → 27-09-2026)'."""
    if not period:
        return ""
    f = ddmmyyyy or (lambda d: str(d or ""))
    parts = [f"{p['label']} ({f(p['start'])} → {f(p['end'])})" for p in period.get("periods") or []]
    head = (period.get("label") or "Period").title() if period.get("period_type") in ("last_n_calendar_months", "previous_month", "current_month_to_date", "last_n_days", "last_n_weeks", "last_n_years", "previous_year", "current_year_to_date") else None
    if not parts:
        parts = [f"{f(period.get('from'))} → {f(period.get('to'))}"]
    return (head + ": " if head else "Period: ") + ", ".join(parts)


def enforce_period(plan, question, reference_date=None, clamp_only=False):
    """Deterministic validation of LLM-chosen dates. When the question names a period, the plan's date_from/date_to
    are set from the resolver (override), or — for agent steps that may legitimately query a sub-period — clamped
    into it. Returns the (possibly changed) plan; plan["period"] carries the metadata."""
    if not isinstance(plan, dict):
        return plan
    p = resolve_date_expression(question, reference_date)
    if not p:
        return plan
    if clamp_only:
        lo, hi = p["from"], p["to"]
        if plan.get("date_from") or plan.get("date_to"):
            plan["date_from"] = max(str(plan.get("date_from") or lo)[:10], lo)
            plan["date_to"] = min(str(plan.get("date_to") or hi)[:10], hi)
        elif plan.get("date_column"):
            plan["date_from"], plan["date_to"] = lo, hi
        else:
            return plan
    else:
        plan["date_from"], plan["date_to"] = p["from"], p["to"]
    plan["period"] = period_metadata(p)
    return plan
