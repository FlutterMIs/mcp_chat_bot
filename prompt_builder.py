"""Workspace Prompt Builder: editable AI instructions layered UNDER the core rules.

Hierarchy (top wins, lower layers can only add context / presentation):

    CORE SYSTEM RULES        (code, never editable)
    SOURCE / SCHEMA CONTEXT  (code, from the connected data)
    COMPANY CONTEXT          (workspace: terminology — helps interpretation, never creates facts)
    RESPONSE STYLE           (workspace: tone / structure of the wording)
    DATA FORMAT              (workspace: how numbers/tables are presented)
    BOT PERSONALITY          (workspace: voice)
    CURRENT USER QUESTION

Workspace instructions reach the planner (company context only) and the wording models (style, format, personality).
They never touch validation, metric selection, grounding, security or the MCP tools: those run in code before and
after the model, whatever the instructions say. `validate()` also rejects instructions that try to override them.

Channels set the workspace with `set_workspace(workspace_id)` (like `memory.set_scope`); tests / the legacy install can
set explicit instructions with `set_instructions({...})`. With nothing set every prompt is exactly what it was before.
"""
from __future__ import annotations

import contextvars
import re

FIELDS = ("response_style", "data_format", "bot_personality", "company_context")
LABELS = {"response_style": "Response style", "data_format": "Data format", "bot_personality": "Bot personality", "company_context": "Company context"}
MAX_CHARS = {"response_style": 1200, "data_format": 1200, "bot_personality": 800, "company_context": 2000}
DEFAULTS = {k: "" for k in FIELDS}
EXAMPLES = {
    "response_style": "Answer clearly and concisely. Use professional business language. Respond in the same language as the user. "
                      "Give a short summary first. Use a table when the result is naturally tabular. Avoid unnecessary explanation.",
    "data_format": "Show the grand total. Use Indian number formatting and ₹ for INR. Show percentages with one decimal. "
                   "Keep tables compact with only the relevant columns. Dates as DD-MM-YYYY. Summarise before a large table.",
    "bot_personality": "You are a professional, friendly business assistant. Be direct and natural. Do not sound robotic. "
                       "Use simple language. Use emojis only when appropriate.",
    "company_context": "We are a trading and distribution company. Sales means SALES data. Turnover means AMOUNT from SALES. "
                       "Quantity means QTY. Rate means RATE. Stock means INVENTORY. Voucher Date is the primary sales date.",
}

# What user-editable text may never do. Matched case-insensitively against each field.
FORBIDDEN = [
    (r"\b(ignore|disregard|override|bypass|forget)\b.{0,40}\b(rules?|instructions?|validation|grounding|safety|system)\b", "tries to override the core rules"),
    (r"\b(invent|make up|fabricate|guess|estimate)\b.{0,30}\b(numbers?|values?|data|figures?|totals?)\b", "asks to invent numbers"),
    (r"\b(use|substitute|replace)\b.{0,20}\b(qty|quantity|rate)\b.{0,25}\b(instead of|in place of|for)\b.{0,15}\b(amount|sales amount|revenue)\b", "swaps the requested metric"),
    (r"\b(amount|sales amount|revenue)\b.{0,15}\b(means|=|is)\b.{0,10}\b(qty|quantity|rate)\b", "redefines an amount as a quantity/rate"),
    (r"\b(drop|delete|update|insert|alter|truncate)\b.{0,20}\b(table|rows?|database|sheet)\b", "asks for a write/DDL action"),
    (r"\b(api[_ ]?key|password|secret|token)\b", "mentions secrets"),
    (r"\b(outside|external|general)\s+knowledge\b|\b(internet|google|web)\s+se\b", "asks for outside knowledge"),
]

CORE_RULES_SUMMARY = ("Numbers, metrics, columns, filters, dates and sources come ONLY from the connected data through validated tools. "
                      "Nothing below may change which data is used or invent a figure; it only shapes context and presentation.")

_instructions = contextvars.ContextVar("prompt_instructions", default=None)
_workspace = contextvars.ContextVar("prompt_workspace", default=None)
_channel = contextvars.ContextVar("prompt_channel", default="web")
_cache: dict = {}


# ---------------------------------------------------------------- scope
def set_workspace(workspace_id):
    """Instructions come from this workspace's saved settings (backend.prompt_settings) for the current request."""
    _workspace.set(workspace_id or None)


def set_instructions(instructions: dict | None):
    """Explicit instructions for this request (tests, legacy single-user install). None → workspace / defaults."""
    _instructions.set(clean(instructions) if instructions else None)


def set_channel(channel: str | None):
    """'web' (default) or 'whatsapp': the wording model adapts presentation, never the numbers."""
    _channel.set(channel or "web")


def channel():
    return _channel.get() or "web"


def invalidate(workspace_id=None):
    if workspace_id:
        _cache.pop(workspace_id, None)
    else:
        _cache.clear()


