"""Per-workspace, per-source learning (replaces the global workspace_memory.json).

Only confirmed things are stored: mappings the user chose or confirmed, rules the user said "haan" to, and plans that
passed validation unchanged. Scope is always (workspace, source): nothing learned here reaches another workspace.
`memory.py` delegates here when a workspace scope is active, so `analyst.py` did not change.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from .db import SourceMapping, SourceRule, ValidatedPlan, now, session_scope

PLANS_PER_SOURCE = 5


def add_rule(workspace_id, source_id, text, user_id=None):
    if not text or not source_id:
        return
    with session_scope() as s:
        if s.scalar(select(SourceRule).where(SourceRule.workspace_id == workspace_id, SourceRule.source_id == source_id, SourceRule.text == text)):
            return
        s.add(SourceRule(workspace_id=workspace_id, source_id=source_id, text=text, confirmed_by=user_id, confirmed_at=now()))


def add_mapping(workspace_id, source_id, sheet, term, column, kind="choice", user_id=None):
    if not (source_id and term and column):
        return
    with session_scope() as s:
        s.execute(delete(SourceMapping).where(SourceMapping.workspace_id == workspace_id, SourceMapping.source_id == source_id,
                                              SourceMapping.sheet.is_(sheet) if sheet is None else SourceMapping.sheet == sheet, SourceMapping.term == term))
        s.add(SourceMapping(workspace_id=workspace_id, source_id=source_id, sheet=sheet, term=term, column=column, kind=kind, confirmed_by=user_id, confirmed_at=now()))


def add_plan(workspace_id, question, plan, validated=False):
    if not validated or not (plan or {}).get("source_id"):
        return
    sid = plan["source_id"]
    with session_scope() as s:
        s.execute(delete(ValidatedPlan).where(ValidatedPlan.workspace_id == workspace_id, ValidatedPlan.source_id == sid, ValidatedPlan.question == question))
        s.add(ValidatedPlan(workspace_id=workspace_id, source_id=sid, question=question, plan=plan))
        s.flush()
        rows = s.scalars(select(ValidatedPlan).where(ValidatedPlan.workspace_id == workspace_id, ValidatedPlan.source_id == sid).order_by(ValidatedPlan.created_at.desc())).all()
        for old in rows[PLANS_PER_SOURCE:]:
            s.delete(old)


def get_context(workspace_id, source_ids, limit=PLANS_PER_SOURCE):
    ids = list(source_ids or [])
    if not ids:
        return {"rules": [], "mappings": [], "successful_plans": []}
    with session_scope() as s:
        rules = s.scalars(select(SourceRule).where(SourceRule.workspace_id == workspace_id, SourceRule.source_id.in_(ids), SourceRule.active == True)  # noqa: E712
                          .order_by(SourceRule.created_at)).all()
        maps = s.scalars(select(SourceMapping).where(SourceMapping.workspace_id == workspace_id, SourceMapping.source_id.in_(ids)).order_by(SourceMapping.updated_at)).all()
        plans = s.scalars(select(ValidatedPlan).where(ValidatedPlan.workspace_id == workspace_id, ValidatedPlan.source_id.in_(ids)).order_by(ValidatedPlan.created_at)).all()
        return {"rules": [r.text for r in rules][-20:],
                "mappings": [{"source": m.source_id, "sheet": m.sheet, "term": m.term, "column": m.column, "updated_at": m.updated_at} for m in maps][-50:],
                "successful_plans": [{"question": p.question, "plan": p.plan, "updated_at": p.created_at} for p in plans][-limit:]}


def list_learned(workspace_id, source_id):
    """For the Data Sources UI: what the assistant remembers about this source."""
    with session_scope() as s:
        maps = s.scalars(select(SourceMapping).where(SourceMapping.workspace_id == workspace_id, SourceMapping.source_id == source_id)).all()
        rules = s.scalars(select(SourceRule).where(SourceRule.workspace_id == workspace_id, SourceRule.source_id == source_id, SourceRule.active == True)).all()  # noqa: E712
        return {"mappings": [{"id": m.id, "sheet": m.sheet, "term": m.term, "column": m.column, "kind": m.kind} for m in maps],
                "rules": [{"id": r.id, "text": r.text} for r in rules]}


def forget_mapping(workspace_id, mapping_id):
    with session_scope() as s:
        m = s.get(SourceMapping, mapping_id)
        if m and m.workspace_id == workspace_id:
            s.delete(m)


def forget_rule(workspace_id, rule_id):
    with session_scope() as s:
        r = s.get(SourceRule, rule_id)
        if r and r.workspace_id == workspace_id:
            r.active = False


def forget_source(workspace_id, source_id):
    with session_scope() as s:
        for model in (SourceMapping, SourceRule, ValidatedPlan):
            s.execute(delete(model).where(model.workspace_id == workspace_id, model.source_id == source_id))


def clear(workspace_id):
    with session_scope() as s:
        for model in (SourceMapping, SourceRule, ValidatedPlan):
            s.execute(delete(model).where(model.workspace_id == workspace_id))
