# V7 Proposal — Persistent, multi-device, ChatGPT-like Business Analyst

Date: 2026-09-27 · Derived from the current code (commit `bd8d35e`) and `audit/*.md`. **Nothing here is implemented yet.**
Purpose: the architecture and database model to approve before work starts, plus the order of work.

---

## 1. Where the project stands against the master prompt

| Master-prompt area | Today | Verdict |
|---|---|---|
| Two layers: LLM = meaning, code = truth | `understanding.py`/`analysis.py` (deterministic), `agent.py` (bounded, results in Python), `grounding.py`, `semantics.validate_plan` | **Done** (Phases 0–2 of the old plan) |
| Central date engine | `periods.py` (2026-09-27): one resolver, planner/agent dates overridden, compound asks | **Done**; add `this week`, `last week`, `tomorrow`, `FY`, `last 30 days` rolling, timezone |
| Metric engine | `semantics.requested_measures` + `resolve_measures` (items = SUM qty, customers = COUNT_DISTINCT, amount = SUM) | **Done**; expose as `metrics.py` facade, add transaction count |
| Follow-up transformations | collapse / additive / correction / drill / top-N / group-by / chart-type in `understanding.understand` | **Done** except FILTER by value and COMPARE as state transforms (planner still handles them) |
| Compound questions, reconciliation | `periods.asks`, `analysis.reconcile`, `Conversation.totals` | **Done** |
| Response policy, one render | `response.finalize` → `FinalResponse`; debug only with toggle | **Done**; add `tables/charts` plural, `date_info`, `drilldown_options` |
| Bounded agent, MCP internal | `agent.py` (8 steps / 10 calls / 45 s) — master asks 6 / 8 | **Adjust constants** |
| Token control | `openrouter.compact_result`, `compact_schema`, `max_tokens`, `llm_usage.jsonl` | **Done** |
| **Authentication, users, workspaces** | none. Web = anonymous browser session; WhatsApp = phone number | **Missing** |
| **Persistent sources (multi-device, survive restart)** | web: `st.session_state.sources` (lost on refresh); WhatsApp: `.env` defaults + per-chat file list in SQLite; registry = in-memory `MCPServer.sources` dict | **Missing** |
| **Private Google Sheets (OAuth)** | public XLSX export only (`mcp_server.load_google_workbook`) | **Missing** |
| **Persistent conversations / chat history** | web: session only; WhatsApp: one blob per chat (`whatsapp/store.py`) | **Missing** |
| Source manager UI, refresh, freshness | sidebar add-only; WhatsApp TTL re-download (300 s) | **Missing** |
| Per-workspace learning | `memory.py` scoped per *source id* in one global JSON file (B6/B7 fixed, but still one file for everyone) | **Move to DB, scope by workspace** |
| Website ingestion (clean → store → retrieve) | `read_web` text + tables + images/videos; `search_text` keyword retrieval, 18k cap | **Partly**; persist normalized content, add refresh + freshness |
| Mobile-friendly ChatGPT-style UI | Streamlit `layout="wide"`, sidebar config, cards row | **Rework** with `st.navigation`, narrow layout, CSS |
| Observability | WhatsApp JSON logs, `llm_usage.jsonl`, Streamlit status steps | **Add** request_id / conversation_id / timings to one `events` log |
| Security | key not in browser, 127.0.0.1 bind, SSRF guard, path allowlist | **Add** credential encryption, per-workspace authorization on every tool call |

Conclusion: the analytical core is in place. The missing layer is **identity + persistence**: a server-side database that owns users, workspaces, sources (with credentials), conversations and results, and both channels reading from it.

---

## 2. Target architecture

