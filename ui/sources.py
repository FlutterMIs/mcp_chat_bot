"""Data Sources page: saved sources of the workspace (name, type, status, freshness, actions) and the Add-source wizard."""
from __future__ import annotations

import os

import streamlit as st

from backend import google_oauth, learning
from backend.sources import SourceError
from ui import shell

ICON = {"google_sheet": "📊", "website": "🌐", "file": "📁", "database": "🗄️"}
LABEL = {"google_sheet": "Google Sheet", "website": "Website", "file": "File", "database": "Database"}
DOT = {"connected": "#22c55e", "error": "#ef4444", "syncing": "#f59e0b", "disabled": "#9ca3af"}


def _card(workspace_id, s):
    reg = shell.REGISTRY
    with st.container(border=True):
        top, act = st.columns([5, 3])
        with top:
            auth = " · 🔒 private (OAuth)" if s["auth_mode"] == "oauth" else ""
            st.markdown(f"{ICON.get(s['type'], '•')} **{s['name']}**  \n<span class='muted'>{LABEL.get(s['type'], s['type'])}{auth}</span>", unsafe_allow_html=True)
            st.markdown(f"<span class='src-dot' style='background:{DOT.get(s['status'], '#9ca3af')}'></span>{s['status'].title()} · "
                        f"<span class='muted'>last synced {reg.freshness(s)}</span>", unsafe_allow_html=True)
            if s["last_error"]:
                st.caption(f"⚠️ {s['last_error'][:200]}")
            snap = s.get("schema_snapshot") or {}
            tabs = snap.get("sheets") or []
            if tabs:
                st.caption(" · ".join(f"{t['name']} ({t.get('row_count', 0):,} rows)" if t.get("name") else f"{t.get('row_count', 0):,} rows" for t in tabs[:6]))
            elif snap.get("text_length"):
                st.caption(f"{snap['text_length']:,} characters of text" + (f", {snap.get('image_count')} images" if snap.get("image_count") else ""))
        with act:
            b1, b2 = st.columns(2)
            if b1.button("Refresh", key=f"rf_{s['id']}", width="stretch"):
                with st.spinner("Refreshing…"):
                    reg.refresh(workspace_id, s["id"])
                st.rerun()
            if b2.button("Remove", key=f"rm_{s['id']}", width="stretch"):
                st.session_state[f"removing_{s['id']}"] = True
            b3, b4 = st.columns(2)
            if b3.button("Rename", key=f"rn_{s['id']}", width="stretch"):
                st.session_state[f"renaming_{s['id']}"] = True
            if b4.button("Settings", key=f"st_{s['id']}", width="stretch"):
                st.session_state[f"settings_{s['id']}"] = not st.session_state.get(f"settings_{s['id']}")
        if st.session_state.get(f"renaming_{s['id']}"):
            new = st.text_input("Name", value=s["name"], key=f"name_{s['id']}")
            if st.button("Save name", key=f"sv_{s['id']}"):
                reg.rename(workspace_id, s["id"], new)
                st.session_state.pop(f"renaming_{s['id']}", None)
                st.rerun()
        if st.session_state.get(f"removing_{s['id']}"):
            st.warning("Source hat jayega aur iske liye seekhi hui mappings bhi. Purani chats rahengi.")
            y, n = st.columns(2)
            if y.button("Yes, remove", key=f"yr_{s['id']}", type="primary"):
                reg.remove(workspace_id, s["id"])
                st.session_state.pop(f"removing_{s['id']}", None)
                st.rerun()
            if n.button("Cancel", key=f"nr_{s['id']}"):
                st.session_state.pop(f"removing_{s['id']}", None)
                st.rerun()
        if st.session_state.get(f"settings_{s['id']}"):
            cfg = s["connection_config"]
            if cfg.get("url"):
                st.caption(f"URL: {cfg['url']}")
            enabled = s["status"] != "disabled"
            if st.toggle("Use this source in answers", value=enabled, key=f"en_{s['id']}") != enabled:
                reg.set_enabled(workspace_id, s["id"], not enabled)
                st.rerun()
            learned = learning.list_learned(workspace_id, s["id"])
            if learned["mappings"] or learned["rules"]:
                st.caption("What the assistant remembers for this source:")
                for m in learned["mappings"]:
                    c1, c2 = st.columns([5, 1])
                    c1.write(f"• {m['term']} → **{m['column']}**" + (f" ({m['sheet']})" if m["sheet"] else ""))
                    if c2.button("✕", key=f"fm_{m['id']}"):
                        learning.forget_mapping(workspace_id, m["id"])
                        st.rerun()
                for r in learned["rules"]:
                    c1, c2 = st.columns([5, 1])
                    c1.write(f"• rule: {r['text']}")
                    if c2.button("✕", key=f"fr_{r['id']}"):
                        learning.forget_rule(workspace_id, r["id"])
                        st.rerun()
            else:
                st.caption("Nothing learned yet for this source.")


