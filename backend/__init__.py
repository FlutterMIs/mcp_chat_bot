"""Server-side platform layer: users, workspaces, sources, credentials, conversations, learning, events.

Nothing in this package imports Streamlit or WhatsApp code. Both channels call it; `analyst.py` does not know it exists
(it still receives a `Conversation` and a `Tools` object). Storage is SQLAlchemy 2 — SQLite for development and tests,
PostgreSQL in production (`APP_DATABASE_URL`).
"""
