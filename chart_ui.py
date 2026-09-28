"""Chart for any result table, with a ⋮ menu to pick chart type and columns.

Pure pandas + Altair: no LLM call, so the chart always matches the table.
"""
import altair as alt
import pandas as pd
import streamlit as st

CHART_TYPES = {
    "bar": "Bar",
    "barh": "Horizontal bar",
    "line": "Line",
    "area": "Area",
    "pie": "Pie",
    "donut": "Donut",
    "scatter": "Scatter",
    "none": "Table only",
}
PIE_MAX_SLICES = 10


def _numeric_cols(df):
    out = []
    for c in df.columns:
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().sum() and s.notna().mean() >= 0.8:
            out.append(c)
    return out


def _is_datelike(s):
    if pd.api.types.is_datetime64_any_dtype(s):
        return True
    if pd.api.types.is_numeric_dtype(s):
        return False
    parsed = pd.to_datetime(s.astype(str), errors="coerce", format="mixed")
    return parsed.notna().mean() >= 0.8


VALUE_WORDS = ("amount", "sales", "revenue", "total", "value", "price", "net")


def _pick_value(nums, metric=None):
    if "value" in nums:
        return "value"
    lower = {str(c).lower(): c for c in nums}
    if metric and str(metric).lower() in lower:
        return lower[str(metric).lower()]
    for word in VALUE_WORDS:
        for name, c in lower.items():
            if word in name:
                return c
    not_ids = [c for c in nums if str(c).lower() not in ("id", "sr", "sno", "s.no", "index") and not str(c).lower().endswith("_id")]
    return (not_ids or nums)[0]


def chartable(df):
    """A chart needs at least 2 rows and one numeric column; a text comparison table gets no chart menu."""
    return df is not None and len(df) >= 2 and bool(_numeric_cols(df))


def default_chart(df, hint=None, metric=None):
    """Best-guess {type, x, y} for a table, or None when nothing is plottable."""
    if df is None or df.empty or len(df.columns) < 2:
        return None
    if hint and hint.get("x") in df.columns and hint.get("y") in df.columns and df[hint["x"]].nunique() >= 2:
        out = {"type": hint.get("type", "bar"), "x": hint["x"], "y": hint["y"]}
        if hint.get("color") in df.columns and hint["color"] not in (hint["x"], hint["y"]):
            out["color"] = hint["color"]                     # e.g. month on X, amount on Y, the winning salesperson as colour
        return out
    asked = (hint or {}).get("type") if (hint or {}).get("type") in CHART_TYPES else None
    nums = _numeric_cols(df)
    if not nums:
        return None
    y = _pick_value(nums, metric)
    labels = [c for c in df.columns if c not in nums]
    if "period" in df.columns:
        labels.insert(0, "period")
    varied = [c for c in labels if df[c].nunique() >= 2]
    dates = [c for c in varied if _is_datelike(df[c])]
    if dates:
        return {"type": asked or "line", "x": dates[0], "y": y}
    cats = [c for c in varied if df[c].nunique() <= 50]
    x = (cats or varied or labels or [c for c in df.columns if c != y])[0]
    return {"type": asked or "bar", "x": x, "y": y}


def _prepare(df, x, y, kind, color=None):
    cols = [x, y] if x != y else [x]
    if color and color in df.columns and color not in cols:
        cols.append(color)
    c = df[cols].copy()
    c[y] = pd.to_numeric(c[y], errors="coerce")
    c = c.dropna(subset=[y])
    if kind == "scatter":
        return c
    # Detail rows repeat the same label: sum them so each label gets one bar/slice/point (per colour series when given).
    keys = [x] + ([color] if color and color in c.columns else [])
    if c.duplicated(subset=keys).any():
        c = c.groupby(keys, as_index=False, sort=False)[y].sum()
    if kind in ("pie", "donut") and len(c) > PIE_MAX_SLICES:
        c = c.sort_values(y, ascending=False)
        top, rest = c.iloc[: PIE_MAX_SLICES - 1], c.iloc[PIE_MAX_SLICES - 1 :]
        c = pd.concat([top, pd.DataFrame({x: ["Other"], y: [rest[y].sum()]})], ignore_index=True)
    return c


def _build(c, x, y, kind, color=None):
    # Full dates (2026-01-05) get a time axis; periods like "2026-01" or "2026" stay ordered labels,
    # otherwise Vega places them at midnight UTC and months drift across the axis.
    full_dates = pd.api.types.is_datetime64_any_dtype(c[x]) or (c[x].astype(str).str.len().min() >= 10 and _is_datelike(c[x]))
    xtype = "T" if kind in ("line", "area") and full_dates else ("Q" if kind == "scatter" and pd.api.types.is_numeric_dtype(c[x]) else ("O" if kind in ("line", "area") else "N"))
    if xtype == "T":
        c = c.assign(**{x: pd.to_datetime(c[x].astype(str), errors="coerce", format="mixed")}).sort_values(x)
    tip = [alt.Tooltip(f"{x}:{xtype}", title=str(x)), alt.Tooltip(f"{y}:Q", title=str(y), format=",.2f")]
    base = alt.Chart(c)
    if kind in ("pie", "donut"):
        return base.mark_arc(innerRadius=60 if kind == "donut" else 0).encode(
            theta=alt.Theta(f"{y}:Q"),
            color=alt.Color(f"{x}:N", title=str(x), sort=alt.SortField(y, order="descending")),
            order=alt.Order(f"{y}:Q", sort="descending"),
            tooltip=tip,
        )
    if kind == "barh":
        return base.mark_bar().encode(y=alt.Y(f"{x}:N", sort="-x", title=str(x)), x=alt.X(f"{y}:Q", title=str(y)), tooltip=tip)
    mark = {"bar": base.mark_bar(), "line": base.mark_line(point=True), "area": base.mark_area(opacity=0.7), "scatter": base.mark_circle(size=70)}[kind]
    angle = -40 if xtype == "N" and kind == "bar" else (0 if xtype == "O" else None)
    xenc = alt.X(f"{x}:{xtype}", title=str(x), sort=None, axis=alt.Axis(labelAngle=angle) if angle is not None else alt.Undefined)
    if color and color in c.columns and color not in (x, y):
        tip.append(alt.Tooltip(f"{color}:N", title=str(color)))
        return mark.encode(x=xenc, y=alt.Y(f"{y}:Q", title=str(y)), color=alt.Color(f"{color}:N", title=str(color)), tooltip=tip)
    return mark.encode(x=xenc, y=alt.Y(f"{y}:Q", title=str(y)), tooltip=tip)