def _add_wizard(user, workspace_id):
    reg = shell.REGISTRY
    kind = st.segmented_control("Add source", ["Google Sheet", "Website", "File", "Database"], default="Google Sheet", key="add_kind")
    if kind == "Google Sheet":
        mode = "public"
        if google_oauth.configured():
            accounts = google_oauth.accounts(workspace_id)
            choices = ["Public link (viewable by anyone)"] + [f"Private — {a['google_account_email']}" for a in accounts] + ["Connect another Google account…"]
            pick = st.radio("Access", choices, key="gs_mode", label_visibility="collapsed")
            if pick.startswith("Connect"):
                st.link_button("Sign in with Google to allow read-only Sheets access", google_oauth.start_url(workspace_id, user["user_id"]), type="primary")
                st.caption("Google par consent dene ke baad ye page wapas khulega. Tokens server par encrypted rehte hain, browser ko kabhi nahi milte.")
                return
            if pick.startswith("Private"):
                mode = accounts[choices.index(pick) - 1]["id"]
        else:
            st.caption("Public sheet (viewable by link). Private sheets ke liye GOOGLE_OAUTH_CLIENT_ID/SECRET set karo — phir yahan 'Connect Google account' aayega.")
        url = st.text_input("Google Sheet URL", placeholder="https://docs.google.com/spreadsheets/d/…/edit", key="gs_url")
        name = st.text_input("Name (optional)", placeholder="Sales 2026", key="gs_name")
        if st.button("Save sheet", type="primary", disabled=not url.strip(), key="gs_save"):
            try:
                with st.spinner("Sheet padh raha hoon…"):
                    row = reg.add_google_sheet(workspace_id, user["user_id"], url.strip(), name.strip() or None, credential_id=None if mode == "public" else mode)
                (st.success if row["status"] == "connected" else st.error)(f"{row['name']}: {row['status']}" + (f" — {row['last_error']}" if row["last_error"] else ""))
                if row["status"] == "connected":
                    st.rerun()
            except SourceError as e:
                st.error(str(e))
    elif kind == "Website":
        url = st.text_input("Website URL", placeholder="https://example.com", key="web_url")
        name = st.text_input("Name (optional)", placeholder="Company website", key="web_name")
        if st.button("Save website", type="primary", disabled=not url.strip(), key="web_save"):
            try:
                with st.spinner("Page padh raha hoon…"):
                    row = reg.add_website(workspace_id, user["user_id"], url.strip(), name.strip() or None)
                (st.success if row["status"] == "connected" else st.error)(f"{row['name']}: {row['status']}" + (f" — {row['last_error']}" if row["last_error"] else ""))
                if row["status"] == "connected":
                    st.rerun()
            except SourceError as e:
                st.error(str(e))
    elif kind == "File":
        ups = st.file_uploader("Excel / CSV / PDF / Word / PPT / TXT / JSON", accept_multiple_files=True, key="file_up",
                               type=["csv", "xlsx", "xls", "pdf", "docx", "pptx", "txt", "md", "json", "html", "htm"])
        if ups and st.button("Save files", type="primary", key="file_save"):
            for up in ups:
                try:
                    row = reg.add_file(workspace_id, user["user_id"], up.name, up.getvalue(), mime=up.type)
                    (st.success if row["status"] == "connected" else st.error)(f"{row['name']}: {row['status']}" + (f" — {row['last_error']}" if row["last_error"] else ""))
                except Exception as e:
                    st.error(f"{up.name}: {e}")
            st.rerun()
    elif kind == "Database":
        url = st.text_input("Database URL", type="password", placeholder="postgresql://readonly:pass@host:5432/erp", key="db_url",
                            help="SQLAlchemy URL. Read with SELECT only; stored encrypted on the server, never shown again.")
        name = st.text_input("Name (optional)", key="db_name")
        if st.button("Save database", type="primary", disabled=not url.strip(), key="db_save"):
            try:
                with st.spinner("Tables padh raha hoon…"):
                    row = reg.add_database(workspace_id, user["user_id"], url.strip(), name.strip() or None)
                (st.success if row["status"] == "connected" else st.error)(f"{row['name']}: {row['status']}" + (f" — {row['last_error']}" if row["last_error"] else ""))
                if row["status"] == "connected":
                    st.rerun()
            except Exception as e:
                st.error(str(e).replace(url.strip(), "<database url>")[:300])


def page():
    shell.styles()
    user, workspace_id = shell.current()
    st.markdown("### Data Sources")
    # OAuth callback lands here with ?code=…&state=…
    qp = st.query_params
    if qp.get("code") and qp.get("state"):
        try:
            acc = google_oauth.finish(qp["code"], qp["state"], workspace_id, user["user_id"])
            st.success(f"Google account connected: {acc.get('google_account_email') or ''}. Ab niche private sheet add karo.")
        except Exception as e:
            st.error(f"Google connect nahi hua: {e}")
        st.query_params.clear()
    elif qp.get("error"):
        st.error(f"Google ne access deny kiya: {qp['error']}")
        st.query_params.clear()
    rows = shell.REGISTRY.list(workspace_id)
    if not rows:
        st.info("Ek baar source jodo — wo aapke account mein save rahega: laptop, phone, dusra browser, restart ke baad bhi.")
    for kind in ("google_sheet", "website", "file", "database"):
        group = [s for s in rows if s["type"] == kind]
        if group:
            st.markdown(f"**{ICON[kind]} {LABEL[kind]}s**")
            for s in group:
                _card(workspace_id, s)
    st.divider()
    _add_wizard(user, workspace_id)
