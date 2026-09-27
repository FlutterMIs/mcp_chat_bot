"""Keeps answers inside the user's data.

1. `result_definition()` — the authoritative description of what a tool result contains (columns, filters, date range,
   grouping, calculations). Technical metadata (matching row counts) is kept apart from business figures.
2. `unsupported_numbers()` — every number in an answer must be a business value of the result (row cells, derived
   figures, grand totals, distinct counts), a number from the question/plan, or a rank. Matching-row counts and
   other metadata never qualify.
3. `mislabeled_numbers()` — a number stated next to a row label must be that row's value (number + label + source).
4. `unsupported_claims()` — "I added / included / calculated X" is only allowed when X exists in the definition.
"""
import re

from semantics import MONEY_WORDS, QUANTITY_WORDS, stem, tokens

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
UNITS = {"k": 1e3, "thousand": 1e3, "hazaar": 1e3, "hazar": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "lk": 1e5,
         "crore": 1e7, "crores": 1e7, "cr": 1e7, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
         "लाख": 1e5, "करोड़": 1e7, "हज़ार": 1e3, "हजार": 1e3}
NUM_UNIT = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(" + "|".join(sorted(map(re.escape, UNITS), key=len, reverse=True)) + r")(?![\w])", re.I)
META_KEYS = {"rows_matched", "count", "rows_total", "rows_omitted", "groups_total", "matched", "unmatched_left", "unmatched_right", "rows_in_result",
             "matching_rows_technical_metadata", "limit"}
COUNT_AGGS = {"count", "count_distinct"}


def _numbers(text):
    """Numbers in a text. '59.96 lakh' counts as 5,996,000 (the unit is part of the number, not a separate figure)."""
    text = str(text)
    out = []
    for m in NUM_UNIT.finditer(text):
        try:
            out.append(float(m.group(1).replace(",", "")) * UNITS[m.group(2).lower()])
        except (ValueError, KeyError):
            pass
    rest = NUM_UNIT.sub(" ", text)
    for tok in NUM.findall(rest):
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def _close(a, b, tol=0.006):
    return a == b or (a and abs(a - b) / abs(a) < tol) or abs(a - b) < 0.006


# ---------------------------------------------------------------- result definition
def result_definition(result, plan=None, question=None):
    """What this result really is. Everything the wording model may describe must be here."""
    plan = plan or {}
    rows = result.get("rows") or []
    agg = (result.get("aggregation") or plan.get("aggregation") or ("sum" if plan.get("operation") == "aggregate" else None))
    metric = result.get("metric") or plan.get("metric")
    group_by = list(plan.get("group_by") or [])
    cols = []
    if rows:
        for c in rows[0]:
            v = rows[0].get(c)
            if c == "period":
                cols.append({"name": "period", "type": "period", "grain": plan.get("date_grain")})
            elif c == "value":
                cols.append({"name": metric or "value", "type": "count" if (agg in COUNT_AGGS) else "metric", "aggregation": agg, "source_column": metric})
            elif c in group_by or c == result.get("column"):
                cols.append({"name": c, "type": "dimension"})
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                cols.append({"name": c, "type": "metric"})
            else:
                cols.append({"name": c, "type": "dimension"})
    calcs = []
    if result.get("note"):
        calcs.append(result["note"])
    for k in ("grand_total_all_groups", "distinct_count"):
        if result.get(k) is not None:
            calcs.append(f"{k} = {result[k]}")
    if result.get("derived"):
        calcs.append("derived: " + ", ".join(result["derived"].keys()))
    date_range = None
    if plan.get("date_column") or plan.get("date_from") or plan.get("date_to"):
        date_range = {"column": plan.get("date_column"), "from": plan.get("date_from"), "to": plan.get("date_to"), "grain": plan.get("date_grain")}
    return {
        "source": plan.get("source_id") or result.get("source_id"),
        "dataset": plan.get("sheet_name") or result.get("sheet_name"),
        "operation": plan.get("operation") or result.get("source"),
        "metric": metric, "aggregation": agg, "group_by": group_by,
        "filters": list(plan.get("filters") or []), "date_range": date_range,
        "sort": plan.get("sort") or plan.get("sort_by"), "top_n": plan.get("top_n"),
        "columns": cols, "calculations": calcs,
        "rows_in_result": len(rows),
        "matching_rows_technical_metadata": result.get("rows_matched"),
        "resolved_filters": result.get("resolved_filters") or [],
    }