def chart_menu(df, msg_id, hint=None, metric=None, auto=True):
    """Render the ⋮ popover. Call inside the row that holds the message's action buttons.
    auto=False keeps the chart off until the user picks a type (the response policy decided it adds nothing)."""
    best = default_chart(df, hint, metric)
    auto = best if auto else None
    cols = list(df.columns)
    nums = _numeric_cols(df) or cols
    tkey, xkey, ykey = f"chart_type_{msg_id}", f"chart_x_{msg_id}", f"chart_y_{msg_id}"
    st.session_state.setdefault(tkey, auto["type"] if auto else "none")
    with st.popover("Chart", icon=":material/more_vert:", type="tertiary", help="Choose chart type and columns"):
        st.selectbox("Chart type", list(CHART_TYPES), format_func=CHART_TYPES.get, key=tkey)
        x_default = best["x"] if best else cols[0]
        y_default = best["y"] if best else nums[-1]
        st.selectbox("Label / X axis", cols, index=cols.index(x_default), key=xkey)
        st.selectbox("Value / Y axis", nums, index=nums.index(y_default) if y_default in nums else 0, key=ykey)


def draw_selected_chart(df, msg_id):
    kind = st.session_state.get(f"chart_type_{msg_id}", "none")
    x, y = st.session_state.get(f"chart_x_{msg_id}"), st.session_state.get(f"chart_y_{msg_id}")
    if kind == "none" or x not in df.columns or y not in df.columns:
        return
    if x == y and kind != "scatter":
        st.caption("Label aur value ke liye alag columns choose karo.")
        return
    color = st.session_state.get(f"chart_color_{msg_id}")
    c = _prepare(df, x, y, kind, color)
    if c.empty:
        st.caption(f"'{y}' column mein chart ke liye numbers nahi mile.")
        return
    st.altair_chart(_build(c, x, y, kind, color), key=f"chart_{msg_id}")


def render_png(df, hint=None, metric=None, title=None, kind=None):
    """PNG of the same Altair chart the web UI draws, for channels without a browser (WhatsApp).
    Returns None when the table has nothing chartable, so callers fall back to text."""
    import vl_convert as vlc
    if metric and "value" in df.columns and metric not in df.columns:
        df = df.rename(columns={"value": metric})
        hint = hint and {**hint, **({"y": metric} if hint.get("y") == "value" else {})}
    auto = default_chart(df, hint, metric)
    if not auto:
        return None
    kind = kind or auto["type"]
    x, y = auto["x"], auto["y"]
    if x == y:
        return None
    color = auto.get("color")
    c = _prepare(df, x, y, kind, color)
    if c.empty:
        return None
    # An image can't be hovered, so print the numbers on the chart itself.
    chart = _build(c, x, y, kind, color) + _value_labels(c, x, y, kind)
    chart = (chart
             .properties(width=640, height=360, title=title or "")
             .configure(background="white")
             .configure_view(stroke=None)
             .configure_axis(labelFontSize=12, titleFontSize=13, grid=True, gridColor="#e6e6e6")
             .configure_title(fontSize=16, anchor="start")
             .configure_mark(color="#2563eb")
             .configure_text(color="#1f2937", fontSize=12))
    return vlc.vegalite_to_png(chart.to_json(), scale=2)


def _value_labels(c, x, y, kind):
    """Text layer for PNG charts: % on pie/donut slices, values at bar ends / line points."""
    if kind in ("pie", "donut"):
        c = c.assign(__pct__=(c[y] / c[y].sum() * 100).round(1).astype(str) + "%")
        return alt.Chart(c).mark_text(radius=150 if kind == "pie" else 125, fontWeight="bold", color="white").encode(
            theta=alt.Theta(f"{y}:Q", stack=True), text="__pct__:N",
            order=alt.Order(f"{y}:Q", sort="descending"))
    if kind == "barh":
        return alt.Chart(c).mark_text(align="left", dx=4).encode(y=alt.Y(f"{x}:N", sort="-x"), x=f"{y}:Q", text=alt.Text(f"{y}:Q", format=",.0f"))
    if kind in ("bar", "line", "area"):
        xtype = "N" if kind == "bar" else ("O" if not pd.api.types.is_datetime64_any_dtype(c[x]) else "T")
        return alt.Chart(c).mark_text(dy=-8 if kind == "bar" else -14).encode(x=alt.X(f"{x}:{xtype}", sort=None), y=f"{y}:Q", text=alt.Text(f"{y}:Q", format=",.0f"))
    return alt.Chart(c).mark_text().encode()
