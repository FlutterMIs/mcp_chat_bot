"""Users and login. Two identity paths, one `users` row:

- google_oidc: Streamlit `st.login()` (or any OIDC provider) proves the identity; we look the user up by subject/email.
- password: email + password (scrypt, stdlib) for local/dev deployments without an identity provider.

Every user owns one personal workspace, created on first login. Passwords and tokens never leave the server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets

from sqlalchemy import select

from .db import User, Workspace, WorkspaceMember, now, session_scope

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    pass


def hash_password(password: str) -> str:
    if len(password or "") < 8:
        raise AuthError("Password kam se kam 8 characters ka ho.")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, salt, key = (stored or "").split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(key)
        got = hashlib.scrypt((password or "").encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1, dklen=len(want))
        return hmac.compare_digest(want, got)
    except Exception:
        return False


def _norm_email(email):
    e = (email or "").strip().lower()
    if not _EMAIL.match(e):
        raise AuthError("Email address sahi nahi lag raha.")
    return e


def ensure_personal_workspace(s, user: User) -> Workspace:
    ws = s.scalar(select(Workspace).where(Workspace.owner_user_id == user.id).order_by(Workspace.created_at))
    if ws is None:
        ws = Workspace(name=f"{(user.name or user.email.split('@')[0])}'s workspace", owner_user_id=user.id)
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    return ws


def register(email, password, name=None) -> dict:
    """Create a password user + personal workspace. Returns {"user_id", "workspace_id", "email", "name"}."""
    email = _norm_email(email)
    with session_scope() as s:
        if s.scalar(select(User).where(User.email == email)):
            raise AuthError("Is email se account already hai — login karo.")
        u = User(email=email, name=(name or "").strip() or None, auth_provider="password", password_hash=hash_password(password), last_login_at=now())
        s.add(u)
        s.flush()
        ws = ensure_personal_workspace(s, u)
        return {"user_id": u.id, "workspace_id": ws.id, "email": u.email, "name": u.name}


def login_password(email, password) -> dict:
    email = _norm_email(email)
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        if u is None or not u.is_active or not verify_password(password, u.password_hash):
            raise AuthError("Email ya password galat hai.")
        u.last_login_at = now()
        ws = ensure_personal_workspace(s, u)
        return {"user_id": u.id, "workspace_id": ws.id, "email": u.email, "name": u.name}


def login_oidc(email, name=None, subject=None) -> dict:
    """Identity proven by an OIDC provider (Streamlit st.user). Get-or-create the user; never trust a password here."""
    email = _norm_email(email)
    with session_scope() as s:
        u = None
        if subject:
            u = s.scalar(select(User).where(User.oidc_subject == str(subject)))
        u = u or s.scalar(select(User).where(User.email == email))
        if u is None:
            u = User(email=email, name=(name or "").strip() or None, auth_provider="google_oidc", oidc_subject=str(subject) if subject else None)
            s.add(u)
            s.flush()
        else:
            u.oidc_subject = u.oidc_subject or (str(subject) if subject else None)
            u.name = u.name or ((name or "").strip() or None)
        if not u.is_active:
            raise AuthError("Ye account disabled hai.")
        u.last_login_at = now()
        ws = ensure_personal_workspace(s, u)
        return {"user_id": u.id, "workspace_id": ws.id, "email": u.email, "name": u.name}


def dev_user(email="local@localhost", name="Local user") -> dict:
    """APP_AUTH_MODE=none (single-user local install, tests): one fixed account, no password."""
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        if u is None:
            u = User(email=email, name=name, auth_provider="none")
            s.add(u)
            s.flush()
        ws = ensure_personal_workspace(s, u)
        return {"user_id": u.id, "workspace_id": ws.id, "email": u.email, "name": u.name}


def auth_mode() -> str:
    """oidc (st.login) | password | none. Default: password. `none` is only for a local single-user install."""
    return (os.getenv("APP_AUTH_MODE") or "password").strip().lower()
