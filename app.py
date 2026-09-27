"""Business Analyst — web channel. Login → workspace → pages (Chat · Chats · Data Sources · Settings).

Everything the user adds or says is stored server-side (backend/*), so the same account sees the same sources and
chats on every device. This file only wires the pages; rendering lives in ui/*, analysis in analyst.py.
"""
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title="Business Analyst", page_icon="🧠", layout="centered", initial_sidebar_state="auto")

from ui import chat, chats, settings, shell, sources  # noqa: E402

user = shell.require_login()
workspace_id = user["workspace_id"]
settings.load_prefs(user, workspace_id)

chat_page = st.Page(chat.page, title="Chat", icon=":material/chat:", default=True, url_path="chat")
st.session_state["_chat_page"] = chat_page
nav = st.navigation([chat_page,
                     st.Page(chats.page, title="Chats", icon=":material/history:", url_path="chats"),
                     st.Page(sources.page, title="Data Sources", icon=":material/database:", url_path="sources"),
                     st.Page(settings.page, title="Settings", icon=":material/settings:", url_path="settings")])
shell.sidebar(user, workspace_id)
nav.run()
