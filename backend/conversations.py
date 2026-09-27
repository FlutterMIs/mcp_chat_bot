"""Conversations, messages and results in the database. The analyst's `Conversation` dataclass is hydrated from
`conversations.analytical_context` before a question and written back after it, so every channel and device sees the
same analytical state. Tables/values of an answer live in `results`; a message stores its FinalResponse without rows.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

import pandas as pd
from sqlalchemy import delete, select

from .db import Conversation as ConvRow, Message, Result, now, session_scope
from .workspaces import require_conversation

MAX_ROWS = 2000          # rows kept per result; the full frame is what the UI exported anyway (cap keeps rows JSON small)
CONTEXT_KEYS = ("state", "last_plan", "recent_plans", "focus", "pending_rule", "pending_choice", "forced_table", "_asked_for", "totals", "last_cards", "history")


def _clean(obj):
    """JSON-safe copy: NaN → None, Timestamp/date → ISO, numpy → python."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return _clean(obj.item())
        except Exception:
            return str(obj)
    if isinstance(obj, (datetime, pd.Timestamp)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, float):
        return obj
    return str(obj)


def title_from(question):
    words = re.sub(r"\s+", " ", str(question or "")).strip().split(" ")
    t = " ".join(words[:7])
    return (t[:60] + "…") if len(t) > 60 or len(words) > 7 else (t or "New chat")


def create(workspace_id, user_id, channel="web", title=None, source_ids=None):
    with session_scope() as s:
        c = ConvRow(workspace_id=workspace_id, created_by=user_id, channel=channel, title=title or "New chat", source_ids=list(source_ids or []), analytical_context={})
        s.add(c)
        s.flush()
        return _row(c)


def _row(c: ConvRow):
    return {"id": c.id, "workspace_id": c.workspace_id, "title": c.title, "channel": c.channel, "source_ids": list(c.source_ids or []),
            "last_message_at": c.last_message_at, "created_at": c.created_at, "archived": c.archived}


def list_conversations(workspace_id, limit=50, channel=None, include_archived=False):
    with session_scope() as s:
        q = select(ConvRow).where(ConvRow.workspace_id == workspace_id)
        if channel:
            q = q.where(ConvRow.channel == channel)
        if not include_archived:
            q = q.where(ConvRow.archived == False)  # noqa: E712
        rows = s.scalars(q.order_by(ConvRow.last_message_at.desc().nullslast(), ConvRow.created_at.desc()).limit(limit)).all()
        return [_row(c) for c in rows]


def get(workspace_id, conversation_id):
    with session_scope() as s:
        return _row(require_conversation(s, workspace_id, conversation_id))


def rename(workspace_id, conversation_id, title):
    with session_scope() as s:
        c = require_conversation(s, workspace_id, conversation_id)
        c.title = (title or "").strip()[:200] or c.title


def archive(workspace_id, conversation_id, archived=True):
    with session_scope() as s:
        require_conversation(s, workspace_id, conversation_id).archived = archived


def delete_conversation(workspace_id, conversation_id):
    with session_scope() as s:
        c = require_conversation(s, workspace_id, conversation_id)
        s.execute(delete(Message).where(Message.conversation_id == c.id))
        s.execute(delete(Result).where(Result.conversation_id == c.id))
        s.delete(c)


def find_by_external(workspace_id, channel, external_key):
    """WhatsApp: one conversation per (workspace, chat). `external_key` is stored in source_ids-free metadata title."""
    with session_scope() as s:
        c = s.scalar(select(ConvRow).where(ConvRow.workspace_id == workspace_id, ConvRow.channel == channel, ConvRow.archived == False,  # noqa: E712
                                           ConvRow.analytical_context["external_key"].as_string() == external_key))
        return _row(c) if c else None


# ---------------------------------------------------------------- analyst.Conversation ⇄ rows

def hydrate(workspace_id, conversation_id, schemas):
    """An analyst.Conversation with this chat's analytical context and the given (current) source schemas."""
    from analyst import Conversation as AConv
    with session_scope() as s:
        c = require_conversation(s, workspace_id, conversation_id)
        ctx = dict(c.analytical_context or {})
        last_result = None
        if ctx.get("last_result_id"):
            r = s.get(Result, ctx["last_result_id"])
            if r is not None and r.rows is not None:
                last_result = pd.DataFrame(r.rows, columns=r.columns or None)
        conv = AConv(schemas=dict(schemas), last_plan=ctx.get("last_plan"), history=list(ctx.get("history") or []), recent_plans=list(ctx.get("recent_plans") or []),
                     focus=ctx.get("focus") if ctx.get("focus") in schemas else None, pending_rule=ctx.get("pending_rule"), state=ctx.get("state"),
                     last_result=last_result, pending_choice=ctx.get("pending_choice"), forced_table=tuple(ctx["forced_table"]) if ctx.get("forced_table") else None,
                     last_cards=list(ctx.get("last_cards") or []), totals=dict(ctx.get("totals") or {}))
        conv._asked_for = ctx.get("_asked_for") or ""
        # a state that points at a source no longer connected is dropped (the next question starts fresh)
        if conv.state and conv.state.get("source_id") not in schemas:
            conv.state = None
        return conv


