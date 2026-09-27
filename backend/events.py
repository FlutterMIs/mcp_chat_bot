"""Structured event log for operators: request_id, conversation_id, workspace_id, source_id, step name, duration, error.

Written as JSON lines to DATA_DIR/events.jsonl (always) and to the `events` table when APP_EVENTS_DB=1. Secrets and
message text never go in; phone numbers and emails are hashed by the caller (see whatsapp/log.py for the same rule).
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_request = contextvars.ContextVar("request_ctx", default={})
_lock = threading.Lock()
_SECRET = re.compile(r"(sk-or-[A-Za-z0-9_-]{6,}|Bearer\s+[A-Za-z0-9._-]+|refresh_token[\"':=\s]+[A-Za-z0-9._/-]+|password[\"':=\s]+\S+)", re.I)


def new_request_id():
    return "req_" + uuid.uuid4().hex[:16]


def bind(**ctx):
    """Attach request/conversation/workspace ids to every event logged in this context (thread/async task)."""
    cur = dict(_request.get())
    cur.update({k: v for k, v in ctx.items() if v is not None})
    _request.set(cur)
    return cur


def _path():
    root = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "app_data")
    root.mkdir(parents=True, exist_ok=True)
    return root / "events.jsonl"


def log_event(name, ms=None, **payload):
    ctx = _request.get()
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "name": name, **{k: ctx.get(k) for k in ("request_id", "conversation_id", "workspace_id", "source_id")}}
    if ms is not None:
        rec["ms"] = int(ms)
    if payload:
        rec["payload"] = json.loads(_SECRET.sub("<secret>", json.dumps(payload, ensure_ascii=False, default=str)[:4000]))
    with _lock:
        try:
            with _path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
    if os.getenv("APP_EVENTS_DB", "").lower() in ("1", "true", "yes"):
        try:
            from .db import Event, session_scope
            with session_scope() as s:
                s.add(Event(request_id=rec.get("request_id"), conversation_id=rec.get("conversation_id"), workspace_id=rec.get("workspace_id"),
                            source_id=rec.get("source_id"), name=name, ms=rec.get("ms"), payload=rec.get("payload")))
        except Exception:
            pass
    return rec


class timed:
    """`with timed("answer", question_len=12): ...` logs the block's duration (and the error type if it raised)."""

    def __init__(self, name, **payload):
        self.name, self.payload, self.t0 = name, payload, None

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, et, ev, tb):
        log_event(self.name, ms=(time.time() - self.t0) * 1000, **self.payload, **({"error": et.__name__} if et else {}))
        return False