# ---------------------------------------------------------------- numbers
def business_values(result):
    """Numbers that are real figures of the result: row cells, derived figures, grand totals, distinct counts."""
    vals = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            vals.add(round(float(v), 2))
        elif isinstance(v, str):
            vals.update(round(n, 2) for n in _numbers(v))
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in META_KEYS:
                    add(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x)
    for r in result.get("rows") or []:
        add(r)
    add(result.get("derived"))
    for k in ("grand_total_all_groups", "distinct_count"):
        add(result.get(k))
    for r in result.get("values") or []:      # distinct_values tool output
        add(r)
    for k in ("text", "note"):                 # document/website text and calculation notes
        add(result.get(k))
    return vals


def unsupported_numbers(answer, result, question="", plan=None):
    """Numbers in the answer that are not business values of the result (nor in the question/plan, nor ranks).
    A number that merely appears in technical metadata (matching rows, limits) does not count."""
    allowed = business_values(result or {})
    allowed.update(round(n, 2) for n in _numbers(question))
    if plan:
        for k in ("top_n", "date_from", "date_to", "limit"):
            if plan.get(k) is not None:
                allowed.update(round(n, 2) for n in _numbers(str(plan[k])))
    n_rows = len((result or {}).get("rows") or [])
    bad = []
    for n in _numbers(answer):
        if any(_close(a, n) for a in allowed) or (n == int(n) and 0 <= n <= max(n_rows, 1)):
            continue
        bad.append(n)
    return bad


def _row_label(row):
    return " ".join(str(v) for k, v in row.items() if k != "value" and not isinstance(v, (int, float)))


def mislabeled_numbers(answer, result):
    """(number, label) pairs where the sentence names one row but the number is another row's value."""
    rows = [r for r in (result or {}).get("rows") or [] if "value" in r and isinstance(r.get("value"), (int, float))]
    if len(rows) < 2:
        return []
    labels = {}
    for r in rows:
        lab = _row_label(r).strip()
        if len(lab) >= 3:
            labels[lab.lower()] = round(float(r["value"]), 2)
    values = set(labels.values())
    bad = []
    for sentence in re.split(r"(?<=[.!?।\n])\s+|\n", str(answer)):
        s = sentence.lower()
        found = [lab for lab in labels if lab in s]
        if len(found) != 1:
            continue
        own = labels[found[0]]
        for n in _numbers(sentence):
            n = round(n, 2)
            if any(_close(v, n) for v in values) and not _close(own, n):
                bad.append((n, found[0]))
    return bad


# ---------------------------------------------------------------- claims
INCLUSION = re.compile(r"\b(add(?:ed)?|include[ds]?|included|shamil|jod\w*|calculate[ds]?|computed|nikal\w*|diya gaya|kiya gaya|kiye gaye|di gayi|"
                       r"provided|given|shown|dikha\w*|columns?|along with|ke saath|saath hi|bhi)\b", re.I)
COUNT_WORDS = {"count", "counts", "number", "sankhya", "ginti", "distinct", "unique", "kitne", "kitni"}
STOP_CLAIM = {"total", "value", "data", "month", "months", "wise", "period", "hisaab", "hisab", "mein", "me", "ka", "ki", "ke", "har", "each"}


def _claim_terms(clause, schema_dim_tokens):
    """Measures a clause talks about: ('kind', 'monetary'|'quantity') or ('count', entity_stem)."""
    toks = [stem(t) for t in tokens(clause)]
    money_s, qty_s = {stem(w) for w in MONEY_WORDS}, {stem(w) for w in QUANTITY_WORDS}
    terms = set()
    for i, t in enumerate(toks):
        if t in money_s:
            terms.add(("kind", "monetary"))
        elif t in qty_s:
            terms.add(("kind", "quantity"))
        elif t in COUNT_WORDS:
            neighbours = [toks[j] for j in (i - 2, i - 1, i + 1, i + 2) if 0 <= j < len(toks)]
            ent = next((n for n in neighbours if n in schema_dim_tokens and n not in STOP_CLAIM), None)
            terms.add(("count", ent))
    return terms


