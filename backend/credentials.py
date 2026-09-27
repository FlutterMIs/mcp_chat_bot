"""Secrets at rest: Google refresh tokens, database URLs. Fernet (AES-128-CBC + HMAC) keyed from APP_SECRET_KEY.

The plaintext never goes into `sources.connection_config`, logs, FinalResponse or the browser. `reveal()` is called only
by the loader that needs the secret, with the workspace it must belong to.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken

from .db import SourceCredential, now, session_scope
from .workspaces import Forbidden


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.getenv("APP_SECRET_KEY", "")
    if not key:
        raise SecretKeyMissing("APP_SECRET_KEY set karo (.env) — credentials encrypt karne ke liye zaroori hai.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def encrypt(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()


def decrypt(blob: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except InvalidToken as e:
        raise ValueError("Credential decrypt nahi hua — APP_SECRET_KEY badal gaya lagta hai.") from e


def store(workspace_id: str, kind: str, payload: dict, google_account_email=None, scopes=None, expires_at=None, credential_id=None) -> str:
    """Encrypt and save; returns the credential id. With credential_id, rotates the existing row in place."""
    with session_scope() as s:
        if credential_id:
            row = s.get(SourceCredential, credential_id)
            if row is None or row.workspace_id != workspace_id:
                raise Forbidden("Credential is workspace ka nahi hai.")
            row.encrypted_blob, row.rotated_at = encrypt(payload), now()
            row.google_account_email = google_account_email or row.google_account_email
            row.scopes, row.expires_at = scopes or row.scopes, expires_at or row.expires_at
            return row.id
        row = SourceCredential(workspace_id=workspace_id, kind=kind, encrypted_blob=encrypt(payload), google_account_email=google_account_email,
                               scopes=scopes, expires_at=expires_at)
        s.add(row)
        s.flush()
        return row.id


def reveal(workspace_id: str, credential_id: str) -> dict:
    with session_scope() as s:
        row = s.get(SourceCredential, credential_id)
        if row is None or row.workspace_id != workspace_id:
            raise Forbidden("Credential is workspace ka nahi hai.")
        return decrypt(row.encrypted_blob)


def describe(workspace_id: str, credential_id: str) -> dict:
    """What the UI may show: kind, account email, scopes — never the secret."""
    with session_scope() as s:
        row = s.get(SourceCredential, credential_id)
        if row is None or row.workspace_id != workspace_id:
            raise Forbidden("Credential is workspace ka nahi hai.")
        return {"id": row.id, "kind": row.kind, "google_account_email": row.google_account_email, "scopes": row.scopes, "expires_at": row.expires_at}


def redact(text: str, secrets: list[str]) -> str:
    out = str(text or "")
    for sct in secrets:
        if sct and len(str(sct)) >= 6:
            out = out.replace(str(sct), "<secret>")
    return out
