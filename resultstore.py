"""Result store for the agent: every tool result stays in Python under a result_id; the LLM only sees summaries.

Also the deterministic analysis tools that work on stored results: join_results, calculate (safe expressions,
no eval), and helpers for compare_periods. No business domain is assumed anywhere.
"""
import ast
import re

import pandas as pd

SUMMARY_ROWS = 10
_norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


class ResultStore:
    def __init__(self):
        self.items = {}
        self.order = []

    # ---------- storage ----------
    def put(self, result, label, source_id=None, sheet_name=None):
        rid = f"r{len(self.order) + 1}"
        rows = result.get("rows") or []
        df = pd.DataFrame(rows)
        # "truncated" = a lookup that is missing rows: a row query cut by its limit, or groups cut by top_n.
        if result.get("aggregation") or result.get("groups_total") is not None:
            total = result.get("groups_total")
        else:
            total = result.get("rows_matched")
        truncated = isinstance(total, int) and total > len(rows)
        self.items[rid] = {"id": rid, "label": label, "result": result, "df": df, "source_id": source_id, "sheet_name": sheet_name,
                           "truncated": truncated, "rows_total": total}
        self.order.append(rid)
        return rid

    def get(self, rid):
        if rid not in self.items:
            raise ValueError(f"Unknown result_id {rid}. Known: {self.order}")
        return self.items[rid]

    def df(self, rid):
        return self.get(rid)["df"]

    def summary(self, rid, rows=SUMMARY_ROWS):
        """Compact view for the LLM: shape, columns, a few rows, and every aggregate the tool computed."""
        it = self.get(rid)
        res, df = it["result"], it["df"]
        out = {"result_id": rid, "label": it["label"], "row_count": int(len(df)), "columns": [str(c) for c in df.columns]}
        if it.get("truncated"):
            out["truncated"] = f"only {len(df)} of {it['rows_total']} rows/groups are here (a limit or top_n was applied); fine as the driving side of a left join, not as a lookup"
        for k in ("metric", "aggregation", "grand_total_all_groups", "groups_total", "rows_matched", "resolved_filters", "derived",
                  "distinct_count", "unmatched_left", "unmatched_right", "matched", "note"):
            if res.get(k) not in (None, [], {}):
                out[k] = res[k]
        if len(df):
            out["first_rows"] = df.head(rows).astype(object).where(pd.notna(df.head(rows)), None).to_dict(orient="records")
            if len(df) > rows:
                out["last_rows"] = df.tail(3).astype(object).where(pd.notna(df.tail(3)), None).to_dict(orient="records")
            for c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                if s.notna().sum() >= max(2, len(df) // 2) and c not in ("value",):
                    out.setdefault("numeric_stats", {})[str(c)] = {"sum": round(float(s.sum()), 2), "min": round(float(s.min()), 2), "max": round(float(s.max()), 2)}
        return out

    def all_numbers(self):
        """Every number that appeared in any stored result (for final-answer grounding)."""
        vals = set()

        def walk(v):
            if isinstance(v, bool) or v is None:
                return
            if isinstance(v, (int, float)):
                vals.add(round(float(v), 2))
            elif isinstance(v, str):
                from grounding import _numbers      # same parser as the answer check, so "Code-1053" ↔ 1053 agree
                vals.update(round(n, 2) for n in _numbers(v))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
        for it in self.items.values():
            walk(it["result"])
            vals.add(float(len(it["df"])))
        return vals

    # ---------- deterministic tools ----------
    def join(self, left_id, right_id, left_key, right_key, how="inner", label=None):
        """Merge two stored results on normalised keys. Never loses rows silently: unmatched counts and samples are reported."""
        if how not in ("inner", "left", "right", "outer"):
            raise ValueError("join_type must be inner, left, right or outer")
        # The lookup side must be complete, or matches go missing silently. A top-N driver is fine on the other side;
        # an inner join only needs one complete side (the truncated one then acts as the driver).
        lookups = {"inner": (), "outer": (left_id, right_id), "left": (right_id,), "right": (left_id,)}[how]
        if how == "inner" and self.get(left_id).get("truncated") and self.get(right_id).get("truncated"):
            lookups = (left_id, right_id)
        for rid_ in lookups:
            it = self.get(rid_)
            if it.get("truncated"):
                raise ValueError(f"{rid_} holds only {len(it['df'])} of {it['rows_total']} rows/groups (limit or top_n), so using it as a lookup would miss matches. "
                                 f"Re-fetch it completely (aggregate_data grouped by the key without top_n, or query_data with limit >= {it['rows_total']}), "
                                 f"or make the complete side the lookup (join_type 'left' with the small result on the left).")
        L, R = self.df(left_id).copy(), self.df(right_id).copy()
        if left_key not in L.columns or right_key not in R.columns:
            raise ValueError(f"Join keys not found. {left_id} columns: {list(L.columns)}; {right_id} columns: {list(R.columns)}")
        # A bare "value" column is renamed to its metric ("AMOUNT", "CLOSING STOCK") so the joined table reads naturally.
        for df_, rid_ in ((L, left_id), (R, right_id)):
            metric = self.items[rid_]["result"].get("metric")
            if "value" in df_.columns and metric and metric not in df_.columns:
                df_.rename(columns={"value": metric}, inplace=True)
        if right_key != left_key and right_key in L.columns:
            R.rename(columns={right_key: f"{right_key}_{right_id}"}, inplace=True); right_key = f"{right_key}_{right_id}"
        L["__k"], R["__k"] = L[left_key].map(_norm), R[right_key].map(_norm)
        lk, rk = set(L["__k"]), set(R["__k"])
        if right_key == left_key:
            R = R.rename(columns={right_key: f"{right_key}_{right_id}"}); right_key = f"{right_key}_{right_id}"
        merged = L.merge(R, on="__k", how=how, suffixes=("", f"_{right_id}"))
        # Rows that exist only on the right keep their key visible in the left key column (never a blank name).
        if right_key in merged.columns:
            merged[left_key] = merged[left_key].where(merged[left_key].notna(), merged[right_key])
            merged = merged.drop(columns=[right_key])   # the key now lives in one column; match counts say the rest
        merged = merged.drop(columns="__k")
        if "period" in merged.columns:
            merged = merged.sort_values("period", kind="stable")     # time series stay chronological after a join
        um_l, um_r = sorted(lk - rk), sorted(rk - lk)
        result = {"rows": merged.astype(object).where(pd.notna(merged), None).to_dict(orient="records"), "count": len(merged),
                  "matched": int(len(lk & rk)), "unmatched_left": len(um_l), "unmatched_right": len(um_r),
                  "unmatched_left_sample": [L.loc[L["__k"] == k, left_key].iloc[0] for k in um_l[:5]],
                  "unmatched_right_sample": [R.loc[R["__k"] == k, right_key].iloc[0] for k in um_r[:5]], "source": "join"}
        return self.put(result, label or f"join {left_id}.{left_key} × {right_id}.{right_key} ({how})")

    def calculate(self, rid, expression, new_column=None, label=None):
        """Add a computed column (or a scalar) using a safe expression: + - * / ( ), numbers, column names,
        and pct_change(a, b), share(col), rank(col), running_total(col), abs(x), round(x, n), diff(a, b), ratio(a, b)."""
        df = self.df(rid).copy()
        value = safe_eval(expression, df)
        if isinstance(value, pd.Series):
            name = new_column or expression
            df[name] = value if value.dtype == bool else value.round(4)
            result = {"rows": df.astype(object).where(pd.notna(df), None).to_dict(orient="records"), "count": len(df),
                      "note": f"{name} = {expression}", "source": "calculate"}
            return self.put(result, label or f"{rid} + {name}")
        result = {"rows": [{"value": round(float(value), 4)}], "count": 1, "note": f"{expression} over {rid}", "source": "calculate"}
        return self.put(result, label or f"calc {expression}")

    def sort_limit(self, rid, by, direction="desc", limit=None, label=None):
        df = self.df(rid)
        if by not in df.columns:
            raise ValueError(f"Column {by} not in {rid}: {list(df.columns)}")
        key = pd.to_numeric(df[by], errors="coerce")
        out = df.assign(__k=key if key.notna().any() else df[by].astype(str)).sort_values("__k", ascending=direction == "asc").drop(columns="__k")
        if limit:
            out = out.head(int(limit))
        result = {"rows": out.astype(object).where(pd.notna(out), None).to_dict(orient="records"), "count": len(out), "source": "sort"}
        return self.put(result, label or f"{rid} sorted by {by} {direction}" + (f" top {limit}" if limit else ""))


# ---------- safe expression evaluation (no eval) ----------
_FUNCS = {
    "pct_change": lambda cur, prev: (cur - prev) / prev.replace(0, float("nan")) * 100 if isinstance(prev, pd.Series) else (cur - prev) / prev * 100,
    "diff": lambda a, b: a - b,
    "ratio": lambda a, b: a / (b.replace(0, float("nan")) if isinstance(b, pd.Series) else b),
    "share": lambda col: col / col.sum() * 100,
    "rank": lambda col, desc=True: col.rank(ascending=not desc, method="min"),
    "running_total": lambda col: col.cumsum(),
    "abs": lambda x: x.abs() if isinstance(x, pd.Series) else abs(x),
    "round": lambda x, n=2: x.round(n) if isinstance(x, pd.Series) else round(x, n),
    "sum": lambda col: float(col.sum()),
    "avg": lambda col: float(col.mean()),
    "min": lambda col: float(col.min()),
    "max": lambda col: float(col.max()),
    "count": lambda col: int(col.notna().sum()),
    "count_true": lambda col: int(col.astype(bool).sum()),
}


def _column(df, name):
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    norm = {re.sub(r"\W+", "_", str(c)).lower(): c for c in df.columns}
    key = re.sub(r"\W+", "_", name).lower()
    if key in norm:
        return pd.to_numeric(df[norm[key]], errors="coerce")
    raise ValueError(f'Unknown column "{name}". Columns: {list(df.columns)}')


def safe_eval(expression, df):
    # Column names with spaces/punctuation ("CLOSING STOCK", "Order No") are replaced by safe identifiers first, longest name first.
    aliases = {}
    expr = expression
    for c in sorted(map(str, df.columns), key=len, reverse=True):
        if re.search(r"[^A-Za-z0-9_]", c) or c[:1].isdigit():
            ident = f"_col{len(aliases)}"
            aliases[ident] = c
            expr = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(c) + r"(?![A-Za-z0-9_])", ident, expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Could not parse expression {expression!r}: {e.msg}. Use column names from the result and + - * / or the allowed functions.") from e
    df = df.rename(columns={v: k for k, v in aliases.items()}) if aliases else df

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _column(df, node.value)
        if isinstance(node, ast.Name):
            return _column(df, node.id)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise ValueError(f"Function not allowed: {ast.dump(node.func)[:40]}. Allowed: {sorted(_FUNCS)}")
            if node.func.id == "col" or node.keywords and any(k.arg not in ("desc", "n") for k in node.keywords):
                raise ValueError("Unsupported keyword")
            args = [ev(a) for a in node.args]
            kwargs = {k.arg: ev(k.value) for k in node.keywords}
            return _FUNCS[node.func.id](*args, **kwargs)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / (b.replace(0, float("nan")) if isinstance(b, pd.Series) else b)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)):
            a, b = ev(node.left), ev(node.comparators[0])
            op = node.ops[0]
            return (a > b) if isinstance(op, ast.Gt) else (a >= b) if isinstance(op, ast.GtE) else (a < b) if isinstance(op, ast.Lt) \
                else (a <= b) if isinstance(op, ast.LtE) else (a == b) if isinstance(op, ast.Eq) else (a != b)
        raise ValueError(f"Expression element not allowed: {type(node).__name__}")
    return ev(tree)
