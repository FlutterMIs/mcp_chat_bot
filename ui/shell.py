"""Login gate, current user/workspace, sidebar (new chat + recent chats), shared styles."""
from __future__ import annotations

import os

import streamlit as st

from backend import auth, conversations, db
from backend.sources import SourceRegistry

CSS = """
<style>
/* ChatGPT-like: narrow column, readable on phones, no horizontal overflow */
.block-container {max-width: 880px; padding-top: 1.2rem; padding-bottom: 5rem;}
@media (max-width: 640px) {
  .block-container {padding-left: 0.8rem; padding-right: 0.8rem; padding-top: 0.6rem;}
  [data-testid="stMetric"] {padding: 0.4rem 0.6rem;}
  [data-testid="stHorizontalBlock"] {flex-wrap: wrap;}
  [data-testid="stHorizontalBlock"] > div {min-width: 45% !important;}
}
[data-testid="stMetric"] {background: rgba(128,128,128,0.08); border-radius: 12px; padding: 0.6rem 0.9rem;}
[data-testid="stMetricValue"] {font-size: 1.5rem;}
[data-testid="stChatMessage"] {padding: 0.6rem 0.8rem;}
.src-card {border: 1px solid rgba(128,128,128,0.25); border-radius: 12px; padding: 0.8rem 1rem; margin-bottom: 0.6rem;}
.src-dot {display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px;}
.muted {opacity: 0.65; font-size: 0.85rem;}
div[data-testid="stDataFrame"] {overflow-x: auto;}
</style>
"""

REGISTRY = SourceRegistry()


def styles():
    st.markdown(CSS, unsafe_allow_html=True)


def _set_user(u):
    st.session_state.auth_user = u
    st.session_state.workspace_id = u["workspace_id"]


def require_login() -> dict:
    """Returns {"user_id","workspace_id","email","name"} or renders the login screen and stops the script."""
    db.engine()
    if st.session_state.get("auth_user"):
        return st.session_state.auth_user
    mode = auth.auth_mode()
    if mode == "none":
        u = auth.dev_user()
        _set_user(u)
        return u
    if mode == "oidc":
        user = getattr(st, "user", None)
        if user is not None and getattr(user, "is_logged_in", False):
            u = auth.login_oidc(user.email, getattr(user, "name", None), getattr(user, "sub", None))
            _set_user(u)
            return u
        st.markdown("## 🧠 Business Analyst")
        st.write("Apne data se baat karne ke liye sign in karo.")
        if st.button("Sign in with Google", type="primary", width="stretch"):
            st.login()
        st.stop()
    # password mode
    st.markdown("## 🧠 Business Analyst")
    tab_login, tab_register = st.tabs(["Login", "Create account"])
    with tab_login:
        with st.form("login"):
            email = st.text_input("Email")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Login", type="primary", width="stretch"):
                try:
                    _set_user(auth.login_password(email, pw))
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    with tab_register:
        with st.form("register"):
            name = st.text_input("Name")
            email = st.text_input("Email", key="reg_email")
            pw = st.text_input("Password (8+ chars)", type="password", key="reg_pw")
            if st.form_submit_button("Create account", width="stretch"):
                try:
                    _set_user(auth.register(email, pw, name))
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    st.stop()


def logout():
    for k in ("auth_user", "workspace_id", "conversation_id", "messages", "pending_q", "pending_choice"):
        st.session_state.pop(k, None)
    if auth.auth_mode() == "oidc":
        try:
            st.logout()
        except Exception:
            pass
    st.rerun()


def current():
    u = st.session_state.auth_user
    return u, u["workspace_id"]


def open_conversation(conversation_id):
    st.session_state.conversation_id = conversation_id
    st.session_state.messages = None          # reloaded from the database by the chat page
    st.session_state.pending_q = None


def new_chat():
    st.session_state.conversation_id = None
    st.session_state.messages = []
    st.session_state.pending_q = None


def sidebar(user, workspace_id):
    with st.sidebar:
        st.markdown(f"**🧠 Business Analyst**  \n<span class='muted'>{user.get('name') or user['email']}</span>", unsafe_allow_html=True)
        if st.button("＋ New chat", width="stretch", type="primary"):
            new_chat()
            st.switch_page(st.session_state.get("_chat_page")) if st.session_state.get("_chat_page") else st.rerun()
        recent = conversations.list_conversations(workspace_id, limit=12, channel="web")
        if recent:
            st.caption("Recent chats")
            for c in recent:
                label = ("▸ " if c["id"] == st.session_state.get("conversation_id") else "") + c["title"]
                if st.button(label, key=f"side_{c['id']}", width="stretch"):
                    open_conversation(c["id"])
                    st.switch_page(st.session_state.get("_chat_page")) if st.session_state.get("_chat_page") else st.rerun()
        st.divider()
        srcs = REGISTRY.list(workspace_id, include_disabled=False)
        ok = [s for s in srcs if s["status"] == "connected"]
        st.caption(f"Data sources: {len(ok)} connected" + (f", {len(srcs) - len(ok)} with issues" if len(srcs) != len(ok) else ""))
        if st.button("Logout", width="stretch"):
            logout()