def unsupported_claims(answer, definition, schema_columns=None):
    """Claims of inclusion ("items count add kiya", "distinct customers bhi include hain") that the result does not back."""
    cols = definition.get("columns") or []
    col_tokens = [(c, {stem(t) for t in tokens(c["name"])} | {stem(t) for t in tokens(c.get("source_column") or "")}) for c in cols]
    schema_dim_tokens = {stem(t) for c in (schema_columns or []) for t in tokens(c.get("name", ""))} - STOP_CLAIM
    money_s, qty_s = {stem(w) for w in MONEY_WORDS}, {stem(w) for w in QUANTITY_WORDS}
    unmet = []
    for clause in re.split(r"[.!?।\n;]|\bjisme\b|\bjismein\b|\bwhich\b", str(answer)):
        if not INCLUSION.search(clause):
            continue
        for kind, arg in _claim_terms(clause, schema_dim_tokens):
            if kind == "kind":
                ok = any(((money_s if arg == "monetary" else qty_s) & tk) for _, tk in col_tokens)
                label = "amount/money column" if arg == "monetary" else "quantity column"
            else:
                ok = any(c["type"] == "count" and (arg is None or arg in tk) for c, tk in col_tokens) or \
                     bool(arg) and any(arg in tk and c["type"] in ("count", "metric") for c, tk in col_tokens)
                label = f"count of {arg}" if arg else "a count column"
            if not ok and label not in unmet:
                unmet.append(label)
    return unmet


# ---------------------------------------------------------------- deterministic helpers
def enrich_result(result, plan=None):
    """Add derived figures computed here, never by the LLM: total, share %, and change between rows."""
    plan = plan or {}
    rows = result.get("rows") or []
    vals = [r.get("value") for r in rows]
    if len(rows) < 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
        return result
    label = lambda r: " / ".join(str(v) for k, v in r.items() if k != "value")
    total = float(sum(vals))
    derived = {("total_of_shown_rows_only" if result.get("groups_total", len(rows)) > len(rows) else "total_of_rows"): round(total, 2),
               "highest": label(rows[vals.index(max(vals))]), "lowest": label(rows[vals.index(min(vals))])}
    if total:
        derived["share_pct"] = [{"label": label(r), "pct": round(v / total * 100, 2)} for r, v in zip(rows, vals)]
    if plan.get("date_grain") or len(rows) == 2:
        derived["change_vs_previous_row"] = [
            {"from": label(a), "to": label(b), "change": round(vb - va, 2), "change_pct": round((vb - va) / va * 100, 2) if va else None}
            for a, b, va, vb in zip(rows, rows[1:], vals, vals[1:])]
    return result | {"derived": derived}


def plain_answer(result, question=""):
    """Deterministic fallback when the LLM answer can't be verified."""
    rows = result.get("rows") or []
    if not rows:
        return "Is sawal ke liye data mein koi matching row nahi mili."
    if len(rows) == 1 and set(rows[0]) <= {"value"}:
        v = rows[0]["value"]
        return f"{result.get('metric', 'Value')} ({result.get('aggregation', 'sum')}): **{v:,}**" if isinstance(v, (int, float)) else f"Result: **{v}**"
    total = (result.get("derived") or {}).get("total_of_rows")
    if total is not None and not any(k for r in rows for k in r if k not in ("value", "period")):
        return f"Kul: **{total:,.2f}** ({len(rows)} periods ka jod). Breakdown neeche hai."
    head = f"{len(rows)} rows mile" + (f" ({result.get('metric')}, {result.get('aggregation')})" if result.get("metric") else "") + ". Poori table neeche hai."
    return head
