"""Deterministic entity filters: "jsp trader ki last 12 months ki sale" → CUSTOMER NAME = "JSP TRADERS (HISAR)".

The words of a question that are neither analysis vocabulary nor column names are tried, as contiguous spans, against
the REAL distinct values of the table's dimension columns (through the data tools, never the model). One clear match
becomes a filter; a tie is returned as a choice; nothing close → None (the planner/LLM path stays available).
Metric columns are never matched, so AMOUNT/QTY/RATE can never be "found" as an entity."""
from __future__ import annotations

import re

from entities import AmbiguousValue, match_entity
import semantics
from semantics import only_known_words, stem, tokens

MAX_DISTINCT = 5000          # columns with more values than this (free text, ids) are not entity axes
MAX_SPAN = 4


def unknown_spans(question, columns, sheet_names=()):
    """Contiguous runs of question words that are not vocabulary / column names / digits, longest first."""
    only_known_words("", columns)                         # builds KNOWN_WORDS
    from periods import resolve_date_expression
    p = resolve_date_expression(question)
    period_words = set(tokens(p["date_expression"])) if p else set()
    qstems = {stem(t) for t in tokens(question)}
    # A column word only counts when the column is named in full ("party mobile"); "mobile" alone next to a
    # Party_Mobile column is still a possible entity.
    col_words = {stem(t) for c in columns if {stem(x) for x in tokens(c["name"])} <= qstems for t in tokens(c["name"])}
    col_words |= {stem(t) for c in columns for t in tokens(c["name"]) if len(tokens(c["name"])) == 1} | {stem(t) for n in sheet_names for t in tokens(n)}
    words = re.findall(r"[A-Za-z0-9&.'()/-]+", str(question or ""))
    flags = []
    for w in words:
        t = w.lower().strip(".,()")
        known = (not t) or stem(t) in semantics.KNOWN_WORDS or stem(t) in col_words or t in period_words or (t.isdigit() and len(t) <= 2)
        flags.append((w, known))
    spans, run = [], []
    for w, known in flags + [("", True)]:
        if not known:
            run.append(w)
            continue
        if run:
            for n in range(min(len(run), MAX_SPAN), 0, -1):
                for i in range(0, len(run) - n + 1):
                    spans.append(" ".join(run[i:i + n]))
            run = []
    seen, out = set(), []
    for s in spans:
        if s.lower() not in seen and len(s.strip("()")) >= 3:
            seen.add(s.lower())
            out.append(s)
    return out


POSSESSIVE = {"ki", "ka", "ke", "wale", "wala", "wali", "ko", "se", "me", "mein", "par", "pe"}


def filter_like_spans(question, columns, sheet_names=()):
    """Unknown spans that the sentence treats as an entity: followed by ki/ka/ke/wale… ("mobile ki sale", "Acme wale").
    Period words in between are skipped ("mobile last year ki sale")."""
    spans = unknown_spans(question, columns, sheet_names)
    if not spans:
        return []
    toks = [t.strip(".,()") for t in re.sub(r"\s+", " ", str(question or "").lower()).split()]
    from periods import NUM_WORDS
    skip = {"last", "this", "previous", "pichhle", "pichle", "is", "iss", "current", "month", "months", "year", "years", "saal", "mahine", "mahina", "week", "weeks", "din", "days", "day", "mnths", "mnth"} | set(NUM_WORDS) | {str(i) for i in range(1, 40)}
    out = []
    for span in spans:
        st = [t.strip(".,()") for t in span.lower().split()]
        for i in range(len(toks) - len(st) + 1):
            if toks[i:i + len(st)] != st:
                continue
            j = i + len(st)
            while j < len(toks) and toks[j] in skip and j - (i + len(st)) < 4:      # "mobile last year ki sale": the period sits in between
                j += 1
            if j < len(toks) and toks[j] in POSSESSIVE:
                out.append(span)
                break
    return out


def find_entity_filter(question, sid, table, tools, sheet_names=()):
    """{"column", "op": "eq", "value", "typed", "score"} for the best matching span/column, None when nothing matches.
    Raises AmbiguousValue when a span fits several real values about equally (the bot asks)."""
    cols = table.get("columns", [])
    spans = unknown_spans(question, cols, sheet_names)
    if not spans:
        return None
    dims = [c for c in cols if c.get("role") == "dimension" and (c.get("distinct_count") or 0) <= MAX_DISTINCT and (c.get("distinct_count") or 0) >= 1]
    if not dims:
        return None
    values = {}
    for c in dims:
        try:
            res = tools.call("distinct_values", {"source_id": sid, "column": c["name"], "limit": MAX_DISTINCT} | ({"sheet_name": table["name"]} if table.get("name") else {}))
            values[c["name"]] = [v["value"] for v in res.get("values") or []]
        except Exception:
            continue
    from entities import _core
    best, ambiguous = None, None
    failed = []                                            # maximal spans that matched nothing: their fragments must not match loosely
    for span in spans:                                     # longest spans first: "jsp trader" before "jsp"
        fragment_of_failed = any(span.lower() != f.lower() and span.lower() in f.lower() for f in failed)
        found = []
        for col, vals in values.items():
            m = match_entity(vals, span)
            if m["status"] in ("exact", "single"):
                if fragment_of_failed and m["status"] != "exact" and not _core(m["value"]).startswith(_core(span)):
                    continue                               # "Zorblax Traders" failed → "Traders" alone must not become "Pranjal Traders"
                found.append({"column": col, "op": "eq", "value": m["value"], "typed": span, "score": m["candidates"][0]["score"]})
            elif m["status"] == "ambiguous" and ambiguous is None:
                ambiguous = AmbiguousValue(col, span, m["candidates"])
        found.sort(key=lambda f: -f["score"])
        if len(found) >= 2 and found[0]["score"] < 1.0 and found[0]["score"] - found[1]["score"] <= 0.06 and found[0]["value"] != found[1]["value"]:
            # "Pranjal" ≈ a customer AND a salesperson: the user decides which one (chips carry the column)
            raise AmbiguousValue("naam", span, [{"value": f"{f['value']} ({f['column']})", "score": f["score"]} for f in found[:4]])
        if found and (best is None or found[0]["score"] > best["score"] or (found[0]["score"] == best["score"] and len(span) > len(best["typed"]))):
            best = found[0]
        elif not found and " " in span:
            failed.append(span)
        if best and best["score"] >= 0.9:
            break
    if best:
        return best
    if ambiguous:
        raise ambiguous
    return None


def strip_span(question, span):
    """The question without the entity words, so the rest of the understanding sees only vocabulary."""
    return re.sub(r"\s+", " ", re.sub(re.escape(span), " ", question, count=1, flags=re.I)).strip()


def merge_filters(existing, new, additive=False):
    """"Code-8030 ki sale" then "jsp trader ki sale": a new entity is a new question — earlier entity filters go, unless
    the message adds to the current analysis ("… bhi", "usme se", "unme") in which case both apply (intersection)."""
    if additive:
        out = [f for f in (existing or []) if f.get("column") != new["column"]]
    else:
        out = [f for f in (existing or []) if (f.get("op") or "eq") not in ("eq", "in", "contains")]      # keep only non-entity filters (gt/lt…)
    out.append({"column": new["column"], "op": "eq", "value": new["value"]})
    return out