def save_turn(workspace_id, conversation_id, question, reply, final, conv, audio_ref=None, request_id=None):
    """Persist one exchange: user message, assistant message (+ result row for tables/values), analytical context."""
    df = reply.df if getattr(reply, "df", None) is not None else None
    with session_scope() as s:
        c = require_conversation(s, workspace_id, conversation_id)
        plan = reply.plan or {}
        result_id = None
        if df is not None or reply.value is not None or reply.cards:
            res = Result(conversation_id=c.id, workspace_id=workspace_id, source_id=plan.get("source_id"), metric=plan.get("metric") or reply.metric,
                         aggregation=plan.get("aggregation"), dimensions=list(plan.get("group_by") or []), filters=_clean(plan.get("filters") or []),
                         period=_clean(plan.get("period")), calculation=_calc_text(plan), columns=[str(x) for x in df.columns] if df is not None else None,
                         rows=_clean(df.head(MAX_ROWS).to_dict(orient="records")) if df is not None else None, row_count=int(len(df)) if df is not None else 0,
                         value=float(reply.value) if isinstance(reply.value, (int, float)) and not isinstance(reply.value, bool) else None)
            s.add(res)
            s.flush()
            result_id = res.id
        s.add(Message(conversation_id=c.id, role="user", content=question, audio_ref=audio_ref))
        s.add(Message(conversation_id=c.id, role="assistant", content=final.answer, result_id=result_id, final_response=_final_json(final, reply, result_id),
                      source_context={"source_id": plan.get("source_id"), "sheet": plan.get("sheet_name")} if plan else None,
                      intent=_clean({"shape": final.shape, "kind": final.kind, "request_id": request_id})))
        ctx = {k: _clean(getattr(conv, k, None)) for k in CONTEXT_KEYS}
        ctx["last_result_id"] = result_id if df is not None else (c.analytical_context or {}).get("last_result_id") if conv.last_result is not None else None
        ctx["external_key"] = (c.analytical_context or {}).get("external_key")
        c.analytical_context = ctx
        c.last_message_at = now()
        used = set(c.source_ids or []) | ({plan["source_id"]} if plan.get("source_id") else set())
        c.source_ids = sorted(used)
        if c.title == "New chat":
            c.title = title_from(question)
        return result_id


def _calc_text(plan):
    if not plan:
        return None
    ms = plan.get("metrics")
    if ms:
        return "; ".join(f"{m.get('label')} = {m.get('aggregation', '').upper()} of {m.get('column')}" for m in ms)
    if plan.get("metric"):
        return f"{(plan.get('aggregation') or 'sum').upper()} of {plan['metric']}"
    return None


def _final_json(final, reply, result_id):
    return _clean({"shape": final.shape, "kind": final.kind, "metrics": final.metrics, "chart": final.chart, "table_note": final.table_note,
                   "drilldown": final.drilldown, "options": final.options, "source_info": final.source_info, "period": final.period, "value": final.value,
                   "date_range": final.date_range, "images": final.images, "videos": final.videos, "result_id": result_id, "metric": reply.metric,
                   "has_table": final.table is not None, "table_rows": int(len(final.table)) if final.table is not None else 0})


def messages(workspace_id, conversation_id, limit=200):
    """Messages for the UI, oldest first. Assistant messages carry `final_response` and, when there was a table, `df`."""
    with session_scope() as s:
        c = require_conversation(s, workspace_id, conversation_id)
        rows = s.scalars(select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at, Message.id).limit(limit)).all()
        out = []
        for m in rows:
            item = {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at, "final_response": m.final_response, "audio_ref": m.audio_ref}
            if m.role == "assistant" and m.result_id and (m.final_response or {}).get("has_table"):
                r = s.get(Result, m.result_id)
                if r is not None and r.rows is not None:
                    item["df"] = pd.DataFrame(r.rows, columns=r.columns or None)
                    item["table_note"] = (m.final_response or {}).get("table_note")
            out.append(item)
        return out


def result(workspace_id, result_id):
    with session_scope() as s:
        r = s.get(Result, result_id)
        if r is None or r.workspace_id != workspace_id:
            return None
        return {"id": r.id, "metric": r.metric, "aggregation": r.aggregation, "dimensions": r.dimensions, "filters": r.filters, "period": r.period,
                "calculation": r.calculation, "df": pd.DataFrame(r.rows, columns=r.columns or None) if r.rows is not None else None, "row_count": r.row_count,
                "value": r.value, "created_at": r.created_at}