```
Browser (Streamlit, st.navigation: Chat · Chats · Data Sources · Settings)        WhatsApp (OpenWA webhook → whatsapp/bot.py)
      │  st.login (Google OIDC) or email+password (fallback)                             │  phone number → linked user/workspace (invite code)
      ▼                                                                                  ▼
 ┌──────────────────────────────── platform/ (new package, no Streamlit imports) ─────────────────────────────────┐
 │ auth.py          current_user(), sessions, password hashing (bcrypt), OIDC identity → users row                 │
 │ workspaces.py    workspace + members + authorization: require(workspace_id, user_id)                            │
 │ sources.py       SourceRegistry: CRUD, status, refresh, schema_snapshot; SourceLoader → parsed data (cached)     │
 │ credentials.py   Fernet-encrypted secrets (APP_SECRET_KEY): Google refresh tokens, DB URLs                       │
 │ google_oauth.py  OAuth 2.0 (drive.readonly + spreadsheets.readonly): start URL, callback, token refresh          │
 │ conversations.py conversations, messages, analytical_context, results (Conversation dataclass ⇄ rows)           │
 │ learning.py      per-workspace+source mappings/rules/settled choices (replaces workspace_memory.json)            │
 │ db.py            SQLAlchemy 2 models + engine (SQLite dev / PostgreSQL prod) + versioned migrations             │
 │ events.py        structured event log: request_id, conversation_id, source_id, step, ms, error (no secrets)      │
 └────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
      │ schemas + parsed data for the workspace's sources (loaded once per process, refreshed by TTL / Refresh)
      ▼
 analyst.answer(conv, question, tools)   ← unchanged contract; Conversation now hydrated from the DB
      understanding → periods → semantics/metrics → (state executor | multi-metric | agent | planner)
      → MCPServer tools (in-process or MCP stdio; sources registered from the registry, ids = source rows)
      → grounding / reconcile → response.finalize → FinalResponse
      ▼
 channel renders FinalResponse only; platform.conversations saves message + result + context
```

