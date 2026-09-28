"""Text → chunks with metadata. Page numbers come from the loader (PDF pages); sections from heading-like lines.

    chunk_document(parsed, max_chars) -> [{"chunk_id", "text", "page", "section", "start"}]

Chunks follow paragraph boundaries, are capped at `max_chars`, and overlap by one trailing paragraph so a sentence
split across chunks is still findable. Nothing is summarised or rewritten: chunk text is the source text."""
from __future__ import annotations

import re

HEADING = re.compile(r"^(?:#{1,6}\s+)?([A-Z0-9][^.!?]{2,70})$")     # short line, no sentence punctuation, starts with a capital/digit


def _paragraphs(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:[-•*]|\d+[.)])\s)", str(text or ""))]
    out = []
    for p in parts:
        if not p:
            continue
        # very long paragraphs (PDF text without blank lines) are split on sentence ends
        if len(p) > 1200:
            buf = ""
            for s in re.split(r"(?<=[.!?।])\s+", p):
                if len(buf) + len(s) > 1000 and buf:
                    out.append(buf.strip())
                    buf = ""
                buf += s + " "
            if buf.strip():
                out.append(buf.strip())
        else:
            out.append(p)
    return out


def _is_heading(line):
    line = line.strip()
    return bool(line) and len(line) <= 80 and "\n" not in line and bool(HEADING.match(line)) and not line.endswith(",")


def chunk_pages(pages, max_chars=900, source_name=""):
    """pages: [{"page": int|None, "text": str}] (a plain document = one page with page=None)."""
    chunks, section = [], None
    n = 0
    for pg in pages:
        page_no, text = pg.get("page"), pg.get("text") or ""
        buf, buf_start, carry = [], 0, None
        paras = _paragraphs(text)
        pos = 0
        for p in paras:
            first_line = p.splitlines()[0] if p else ""
            if _is_heading(first_line) and len(p) <= 120:
                section = first_line.strip("# ").strip()
            size = sum(len(x) + 1 for x in buf)
            if buf and size + len(p) > max_chars:
                chunks.append({"chunk_id": n, "text": "\n".join(buf).strip(), "page": page_no, "section": section, "start": buf_start, "source": source_name})
                n += 1
                carry = buf[-1] if len(buf[-1]) < max_chars // 3 else None      # small overlap: the last paragraph continues
                buf, buf_start = ([carry] if carry else []), pos
            buf.append(p)
            pos += len(p) + 2
        if buf and "\n".join(buf).strip():
            chunks.append({"chunk_id": n, "text": "\n".join(buf).strip(), "page": page_no, "section": section, "start": buf_start, "source": source_name})
            n += 1
    return [c for c in chunks if len(c["text"]) >= 20]


def chunk_document(parsed, max_chars=900):
    """A parsed source (source_loader / MCPServer.sources entry) → chunks. Uses `pages` when the loader provided them."""
    name = str(parsed.get("name") or parsed.get("url") or "document")
    pages = parsed.get("pages") or [{"page": None, "text": parsed.get("text") or ""}]
    return chunk_pages(pages, max_chars=max_chars, source_name=name)
