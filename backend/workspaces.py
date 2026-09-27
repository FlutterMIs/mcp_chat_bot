"""Workspace membership and authorization. Every read of a source, conversation or result goes through `require()`."""
from __future__ import annotations

from sqlalchemy import select

from .db import Conversation, Source, Workspace, WorkspaceMember, session_scope


class Forbidden(PermissionError):
    pass


def require(s, workspace_id: str, user_id: str | None) -> Workspace:
    """The workspace, if `user_id` is a member (or the caller is a trusted channel with user_id=None and a valid workspace).
    Raises Forbidden otherwise — never returns another user's workspace."""
    ws = s.get(Workspace, workspace_id)
    if ws is None:
        raise Forbidden("Workspace nahi mila.")
    if user_id is not None and not s.get(WorkspaceMember, (workspace_id, user_id)):
        raise Forbidden("Is workspace tak aapki access nahi hai.")
    return ws


def require_source(s, workspace_id: str, source_id: str) -> Source:
    src = s.get(Source, source_id)
    if src is None or src.workspace_id != workspace_id or src.is_deleted:
        raise Forbidden("Ye data source is workspace ka nahi hai.")
    return src


def require_conversation(s, workspace_id: str, conversation_id: str) -> Conversation:
    conv = s.get(Conversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise Forbidden("Ye chat is workspace ki nahi hai.")
    return conv


def list_for_user(user_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(select(Workspace, WorkspaceMember.role).join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                         .where(WorkspaceMember.user_id == user_id).order_by(Workspace.created_at)).all()
        return [{"id": w.id, "name": w.name, "role": role, "timezone": w.default_timezone} for w, role in rows]


def add_member(workspace_id: str, user_id: str, by_user_id: str, role="member"):
    with session_scope() as s:
        ws = require(s, workspace_id, by_user_id)
        me = s.get(WorkspaceMember, (workspace_id, by_user_id))
        if me is None or me.role != "owner":
            raise Forbidden("Sirf workspace owner members add kar sakta hai.")
        if not s.get(WorkspaceMember, (workspace_id, user_id)):
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=user_id, role=role, invited_by=by_user_id))
