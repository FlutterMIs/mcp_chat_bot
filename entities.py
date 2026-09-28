"""Safe fuzzy matching of user-typed entity values ("Pranjli ji") onto real column values ("Pranjali Ji").

    exact / normalised match      → that value                       (status "exact")
    one clearly closest value     → that value, with a note          (status "single")
    several close values          → the candidates, nothing chosen   (status "ambiguous"; the bot asks)
    nothing close                 → no candidate                     (status "none")

Generic: works on any text column. It is only ever applied to filter VALUES of dimension columns — never to metric
columns (AMOUNT / QTY / RATE are chosen by schema validation, not by string similarity)."""
from __future__ import annotations

import difflib
import re

SINGLE_CUTOFF = 0.82        # similarity a lone candidate needs ("dilli" must not become "DILIP")
AMBIGUOUS_GAP = 0.06        # two candidates this close in score are a real tie → ask
MAX_CANDIDATES = 4
HONORIFICS = {"ji", "sahab", "saheb", "sir", "madam", "mr", "mrs", "ms", "shri", "smt", "bhai", "bhaiya"}


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _core(s):
    """Normalised value without honorifics/spaces: 'pranjali ji' → 'pranjali' (so 'Pranjli ji' vs 'Pranjali Ji' compares the names)."""
    toks = [t for t in norm(s).split() if t not in HONORIFICS]
    return "".join(toks) or norm(s).replace(" ", "")


def _score(want, cand):
    a, b = _core(want), _core(cand)
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:      # "ravi" typed for "ravikumar": a whole-word prefix/part is a strong match, longer part = stronger
        r = max(r, 0.85 + 0.15 * min(len(a), len(b)) / max(len(a), len(b)))
    return r


def match_entity(values, want, cutoff=SINGLE_CUTOFF):
    """{"status": exact|single|ambiguous|none, "value": str|None, "candidates": [{"value", "score"}]}"""
    want = str(want or "").strip()
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not want or not vals:
        return {"status": "none", "value": None, "candidates": []}
    for v in vals:
        if v == want:
            return {"status": "exact", "value": v, "candidates": [{"value": v, "score": 1.0}]}
    by_norm = {norm(v): v for v in vals}
    if norm(want) in by_norm:
        return {"status": "exact", "value": by_norm[norm(want)], "candidates": [{"value": by_norm[norm(want)], "score": 1.0}]}
    if len(_core(want)) < 3:
        return {"status": "none", "value": None, "candidates": []}
    scored = sorted(((round(_score(want, v), 3), v) for v in dict.fromkeys(vals)), key=lambda x: (-x[0], x[1]))
    close = [(s, v) for s, v in scored if s >= cutoff][:MAX_CANDIDATES]
    if not close:
        return {"status": "none", "value": None, "candidates": []}
    cands = [{"value": v, "score": s} for s, v in close]
    if len(close) == 1 or close[0][0] - close[1][0] > AMBIGUOUS_GAP or (close[0][0] >= 0.97 and close[1][0] < 0.9):
        return {"status": "single", "value": close[0][1], "candidates": cands}
    return {"status": "ambiguous", "value": None, "candidates": cands}


class AmbiguousValue(ValueError):
    """Several real values fit the typed one; the user must choose. `column`, `typed`, `candidates` feed the chips."""

    def __init__(self, column, typed, candidates):
        self.column, self.typed, self.candidates = column, typed, [c["value"] for c in candidates]
        super().__init__(f'"{typed}" in column "{column}" matches several values: {", ".join(self.candidates)}. Ask the user which one.')
