"""Learned memory, scoped per data source. Nothing is stored globally by accident.

- rules: {"text", "source_id", "created_at"} — saved only after the user confirms.
- mappings: {"source", "sheet", "term", "column"} — user-taught column meanings.
- successful_plans: only plans that passed validation unchanged; at most 5 per source, 100 total.
"""
import contextvars
import json
import threading
from datetime import datetime
from pathlib import Path

MEMORY_FILE = Path(__file__).with_name("workspace_memory.json")
PLANS_PER_SOURCE = 5
_lock = threading.Lock()
# Workspace scope: when a channel sets it (backend.db-backed deployments), every call below goes to backend.learning
# for that workspace instead of the JSON file. Tests and the legacy single-user install keep the file.
_scope = contextvars.ContextVar("memory_scope", default=None)


def set_scope(workspace_id, user_id=None):
    """Route learning to the database for this workspace (for the current thread / request). None → JSON file."""
    _scope.set({"workspace_id": workspace_id, "user_id": user_id} if workspace_id else None)
    try:
        import prompt_builder
        prompt_builder.set_workspace(workspace_id)       # the workspace's AI instructions follow the same scope
    except Exception:
        pass


def scope():
    return _scope.get()


def _empty():
    return {"rules": [], "mappings": [], "successful_plans": []}


def _load():
    if not MEMORY_FILE.exists():
        return _empty()
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    # migrate the old format (rules were plain strings and applied to everyone) into scoped rules with no scope,
    # which get_context() no longer returns — they must be re-confirmed for a source.
    data["rules"] = [r if isinstance(r, dict) else {"text": r, "source_id": None, "legacy": True} for r in data.get("rules", [])]
    data.setdefault("mappings", [])
    data.setdefault("successful_plans", [])
    return data


def _save(data):
    MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def add_rule(text: str, source_id: str | None = None):
    """Store a confirmed rule for one source. A rule without source_id is never sent to the planner."""
    sc = scope()
    if sc:
        from backend import learning
        return learning.add_rule(sc["workspace_id"], source_id, text, sc.get("user_id"))
    with _lock:
        data = _load()
        if text and not any(r.get("text") == text and r.get("source_id") == source_id for r in data["rules"]):
            data["rules"].append({"text": text, "source_id": source_id, "created_at": datetime.now().isoformat()})
            data["rules"] = data["rules"][-100:]
            _save(data)


def add_mapping(source: str, sheet: str | None, user_term: str, column: str):
    sc = scope()
    if sc:
        from backend import learning
        return learning.add_mapping(sc["workspace_id"], source, sheet, user_term, column, user_id=sc.get("user_id"))
    with _lock:
        data = _load()
        item = {"source": source, "sheet": sheet, "term": user_term, "column": column, "updated_at": datetime.now().isoformat()}
        data["mappings"] = [x for x in data["mappings"] if not (x.get("source") == source and x.get("sheet") == sheet and x.get("term") == user_term)]
        data["mappings"].append(item)
        data["mappings"] = data["mappings"][-200:]
        _save(data)


def add_plan(question: str, plan: dict, validated: bool = False):
    """Only validated plans become examples. Keeps the newest PLANS_PER_SOURCE per source."""
    if not validated or not plan.get("source_id"):
        return
    sc = scope()
    if sc:
        from backend import learning
        return learning.add_plan(sc["workspace_id"], question, plan, validated)
    with _lock:
        data = _load()
        sid = plan["source_id"]
        same = [p for p in data["successful_plans"] if (p.get("plan") or {}).get("source_id") == sid]
        others = [p for p in data["successful_plans"] if (p.get("plan") or {}).get("source_id") != sid]
        same = [p for p in same if p.get("question") != question]
        same.append({"question": question, "plan": plan, "updated_at": datetime.now().isoformat()})
        data["successful_plans"] = (others + same[-PLANS_PER_SOURCE:])[-100:]
        _save(data)


def get_context(limit=PLANS_PER_SOURCE, source_ids=None):
    """Memory for the planner, restricted to the sources connected right now."""
    sc = scope()
    if sc:
        from backend import learning
        return learning.get_context(sc["workspace_id"], source_ids, limit)
    data = _load()
    ids = set(source_ids or [])
    plans = [p for p in data["successful_plans"] if (p.get("plan") or {}).get("source_id") in ids]
    maps = [m for m in data["mappings"] if m.get("source") in ids]
    rules = [r["text"] for r in data["rules"] if r.get("source_id") in ids]
    return {"rules": rules[-20:], "mappings": maps[-50:], "successful_plans": plans[-limit:]}


def forget_source(source_id: str):
    sc = scope()
    if sc:
        from backend import learning
        return learning.forget_source(sc["workspace_id"], source_id)
    with _lock:
        data = _load()
        data["rules"] = [r for r in data["rules"] if r.get("source_id") != source_id]
        data["mappings"] = [m for m in data["mappings"] if m.get("source") != source_id]
        data["successful_plans"] = [p for p in data["successful_plans"] if (p.get("plan") or {}).get("source_id") != source_id]
        _save(data)


def clear_memory():
    sc = scope()
    if sc:
        from backend import learning
        return learning.clear(sc["workspace_id"])
    with _lock:
        _save(_empty())
