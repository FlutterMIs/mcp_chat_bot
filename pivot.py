"""Pivot / cross-tab presentation of a two-key aggregate ("month wise sales by city", "sales person vs month").

The numbers come from the long result the tools produced (period/dimension rows + value); this only re-arranges them:
rows = first key, columns = the second key's real values, cells = the aggregated value, plus a Total row/column that is
the plain sum of the cells. Periods stay chronological (the long result is already sorted; the pivot keeps that order).
A pivot is only used when it reads better than the long table: 2 keys, 1 measure, few enough columns."""
from __future__ import annotations

import re

import pandas as pd

MAX_COLUMNS = 24          # more column values than this → keep the long table (a 60-column matrix is unreadable)
PIVOT_CUE = re.compile(r"\b(vs\.?|versus|cross\s*-?\s*tab|crosstab|pivot|matrix|by)\b", re.I)


def should_pivot(keys, measures, df, question=""):
    """Two grouping keys and one measure → pivot; unless the columns would explode."""
    if len(keys or []) != 2 or len(measures or []) != 1 or df is None or df.empty:
        return False
    rows_key, cols_key = pivot_axes(keys)
    return df[cols_key].nunique() <= MAX_COLUMNS and df[rows_key].nunique() >= 1


def pivot_axes(keys):
    """(row_key, column_key): the period (time) goes down the rows so months read top-to-bottom; else keep the order asked."""
    if "period" in keys:
        return "period", next(k for k in keys if k != "period")
    return keys[0], keys[1]


def pivot_table(df, keys, value_col, totals=True):
    """Long → wide. Returns a DataFrame with the row key first, one column per second-key value, and Totals (sum only)."""
    rows_key, cols_key = pivot_axes(keys)
    work = df[[rows_key, cols_key, value_col]].copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce").fillna(0)
    row_order = list(dict.fromkeys(work[rows_key].tolist()))            # keep the long table's (chronological) order
    col_order = list(dict.fromkeys(work[cols_key].tolist()))
    if cols_key != "period":
        col_order = sorted(col_order, key=lambda v: (-float(work.loc[work[cols_key] == v, value_col].sum()), str(v)))   # biggest column first
    wide = work.pivot_table(index=rows_key, columns=cols_key, values=value_col, aggfunc="sum", fill_value=0)
    wide = wide.reindex(index=row_order, columns=col_order).fillna(0)
    wide.columns = [str(c) for c in wide.columns]
    if totals:
        wide["Total"] = wide.sum(axis=1)
        total_row = wide.sum(axis=0).to_frame().T
        total_row.index = ["Total"]
        wide = pd.concat([wide, total_row])
    out = wide.reset_index().rename(columns={"index": rows_key, rows_key: rows_key})
    if out.columns[0] != rows_key:
        out = out.rename(columns={out.columns[0]: rows_key})
    for c in out.columns[1:]:
        if (out[c] % 1 == 0).all():
            out[c] = out[c].astype("int64")
    return out


def describe(keys, measure_label, n_rows, n_cols):
    rows_key, cols_key = pivot_axes(keys)
    return f"{rows_key.title() if rows_key == 'period' else rows_key} × {cols_key}: {measure_label} ({n_rows} rows × {n_cols} columns, Total row/column = sum of the cells)."