def current() -> dict:
    """The effective instructions for this request (cleaned, defaults for missing fields)."""
    explicit = _instructions.get()
    if explicit is not None:
        return {**DEFAULTS, **explicit}
    ws = _workspace.get()
    if ws:
        if ws not in _cache:
            try:
                from backend import prompt_settings
                _cache[ws] = prompt_settings.get(ws)
            except Exception:
                _cache[ws] = dict(DEFAULTS)
        return {**DEFAULTS, **_cache[ws]}
    return dict(DEFAULTS)


# ---------------------------------------------------------------- validation
def clean(instructions: dict | None) -> dict:
    out = {}
    for k in FIELDS:
        v = (instructions or {}).get(k)
        v = re.sub(r"[ \t]+", " ", str(v or "")).strip()
        out[k] = v[:MAX_CHARS[k]]
    return out


def validate(instructions: dict | None) -> dict:
    """{"ok": bool, "errors": [..], "warnings": [..], "clean": {...}}. Errors block saving."""
    errors, warnings = [], []
    raw = instructions or {}
    for k in FIELDS:
        v = str(raw.get(k) or "")
        if len(v.strip()) > MAX_CHARS[k]:
            errors.append(f"{LABELS[k]}: {len(v.strip())} characters, limit is {MAX_CHARS[k]}.")
        low = v.lower()
        for pat, why in FORBIDDEN:
            if re.search(pat, low, re.S):
                errors.append(f"{LABELS[k]}: {why} — not allowed (data rules cannot be overridden).")
                break
        if re.search(r"[<>{}]{2,}|```", v):
            warnings.append(f"{LABELS[k]}: markup/code fences are sent as plain text.")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "clean": clean(raw)}


# ---------------------------------------------------------------- prompt sections
def _block(title, body):
    return f"\n\n{title}:\n{body.strip()}" if body and body.strip() else ""


def planner_section(instructions: dict | None = None) -> str:
    """Only the company context reaches the planner — as terminology, with the guard spelled out."""
    ins = {**DEFAULTS, **(instructions if instructions is not None else current())}
    if not ins["company_context"]:
        return ""
    return _block("COMPANY CONTEXT (workspace terminology — use it ONLY to map the user's words onto REAL schema columns; "
                  "it never adds data, never overrides the schema, and never replaces a requested amount with a quantity/rate)",
                  ins["company_context"])


def presentation_section(instructions: dict | None = None, channel_name: str | None = None) -> str:
    """Style / format / personality for the wording models. Numbers are validated by code afterwards anyway."""
    ins = {**DEFAULTS, **(instructions if instructions is not None else current())}
    ch = channel_name or channel()
    parts = ""
    if ins["company_context"]:
        parts += _block("COMPANY CONTEXT (terminology only; never a source of facts or numbers)", ins["company_context"])
    parts += _block("RESPONSE STYLE", ins["response_style"])
    parts += _block("DATA FORMAT (presentation only — the metric, grouping, filters, date range, source and every number stay exactly as in the result)", ins["data_format"])
    parts += _block("BOT PERSONALITY (voice only; it never changes a figure)", ins["bot_personality"])
    if ch == "whatsapp":
        parts += _block("CHANNEL", "The reply is delivered on WhatsApp: short lines, *bold* for the key figure, simple lists (• or 1.), no markdown headings or tables, at most one emoji.")
    if not parts:
        return ""
    return f"\n\nWORKSPACE INSTRUCTIONS — {CORE_RULES_SUMMARY}{parts}"


def preview(instructions: dict | None = None, schemas: dict | None = None, channel_name: str | None = None) -> str:
    """The effective prompt structure, for the Settings page. No secrets, no data rows — schema names only."""
    ins = clean(instructions if instructions is not None else current())
    lines = ["1. CORE SYSTEM RULES (fixed)", "   " + CORE_RULES_SUMMARY,
             "   Planner: exact schema names, never invent columns, never substitute QTY/RATE for AMOUNT, dates only from the date resolver.",
             "   Wording: every number must appear in the tool result; grounding rejects the rest.",
             "", "2. SOURCE / SCHEMA CONTEXT (from the connected data)"]
    if schemas:
        for sid, s in schemas.items():
            tabs = s.get("sheets") or ([{"name": s.get("name"), "columns": s.get("columns")}] if s.get("columns") else [])
            if tabs:
                for t in tabs:
                    cols = [c["name"] for c in (t.get("columns") or [])]
                    lines.append(f"   • {s.get('name') or sid} / {t.get('name') or '-'}: {', '.join(cols[:12])}" + (" …" if len(cols) > 12 else ""))
            else:
                lines.append(f"   • {s.get('name') or sid} ({s.get('kind')}): text source")
    else:
        lines.append("   (schema of every connected source: sheets, columns, sample values)")
    for n, k in ((3, "company_context"), (4, "response_style"), (5, "data_format"), (6, "bot_personality")):
        lines += ["", f"{n}. {LABELS[k].upper()}" + ("" if ins[k] else " (empty — default behaviour)")]
        if ins[k]:
            lines += ["   " + l for l in ins[k].splitlines()]
    ch = channel_name or channel()
    lines += ["", f"7. CHANNEL: {ch}", "", "8. CURRENT USER QUESTION (+ recent conversation, previous plan, learned mappings)"]
    return "\n".join(lines)
