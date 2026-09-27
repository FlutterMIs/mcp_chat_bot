"""Link a WhatsApp number to a workspace with a one-time code generated in Settings (`/link ABCD1234` in the chat).
Numbers are stored hashed (sha256) with the last 4 digits for display."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select

from .db import ChannelIdentity, now, session_scope
from .workspaces import Forbidden

CODE_TTL = timedelta(hours=24)


def _hash(number):
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return hashlib.sha256(digits.encode()).hexdigest(), digits[-4:]


def create_code(workspace_id, user_id):
    code = secrets.token_hex(4).upper()
    with session_scope() as s:
        s.add(ChannelIdentity(workspace_id=workspace_id, user_id=user_id, channel="whatsapp", external_hash="", external_last4="", link_code=code))
    return code


def link(code, number):
    """Called by the WhatsApp bot on `/link <code>`. Returns {"workspace_id"} or raises ValueError."""
    code = (code or "").strip().upper()
    h, last4 = _hash(number)
    with session_scope() as s:
        row = s.scalar(select(ChannelIdentity).where(ChannelIdentity.channel == "whatsapp", ChannelIdentity.link_code == code))
        if row is None or (now() - row.created_at.replace(tzinfo=row.created_at.tzinfo or now().tzinfo)) > CODE_TTL:
            raise ValueError("Code galat hai ya expire ho gaya. Web app → Settings → 'Generate link code'.")
        for old in s.scalars(select(ChannelIdentity).where(ChannelIdentity.channel == "whatsapp", ChannelIdentity.external_hash == h, ChannelIdentity.id != row.id)):
            s.delete(old)                                  # a number belongs to one workspace at a time
        row.external_hash, row.external_last4, row.link_code, row.linked_at = h, last4, None, now()
        return {"workspace_id": row.workspace_id, "user_id": row.user_id}


def workspace_for(number):
    """The workspace a WhatsApp number is linked to, or None (then the bot uses its .env defaults)."""
    h, _ = _hash(number)
    if not h:
        return None
    with session_scope() as s:
        row = s.scalar(select(ChannelIdentity).where(ChannelIdentity.channel == "whatsapp", ChannelIdentity.external_hash == h, ChannelIdentity.linked_at.is_not(None)))
        return {"workspace_id": row.workspace_id, "user_id": row.user_id} if row else None


def list_links(workspace_id):
    with session_scope() as s:
        rows = s.scalars(select(ChannelIdentity).where(ChannelIdentity.workspace_id == workspace_id, ChannelIdentity.channel == "whatsapp").order_by(ChannelIdentity.created_at)).all()
        return [{"id": r.id, "link_code": r.link_code, "linked_at": r.linked_at, "external_last4": r.external_last4} for r in rows]


def unlink(workspace_id, identity_id):
    with session_scope() as s:
        row = s.get(ChannelIdentity, identity_id)
        if row is None or row.workspace_id != workspace_id:
            raise Forbidden("Ye link is workspace ka nahi hai.")
        s.delete(row)
