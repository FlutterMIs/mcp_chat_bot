"""One deterministic export representation for every channel: built from the validated result (rows, source info,
calculation), never from the LLM's wording. CSV / XLSX / PDF are three renderings of the same `ResultExport`.

    ex = ResultExport.from_final(final_response, question)      # or from_reply(reply, question)
    ex.to_csv() / ex.to_xlsx() / ex.to_pdf()                     # bytes
    ex.files()                                                    # [{"name", "bytes", "mime"}] for a channel to send

The PDF writer is dependency-free (plain PDF 1.4 with Helvetica), so exports never need an extra package."""
from __future__ import annotations

import io
import re
import zlib
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

MIME = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf"}


def _fmt_cell(v, col):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)) and str(col) != "period":
        from analyst import _fmt
        return _fmt(float(v), str(col))
    return re.sub(r"^(\d{4}-\d{2}-\d{2})[T ]00:00:00(\.0+)?$", r"\1", str(v))


@dataclass
class ResultExport:
    title: str
    question: str = ""
    table: pd.DataFrame | None = None
    value: float | None = None
    metrics: list = field(default_factory=list)          # [{"label", "value", "note"}]
    source_info: dict = field(default_factory=dict)      # source_id, sheet_name, metric, aggregation, date_column, date_range
    answer: str = ""                                     # the shown answer text — informational only, numbers come from table/value
    generated: str = field(default_factory=lambda: date.today().isoformat())

    # ------------------------------------------------------------ builders
    @classmethod
    def from_final(cls, final, question=""):
        title = (final.source_info or {}).get("metric") or "Result"
        if final.table is not None and "period" in final.table.columns:
            title = f"{title} by period"
        return cls(title=str(title), question=question, table=final.table, value=final.value, metrics=list(final.metrics or []),
                   source_info=dict(final.source_info or {}), answer=final.answer or "")

    @classmethod
    def from_reply(cls, reply, question=""):
        from response import finalize
        return cls.from_final(finalize(reply, question), question)

    # ------------------------------------------------------------ the deterministic representation
    def summary_rows(self):
        rows = [["Question", self.question], ["Generated", self.generated]]
        si = self.source_info or {}
        for k, label in (("source_id", "Source"), ("sheet_name", "Sheet"), ("metric", "Metric"), ("aggregation", "Aggregation"), ("date_column", "Date column"), ("date_range", "Date range")):
            if si.get(k):
                rows.append([label, str(si[k])])
        if self.value is not None:
            rows.append(["Value", _fmt_cell(self.value, si.get("metric") or "value")])
        for m in self.metrics:
            rows.append([str(m.get("label")), _fmt_cell(m.get("value"), str(m.get("label")))])
        return rows

    def table_text(self):
        """The table as strings (formatted numbers), or None."""
        if self.table is None or self.table.empty:
            return None
        df = self.table.copy()
        return pd.DataFrame({c: [_fmt_cell(v, c) for v in df[c]] for c in df.columns})

    # ------------------------------------------------------------ renderings
    def to_csv(self) -> bytes:
        if self.table is not None and not self.table.empty:
            return self.table.to_csv(index=False).encode("utf-8-sig")
        return pd.DataFrame(self.summary_rows(), columns=["Field", "Value"]).to_csv(index=False).encode("utf-8-sig")

    def to_xlsx(self) -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            pd.DataFrame(self.summary_rows(), columns=["Field", "Value"]).to_excel(w, index=False, sheet_name="Summary")
            (self.table if self.table is not None else pd.DataFrame()).to_excel(w, index=False, sheet_name="Data")
        return buf.getvalue()

    def to_pdf(self) -> bytes:
        lines = [("H", self.title[:90])]
        for f, v in self.summary_rows():
            lines.append(("N", f"{f}: {v}"))
        tt = self.table_text()
        if tt is not None:
            lines.append(("N", ""))
            lines.append(("B", f"Data ({len(tt)} rows)"))
            widths = [max(len(str(c)), *(len(x) for x in tt[c])) for c in tt.columns]
            widths = [min(w, 28) for w in widths]
            fmt = lambda cells: "  ".join(str(x)[:w].ljust(w) for x, w in zip(cells, widths))
            lines.append(("M", fmt(tt.columns)))
            lines.append(("M", fmt(["-" * w for w in widths])))
            for r in tt.head(400).itertuples(index=False):
                lines.append(("M", fmt(r)))
            if len(tt) > 400:
                lines.append(("N", f"... {len(tt) - 400} more rows (see CSV/Excel)"))
        return _simple_pdf(lines)

    def files(self, base="result"):
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_") or "result"
        return [{"name": f"{base}.csv", "bytes": self.to_csv(), "mime": MIME["csv"]},
                {"name": f"{base}.xlsx", "bytes": self.to_xlsx(), "mime": MIME["xlsx"]},
                {"name": f"{base}.pdf", "bytes": self.to_pdf(), "mime": MIME["pdf"]}]


# ---------------------------------------------------------------- minimal PDF writer (text only, Helvetica / Courier)
_PAGE_W, _PAGE_H, _MARGIN, _LEAD = 595, 842, 40, 13
_REPL = {"₹": "Rs.", "→": "->", "—": "-", "–": "-", "•": "-", "×": "x", "≥": ">=", "≤": "<=", "≠": "!="}


def _pdf_text(s):
    for a, b in _REPL.items():
        s = s.replace(a, b)
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _simple_pdf(lines):
    """lines: [(style, text)] with style H (title), B (bold), N (normal), M (monospace). Wraps and paginates."""
    per_page = int((_PAGE_H - 2 * _MARGIN) / _LEAD) - 1
    wrapped = []
    for style, text in lines:
        width = 110 if style == "M" else 95
        text = str(text)
        if not text:
            wrapped.append((style, ""))
            continue
        while len(text) > width:
            cut = text.rfind(" ", 0, width) if style != "M" else width
            cut = cut if cut > 20 else width
            wrapped.append((style, text[:cut]))
            text = text[cut:].lstrip() if style != "M" else text[cut:]
        wrapped.append((style, text))
    pages = [wrapped[i:i + per_page] for i in range(0, len(wrapped), per_page)] or [[]]
    objects = []          # list of bytes; object number = index + 1

    def add(obj):
        objects.append(obj)
        return len(objects)

    font_n = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_b = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    font_m = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    page_ids = []
    pages_id = len(objects) + 2 * len(pages) + 1
    for page in pages:
        y = _PAGE_H - _MARGIN
        parts = []
        for style, text in page:
            font, size = {"H": ("/FB", 14), "B": ("/FB", 10), "M": ("/FM", 8)}.get(style, ("/FN", 10))
            parts.append(f"BT {font} {size} Tf {_MARGIN} {y:.0f} Td ({_pdf_text(text)}) Tj ET")
            y -= _LEAD if style != "H" else _LEAD + 6
        stream = zlib.compress("\n".join(parts).encode("latin-1"))
        content_id = add(b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream + b"\nendstream")
        page_ids.append(add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}] /Contents {content_id} 0 R "
                            f"/Resources << /Font << /FN {font_n} 0 R /FB {font_b} 0 R /FM {font_m} 0 R >> >> >>".encode()))
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
