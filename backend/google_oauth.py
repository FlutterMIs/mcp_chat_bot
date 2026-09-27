"""Google OAuth 2.0 for private Google Sheets (read-only), with plain `requests` — no Google SDK.

    start_url(ws, user)   → the consent URL (state = HMAC-signed workspace/user/nonce, so no server-side session)
    finish(code, state)   → exchanges the code, stores the *refresh token* encrypted in source_credentials
    load_workbook(...)    → every tab of a spreadsheet as DataFrames via the Sheets API (same shape as the public loader)

Env: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, APP_BASE_URL (redirect = APP_BASE_URL + /sources), APP_SECRET_KEY.
Tokens never reach the browser or the LLM. Scopes: spreadsheets.readonly + drive.metadata.readonly (titles only) + email.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import pandas as pd
import requests
from sqlalchemy import select

from . import credentials
from .db import SourceCredential, session_scope

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly", "https://www.googleapis.com/auth/drive.metadata.readonly", "openid", "email"]
STATE_TTL = 900


class OAuthError(RuntimeError):
    pass


def configured():
    return bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") and os.getenv("APP_BASE_URL") and os.getenv("APP_SECRET_KEY"))


def redirect_uri():
    return os.getenv("APP_BASE_URL", "").rstrip("/") + "/sources"


def _sign(payload: bytes) -> str:
    return hmac.new(os.getenv("APP_SECRET_KEY", "").encode(), payload, hashlib.sha256).hexdigest()[:32]


def make_state(workspace_id, user_id):
    payload = json.dumps({"ws": workspace_id, "u": user_id, "t": int(time.time()), "n": secrets.token_hex(8)}).encode()
    b = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{b}.{_sign(payload)}"


def read_state(state, workspace_id, user_id):
    try:
        b, sig = state.split(".", 1)
        payload = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
    except Exception as e:
        raise OAuthError("OAuth state kharab hai.") from e
    if not hmac.compare_digest(sig, _sign(payload)):
        raise OAuthError("OAuth state signature match nahi hui.")
    data = json.loads(payload)
    if data.get("ws") != workspace_id or data.get("u") != user_id:
        raise OAuthError("Ye Google consent kisi aur workspace/user ke liye tha.")
    if time.time() - int(data.get("t", 0)) > STATE_TTL:
        raise OAuthError("Consent link expire ho gaya; dobara try karo.")
    return data


def start_url(workspace_id, user_id):
    if not configured():
        raise OAuthError("Google OAuth configured nahi hai (GOOGLE_OAUTH_CLIENT_ID/SECRET, APP_BASE_URL, APP_SECRET_KEY).")
    q = {"client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID"), "redirect_uri": redirect_uri(), "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": make_state(workspace_id, user_id)}
    return AUTH_URL + "?" + urlencode(q)


def _token_request(data, http=requests):
    r = http.post(TOKEN_URL, data={"client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID"), "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"), **data}, timeout=30)
    if r.status_code != 200:
        raise OAuthError(f"Google token endpoint: HTTP {r.status_code}")
    return r.json()


def finish(code, state, workspace_id, user_id, http=requests):
    """Exchange the code; store the refresh token for this workspace. Returns credentials.describe(...)."""
    read_state(state, workspace_id, user_id)
    tok = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()}, http)
    refresh = tok.get("refresh_token")
    if not refresh:
        raise OAuthError("Google ne refresh token nahi diya (consent screen par 'offline access' zaroori hai).")
    email = None
    try:
        info = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {tok.get('access_token', '')}"}, timeout=15)
        email = info.json().get("email") if info.status_code == 200 else None
    except requests.RequestException:
        pass
    existing = None
    with session_scope() as s:
        if email:
            existing = s.scalar(select(SourceCredential).where(SourceCredential.workspace_id == workspace_id, SourceCredential.kind == "google_oauth",
                                                               SourceCredential.google_account_email == email))
        existing_id = existing.id if existing else None
    cid = credentials.store(workspace_id, "google_oauth", {"refresh_token": refresh}, google_account_email=email, scopes=tok.get("scope") or " ".join(SCOPES), credential_id=existing_id)
    return credentials.describe(workspace_id, cid)


def accounts(workspace_id):
    with session_scope() as s:
        rows = s.scalars(select(SourceCredential).where(SourceCredential.workspace_id == workspace_id, SourceCredential.kind == "google_oauth").order_by(SourceCredential.created_at)).all()
        return [{"id": r.id, "google_account_email": r.google_account_email or "Google account", "created_at": r.created_at} for r in rows]


def access_token(workspace_id, credential_id, http=requests):
    payload = credentials.reveal(workspace_id, credential_id)
    tok = _token_request({"refresh_token": payload["refresh_token"], "grant_type": "refresh_token"}, http)
    return tok["access_token"]


def _frame(values):
    if not values:
        return pd.DataFrame()
    width = max(len(r) for r in values)
    rows = [list(r) + [None] * (width - len(r)) for r in values]
    header = [str(h).strip() or f"col_{i + 1}" for i, h in enumerate(rows[0])]
    df = pd.DataFrame(rows[1:], columns=header)
    for c in df.columns:                                   # Sheets API returns strings; numbers come back as numbers
        conv = pd.to_numeric(df[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
        if len(df) and conv.notna().sum() >= 0.8 * df[c].notna().sum() and df[c].notna().sum():
            df[c] = conv
    df = df.replace({"": None})
    return df


def load_workbook(workspace_id, credential_id, sheet_id, name=None, http=requests):
    """{"kind": "workbook", "name", "url", "tabs": {title: DataFrame}} for a private spreadsheet."""
    from source_loader import normalize_columns
    token = access_token(workspace_id, credential_id, http)
    h = {"Authorization": f"Bearer {token}"}
    meta = http.get(f"{SHEETS_API}/{sheet_id}", params={"fields": "properties.title,sheets.properties.title"}, headers=h, timeout=60)
    if meta.status_code == 403:
        raise OAuthError("Is Google account ko ye sheet dekhne ki permission nahi hai.")
    if meta.status_code != 200:
        raise OAuthError(f"Sheets API: HTTP {meta.status_code}")
    m = meta.json()
    titles = [s["properties"]["title"] for s in m.get("sheets", [])]
    tabs = {}
    for i in range(0, len(titles), 10):
        chunk = titles[i:i + 10]
        r = http.get(f"{SHEETS_API}/{sheet_id}/values:batchGet", params=[("ranges", f"'{t}'") for t in chunk] + [("valueRenderOption", "UNFORMATTED_VALUE"), ("dateTimeRenderOption", "FORMATTED_STRING")],
                     headers=h, timeout=120)
        if r.status_code != 200:
            raise OAuthError(f"Sheets API values: HTTP {r.status_code}")
        for t, vr in zip(chunk, r.json().get("valueRanges", [])):
            df = _frame(vr.get("values") or [])
            if not df.empty:
                tabs[t] = normalize_columns(df)
    if not tabs:
        raise OAuthError("Spreadsheet mein koi data wali tab nahi mili.")
    return {"kind": "workbook", "name": name or m.get("properties", {}).get("title") or "Google Sheet", "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit", "tabs": tabs}
