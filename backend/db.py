"""Database models and engine. One place for every persistent entity (see audit/V7_ARCHITECTURE_PROPOSAL.md §3).

    APP_DATABASE_URL   sqlite:///app_data/app.db (default) | postgresql+psycopg://user:pass@host/db
    init_db()          creates tables and applies versioned migrations (idempotent)
    session_scope()    `with session_scope() as s:` — commit on success, rollback on error
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc)


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Stamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now, nullable=False)


class User(Base, Stamped):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    auth_provider: Mapped[str] = mapped_column(String(40), default="password")      # password | google_oidc
    oidc_subject: Mapped[str | None] = mapped_column(String(200), index=True)
    password_hash: Mapped[str | None] = mapped_column(String(400))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(Base, Stamped):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ws"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    default_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class WorkspaceMember(Base, Stamped):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="member")                 # owner | member
    invited_by: Mapped[str | None] = mapped_column(String(40))


class ChannelIdentity(Base, Stamped):
    """A phone number (WhatsApp) or other external id linked to a workspace. The number is stored hashed."""
    __tablename__ = "channel_identities"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cid"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)                 # whatsapp
    external_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_last4: Mapped[str] = mapped_column(String(8), default="")
    link_code: Mapped[str | None] = mapped_column(String(16), index=True)            # pending invite code, cleared on link
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(Base, Stamped):
    """The permanent source registry. `connection_config` never holds a secret — those live in source_credentials."""
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("src"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)                    # google_sheet | website | file | database
    provider: Mapped[str | None] = mapped_column(String(40))                         # google | http | upload | postgresql | mysql | sqlite
    auth_mode: Mapped[str] = mapped_column(String(20), default="public")             # public | oauth | secret
    connection_config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("source_credentials.id"))
    status: Mapped[str] = mapped_column(String(20), default="syncing")               # connected | error | syncing | disabled
    version: Mapped[int] = mapped_column(Integer, default=1)                          # bumped on refresh / settings change → caches reload
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    schema_snapshot: Mapped[dict | None] = mapped_column(JSON)
    meta: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(40))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class SourceCredential(Base, Stamped):
    __tablename__ = "source_credentials"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cred"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)                    # google_oauth | db_url
    encrypted_blob: Mapped[str] = mapped_column(Text, nullable=False)
    google_account_email: Mapped[str | None] = mapped_column(String(320))
    scopes: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceSync(Base):
    __tablename__ = "source_syncs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sync"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")               # running | ok | error
    rows: Mapped[int | None] = mapped_column(Integer)
    tables: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))


class SourceFile(Base, Stamped):
    __tablename__ = "source_files"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("file"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))


class WebDocument(Base, Stamped):
    __tablename__ = "web_documents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("web"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    tables: Mapped[list | None] = mapped_column(JSON)
    images: Mapped[list | None] = mapped_column(JSON)
    videos: Mapped[list | None] = mapped_column(JSON)


class SourceMapping(Base, Stamped):
    """A confirmed meaning for one source: term → column (measure / date_column / dimension / settled choice)."""
    __tablename__ = "source_mappings"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("map"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    sheet: Mapped[str | None] = mapped_column(String(200))
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    column: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="choice")
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)


class SourceRule(Base, Stamped):
    __tablename__ = "source_rules"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rule"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(40))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ValidatedPlan(Base, Stamped):
    """Planner examples: only plans that passed validation unchanged, ≤ 5 per source (memory.PLANS_PER_SOURCE)."""
    __tablename__ = "validated_plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("plan"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict] = mapped_column(JSON, nullable=False)


class Conversation(Base, Stamped):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("conv"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    channel: Mapped[str] = mapped_column(String(20), default="web")                  # web | whatsapp
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    analytical_context: Mapped[dict] = mapped_column(JSON, default=dict)             # analyst.Conversation fields (state, last_plan, totals…)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("msg"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)                    # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[dict | None] = mapped_column(JSON)
    resolved_entities: Mapped[dict | None] = mapped_column(JSON)
    source_context: Mapped[dict | None] = mapped_column(JSON)
    result_id: Mapped[str | None] = mapped_column(String(40), index=True)
    final_response: Mapped[dict | None] = mapped_column(JSON)                        # what the UI renders (no DataFrame; rows live in results)
    audio_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class Result(Base):
    """One completed analysis. Rows are stored inline (capped); `value` for scalars. Follow-ups and exports read this."""
    __tablename__ = "results"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("res"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(String(40))
    metric: Mapped[str | None] = mapped_column(String(200))
    aggregation: Mapped[str | None] = mapped_column(String(40))
    dimensions: Mapped[list | None] = mapped_column(JSON)
    filters: Mapped[list | None] = mapped_column(JSON)
    period: Mapped[dict | None] = mapped_column(JSON)
    calculation: Mapped[str | None] = mapped_column(Text)
    columns: Mapped[list | None] = mapped_column(JSON)
    rows: Mapped[list | None] = mapped_column(JSON)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class UserPreference(Base, Stamped):
    __tablename__ = "user_preferences"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)                          # language, number_style, tts, debug_mode, default_chart


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str | None] = mapped_column(String(40), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(40), index=True)
    source_id: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    ms: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


Index("ix_sources_ws_type", Source.workspace_id, Source.type)
Index("ix_conversations_ws_last", Conversation.workspace_id, Conversation.last_message_at)

# ---------------------------------------------------------------- engine / sessions

_engine = None
_Session: sessionmaker | None = None
_lock = threading.Lock()


def database_url():
    url = os.getenv("APP_DATABASE_URL", "").strip()
    if url:
        return url
    root = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent.parent / "app_data")
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{root / 'app.db'}"


def configure(url=None, echo=False):
    """(Re)point the module at a database. Tests call configure('sqlite://') for an in-memory DB."""
    global _engine, _Session
    with _lock:
        url = url or database_url()
        kw = {"future": True, "echo": echo}
        if url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool
            kw["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kw["poolclass"] = StaticPool
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _fk_on(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
        _Session = sessionmaker(_engine, expire_on_commit=False, class_=Session)
        init_db()
        return _engine


def engine():
    if _engine is None:
        configure()
    return _engine


@contextlib.contextmanager
def session_scope():
    if _Session is None:
        configure()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------- migrations

MIGRATIONS = {
    # version: callable(connection). Version 1 = create_all (below). Later versions add ALTERs here.
}


def init_db():
    eng = _engine
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        applied = {r[0] for r in conn.execute(select(SchemaMigration.version)).all()}
        for v in sorted(set(MIGRATIONS) | {1}):
            if v in applied or v > SCHEMA_VERSION:
                continue
            if v in MIGRATIONS:
                MIGRATIONS[v](conn)
            conn.execute(text("INSERT INTO schema_migrations(version, applied_at) VALUES (:v, :t)"), {"v": v, "t": now()})


def to_dict(row):
    return {c.name: getattr(row, c.key if hasattr(row, c.key) else c.name) for c in row.__table__.columns} if row is not None else None


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)
