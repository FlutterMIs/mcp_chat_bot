"""Chats page: previous conversations — open, rename, delete."""
from __future__ import annotations

import streamlit as st

from backend import conversations
from ui import shell


def page():
    shell.styles()
    user, workspace_id = shell.current()
    st.markdown("### Chats")
    if st.button("＋ New chat", type="primary"):
        shell.new_chat()
        st.switch_page(st.session_state["_chat_page"])
    rows = conversations.list_conversations(workspace_id, limit=100, channel=None, include_archived=False)
    if not rows:
        st.info("Abhi koi chat nahi. Chat page par sawal pucho — har chat yahan save hogi aur kisi bhi device se khul jayegi.")
        return
    for c in rows:
        with st.container(border=True):
            left, right = st.columns([4, 2])
            with left:
                when = c["last_message_at"].strftime("%d %b %Y, %H:%M") if c["last_message_at"] else "—"
                tag = " · 📱 WhatsApp" if c["channel"] == "whatsapp" else ""
                st.markdown(f"**{c['title']}**  \n<span class='muted'>{when}{tag}</span>", unsafe_allow_html=True)
            with right:
                b1, b2, b3 = st.columns(3)
                if b1.button("Open", key=f"open_{c['id']}", width="stretch"):
                    shell.open_conversation(c["id"])
                    st.switch_page(st.session_state["_chat_page"])
                if b2.button("Rename", key=f"ren_{c['id']}", width="stretch"):
                    st.session_state[f"renaming_{c['id']}"] = True
                if b3.button("Delete", key=f"del_{c['id']}", width="stretch"):
                    st.session_state[f"deleting_{c['id']}"] = True
            if st.session_state.get(f"renaming_{c['id']}"):
                new = st.text_input("New title", value=c["title"], key=f"title_{c['id']}")
                if st.button("Save", key=f"save_{c['id']}"):
                    conversations.rename(workspace_id, c["id"], new)
                    st.session_state.pop(f"renaming_{c['id']}", None)
                    st.rerun()
            if st.session_state.get(f"deleting_{c['id']}"):
                st.warning("Ye chat aur uske results permanently delete ho jayenge.")
                y, n = st.columns(2)
                if y.button("Yes, delete", key=f"yes_{c['id']}", type="primary"):
                    conversations.delete_conversation(workspace_id, c["id"])
                    if st.session_state.get("conversation_id") == c["id"]:
                        shell.new_chat()
                    st.session_state.pop(f"deleting_{c['id']}", None)
                    st.rerun()
                if n.button("Cancel", key=f"no_{c['id']}"):
                    st.session_state.pop(f"deleting_{c['id']}", None)
                    st.rerun()