Key rules:
- `analyst.py`, `agent.py`, `analysis.py`, `understanding.py`, `periods.py`, `semantics.py`, `response.py`, `mcp_server.py` keep their contracts. The channels stop owning state.
- The **source id is the database row id** (`src_<uuid>`), the same in web, WhatsApp and the MCP stdio server, so learned mappings, results and cached data all key on it.
- `MCPServer.sources` stays as the **per-process cache**, filled from `platform.sources` (`load(source_row)`), invalidated by `sources.version` (bumped on refresh/settings change).
- Every tool call carries `workspace_id`; `platform.workspaces.require()` checks the source belongs to it before `MCPServer` is touched (T: user A cannot reach user B's sheet).

---

## 3. Database model (SQLAlchemy 2, PostgreSQL in production, SQLite for dev/tests)

All ids are strings (`usr_…`, `ws_…`, `src_…`, `conv_…`, `msg_…`, `res_…`), timestamps UTC. `created_at/updated_at` on every table.

| Table | Columns (key ones) | Notes |
|---|---|---|
| `users` | id, email (unique), name, auth_provider (`google_oidc` \| `password`), oidc_subject, password_hash, is_active, last_login_at | one row per person |
| `workspaces` | id, name, owner_user_id, default_timezone (`Asia/Kolkata`), settings JSON | a personal workspace is created at first login |
| `workspace_members` | workspace_id, user_id, role (`owner` \| `member`), invited_by | authorization table |
| `channel_identities` | id, workspace_id, user_id, channel (`whatsapp`), external_id (phone, hashed + last4), linked_at | WhatsApp number → workspace; replaces `WHATSAPP_ALLOWED_NUMBERS` for linked users |
| `sources` | id, workspace_id, name, type (`google_sheet` \| `website` \| `file` \| `database`), provider, connection_config JSON (sheet id/url, website url + crawl_config, file path/key, db host — never the password), credential_id (nullable), auth_mode (`public` \| `oauth` \| `secret`), status (`connected` \| `error` \| `syncing` \| `disabled`), version, last_synced_at, last_error, schema_snapshot JSON, metadata JSON, created_by | **the permanent source registry** |
| `source_credentials` | id, workspace_id, kind (`google_oauth` \| `db_url`), encrypted_blob, google_account_email, scopes, expires_at, rotated_at | Fernet with `APP_SECRET_KEY`; never returned to the browser |
| `source_syncs` | id, source_id, started_at, finished_at, status, rows, tables, error, content_hash | history of fetches; freshness = last successful sync |
| `source_files` | id, source_id, workspace_id, filename, mime, size, storage_path (under `DATA_DIR/<workspace>/`), sha256 | uploaded Excel/CSV/PDF/… persisted server-side |
| `web_documents` | id, source_id, url, title, fetched_at, content_hash, text (cleaned), tables JSON, images JSON, videos JSON | normalized website content; `search_text` reads this, not a re-fetch |
| `source_mappings` | id, workspace_id, source_id, sheet, term, column, kind (`measure` \| `date_column` \| `dimension` \| `choice`), confirmed_by, confirmed_at | replaces `memory.mappings` + settled choices |
| `source_rules` | id, workspace_id, source_id, text, confirmed_by, confirmed_at, active | replaces `memory.rules` (confirmed only) |
| `conversations` | id, workspace_id, created_by, title, channel (`web` \| `whatsapp`), source_ids JSON, analytical_context JSON (= `Conversation.state`, `last_plan`, `recent_plans`, `totals`, `pending_choice`, `focus`), last_message_at, archived | one row per chat |
| `messages` | id, conversation_id, role, content, intent JSON, resolved_entities JSON, source_context JSON, result_id (nullable), final_response JSON (shape, metrics, chart spec, table ref), audio_ref, created_at | what the UI restores |
| `results` | id, conversation_id, workspace_id, source_id, metric, aggregation, dimensions JSON, filters JSON, period JSON (from `periods.py`), calculation, rows_ref (parquet/CSV path or inline JSON ≤ 200 rows), row_count, value, created_at | the result store; follow-ups reference it, exports read it |
| `user_preferences` | user_id, workspace_id, language, number_style, tts JSON, debug_mode, default_chart | sidebar settings that survive devices |
| `events` | id, request_id, conversation_id, workspace_id, source_id, name, ms, payload JSON (redacted), created_at | observability; optional table, else JSONL |

Migration from today:
- `workspace_memory.json` → `source_mappings` / `source_rules` for a bootstrap workspace (one-off script, keeps per-source scoping; legacy global rules dropped as already decided).
- `whatsapp_state.db.conversations` → `conversations` with `channel="whatsapp"` and `channel_identities` rows for the allowlisted numbers (they map to the bootstrap workspace).
- `.env` `WHATSAPP_GOOGLE_SHEET_URL` / `WHATSAPP_DATA_FILES` → seeded `sources` rows in the bootstrap workspace (no more env-only sources).

---

## 4. Google Sheets: public and authenticated

| Mode | How | Stored |
|---|---|---|
| `public` | current XLSX export (`export?format=xlsx`) | `connection_config.url`, sheet id |
| `oauth` | Google OAuth 2.0 code flow with `https://www.googleapis.com/auth/spreadsheets.readonly` + `drive.readonly` (for the picker/list); refresh token encrypted in `source_credentials`; data read via Sheets API `spreadsheets.values.batchGet` for every tab → same `tabs: {name: DataFrame}` shape as today | credential row per Google account, reused by many sheets |

Flow: Data Sources → "Connect Google account" → redirect to Google → callback endpoint (`/oauth/google/callback`, served by a small Starlette app on the same host as `whatsapp_server.py`, or Streamlit page reading `?code=` with state check) → token exchanged server-side → user picks a spreadsheet (list via Drive API, or pastes a URL) → `sources` row. The browser never sees tokens. Identity login (`st.login`) and Sheets authorization are separate: login proves who you are; Sheets consent grants data access.

Requires from the owner: a Google Cloud project with OAuth client id/secret and the redirect URI; `.env`: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `APP_BASE_URL`, `APP_SECRET_KEY`.

---

## 5. UI (Streamlit, kept — no framework switch)

- `st.navigation`: **Chat** (default), **Chats** (history list: title, last message, open/rename/delete), **Data Sources** (cards: name, type, status dot, last synced; actions Refresh · Rename · Settings · Remove; "Add source" wizard: Google Sheet / Website / File / Database), **Settings** (language, number style, TTS, debug).
- Login gate on every page: `st.login()` (Google OIDC via `secrets.toml [auth]`) with email+password fallback for local/dev.
- Chat page: `layout="centered"`, messages only; scalar → one line + one metric; series → chart + collapsible table; actions row (View details · Show table · Show chart · Export) instead of cards/tables by default. Chart-type menu stays under "Show chart".
- Mobile: single column, `use_container_width` charts, `st.dataframe` scrolls horizontally, cards stack (CSS breakpoint ≤ 640 px), no wide sidebar config (moved to Settings).
- WhatsApp: unchanged UX; `/link <code>` links a phone to a workspace; sources come from the registry.

---

## 6. Phased plan (each phase ships green tests; existing 243 tests stay green)

| Phase | Scope | Acceptance tests (master §39) |
|---|---|---|
| **P1 Platform + DB** | `platform/db.py` models + migrations, `auth.py` (st.login + password), `workspaces.py`, `credentials.py`, bootstrap workspace migration script | D, E, F (unchanged core), T (authz unit test), S (credential never in FinalResponse/session) |
| **P2 Source registry** | `sources.py` (CRUD, load, cache, refresh, syncs, freshness), Data Sources page, files persisted under `DATA_DIR/<ws>/`, website `web_documents`, WhatsApp reads registry | A (public sheet), B (website survives restart), C (second browser), R (two similar sources → clarify: already `_likely_table` rivals) |
| **P3 Conversations** | `conversations.py`, Chats page, `Conversation` ⇄ rows, results table + export from result, WhatsApp store → DB | K, L, M, N, O, P, Q (existing tests re-pointed at DB-backed conv), acceptance §43 device-2 continuation |
| **P4 Google OAuth** | `google_oauth.py`, callback endpoint, Sheets API loader, account picker | S (server-side tokens), private sheet e2e (manual, needs the owner's Google project) |
| **P5 UI polish + mobile** | centered chat, actions row, responsive CSS, Settings page, preferences persisted | P, Q visual checks via `AppTest`; manual phone check |
| **P6 Hardening** | agent limits 6/8, `events` log, per-workspace cache keys, website refresh + freshness lines, `FILTER`/`COMPARE` as state transforms, security review, token review | I–J regression, security checklist |

Order rationale: persistence first (largest gap, everything else keys on source/conversation ids), OAuth after the registry exists, UI last because it only renders what P1–P4 store.

---

## 7. Issue matrix (new gaps only; analytical bugs are in `WRONG_ANSWER_ROOT_CAUSES.md` and fixed)

| Issue | Current file / function | Root cause | Fix | Test |
|---|---|---|---|---|
| Sources vanish on refresh/restart | `app.py` sidebar → `st.session_state.sources`; `MCPServer.sources` dict | registry is process memory | `sources` table + `SourceRegistry.load()` cache | A, B, C |
| Same sheet re-added every time; WhatsApp sources only via `.env` | `whatsapp/bot.py Sources.defaults` | config, not data | seeded rows + Data Sources page | A |
| No identity, no isolation | none | Streamlit anonymous | `st.login` + `users/workspaces/members`, `require()` on every tool call | T |
| Private sheets impossible | `mcp_server.load_google_workbook` (public export) | no OAuth | `google_oauth.py` + Sheets API loader | S |
| Chats lost on refresh; WhatsApp blob per chat | `st.session_state.messages`; `whatsapp/store.py` | no conversation model | `conversations/messages/results` | §43 |
| Learning in one global JSON | `memory.py MEMORY_FILE` | file store | `source_mappings/rules` per workspace | P0-9 re-run |
| Stale website answers, no freshness | `mcp_server.register_web` re-fetch only on register | no sync record | `web_documents` + `source_syncs`, "last fetched" line | B + freshness unit |
| Files depend on Streamlit upload objects | `app.py` uploads → `read_file` bytes in session | not stored | `source_files` under `DATA_DIR/<ws>/` | A (file variant) |
| Dashboard-like default render | `app.py render_assistant` (cards + table + chart + downloads) | everything shown | actions row, collapsibles, policy-driven | P, Q |
| Agent limits 8/10 vs 6/8 | `agent.py` constants | spec change | constants + test P1-2 | P1-2 |

---

## 8. Decisions needed before P1 starts

1. **Database:** SQLite file for dev/tests, PostgreSQL URL for production via `APP_DATABASE_URL` (SQLAlchemy, same models). OK?
2. **Login:** Google sign-in through Streamlit `st.login` (needs the same Google Cloud project as OAuth) **plus** email+password fallback. Or password-only to start?
3. **Google Cloud project:** you create it (OAuth client id/secret, redirect URI `https://<host>/oauth/google/callback`); I wire it. Until then, public sheets keep working.
4. **UI stays Streamlit** (reworked with `st.navigation`, centered chat, mobile CSS). A React front-end is out of scope for this pass.
5. **Hosting:** Streamlit Community Cloud cannot run the WhatsApp/OAuth callback server or keep a local SQLite safely; production needs one VPS/host running Streamlit + the Starlette server + PostgreSQL. Confirm the target.
6. **WhatsApp identity:** phone numbers link to a workspace with a one-time `/link <code>`; the `.env` allowlist stays as a fallback. OK?
7. **Repo:** work continues in `Desktop/Md Ashraf/md_ashraf/mcp_chat_bot` (GitHub `FlutterMIs/mcp_chat_bot`); the `Downloads` copy is retired.
