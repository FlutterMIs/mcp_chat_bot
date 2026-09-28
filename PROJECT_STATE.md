# PROJECT_STATE.md — MCP Universal Business Analyst (V8)

Last updated: 2026-09-28. Architecture and every module's role are in `README.md` (this file lists what is not obvious there).

## 1. What it is
Streamlit web app + WhatsApp bot sharing one brain (`analyst.answer`). OpenRouter LLM reads language; MCP-style read-only
tools compute; every number is validated in code (`grounding.py`, `semantics.validate_plan`).

## 2. Files that matter most
| File | Role |
|---|---|
| `backend/` | V7 platform: `db.py` (SQLAlchemy models, SQLite/Postgres), `auth.py`, `workspaces.py` (authz), `sources.py` (permanent registry + process cache), `credentials.py` (Fernet), `google_oauth.py`, `conversations.py`, `learning.py`, `preferences.py`, `whatsapp_link.py`, `events.py` |
| `ui/` | Streamlit pages: `shell.py` (login, sidebar, CSS), `chat.py`, `chats.py`, `sources.py`, `settings.py`; `app.py` only wires `st.navigation` |
| `analyst.py` | routing: pre-routes → understanding/state path → multi-metric → agent → LLM planner; `sanitize_plan`, `seed_state` |
| `periods.py` | **the only** date resolver (`resolve_date_expression`, `enforce_period`) |
| `understanding.py` / `analysis.py` | intent + follow-up merge (collapse, additive, correction, drill) → deterministic executor, `reconcile` |
| `response.py` | `finalize` → one `FinalResponse` per message (shape: scalar/series/ranking/breakdown/detail/text), `display_table` |
| `agent.py`, `resultstore.py` | bounded tool loop for complex questions |
| `app.py`, `whatsapp/bot.py` | the two channels; both render only the `FinalResponse` |
| `prompt_builder.py` + `backend/prompt_settings.py` | V8 workspace AI Instructions (response_style, data_format, bot_personality, company_context) layered UNDER the core rules; stored in `Workspace.settings["ai_instructions"]`; UI in `ui/settings.py`; scope follows `memory.set_scope` |
| `entities.py` | safe fuzzy entity matching for filter values (exact → single close → ambiguous chips → none); `AmbiguousValue` is turned into a choice by `analyst` |
| `pivot.py` | cross-tab presentation of 2-key / 1-measure aggregates ("month wise sales by city", "sales person vs month"); `understanding.cross_tab` reads the two axes |
| `exports.py` | one deterministic `ResultExport` → CSV / XLSX / PDF (dependency-free PDF writer); UI has a PDF button |
| `result_check.py` | last gate in `analyst._done`: plan columns/sheet vs schema, ordered dates, finite numbers — else a clear error, never a figure |
| `rag/` | optional hybrid RAG (`RAG_ENABLED=false` default): chunking (PDF pages / sections) → embeddings (hash offline / openrouter / sentence_transformers) → BM25 + cosine (RRF) → top-k with citations; `mcp_server.search_text` uses it; `agent.classify` routes MCP+RAG hybrids to the agent |

## 3. Rules (do not break)
- Generic: no business column names hardcoded. Never guess a number; ask (chips) when ambiguous.
- Workspace AI instructions only shape context/presentation: `prompt_builder.validate` rejects override attempts; planner gets company_context only; wording models get style/format/personality; validation/grounding/tools run in code regardless.
- RAG never computes totals: data questions stay on the tools even with documents connected; `rag.hits_any` + `semantics.knowledge_words` decide a hybrid (agent) only when the non-data words hit a document.
- Fuzzy matching is for filter VALUES only (never metrics); a tie asks, a clear single match is used and reported in `resolved_filters`.
- Dates: only `periods.py`. "last month" = previous calendar month; "last N months" = N calendar months ending today.
- Follow-ups modify the previous state; "total kar ke batao" collapses, it never restarts.
- One UI render per message; debug material only with the sidebar toggle.
- Secrets never printed/committed (`.env` ignored). Credentials only via `backend.credentials` (encrypted); never in `sources.connection_config`, FinalResponse or logs.
- Every source/conversation read goes through `backend.workspaces.require*` (workspace isolation).
- `memory.set_scope(workspace_id)` before `analyst.answer` in every channel — learning is per workspace; JSON file only for tests / legacy.

## 4. Data / config
`.env`: OpenRouter key/model; V7: `APP_DATABASE_URL` (default SQLite `app_data/app.db`), `APP_SECRET_KEY`, `APP_AUTH_MODE`
(password | oidc | none), `APP_BASE_URL` + `GOOGLE_OAUTH_CLIENT_ID/SECRET` for private sheets, `DATA_DIR`.
Legacy `WHATSAPP_GOOGLE_SHEET_URL` / `WHATSAPP_DATA_FILES` still work as WhatsApp defaults and are seeded into the
bootstrap workspace by `scripts/migrate_v6_to_v7.py`. Real sheet: tabs INVENTORY, SALES; SALES has two date columns
TIMESTAMP / VOUCHER DATE → the bot asks once and remembers per source.

## 5. Memory
`workspace_memory.json` (ignored): per-source rules/mappings (saved only after "haan"), validated plans. Tests must
point `memory.MEMORY_FILE` elsewhere — never leave test mappings in it.

## 6. Pending / ideas
- V7 done in code, **not yet exercised live**: Google OAuth against a real Google Cloud project (needs the owner's client id/secret + public https URL), PostgreSQL in production, `APP_AUTH_MODE=oidc` (needs `.streamlit/secrets.toml [auth]`). Public sheets, files, websites, password login and SQLite are tested.
- WhatsApp conversations still live in `whatsapp_state.db` (per chat, server-side); only sources/learning come from the workspace. Moving them into `conversations` (channel=whatsapp) is the next step.
- FILTER-by-value and COMPARE as state transforms (planner handles them today).
- PDF reports, scheduled runs, approval-gated actions (send/email) — not built.
- WhatsApp media sends fail on the OpenWA server (whatsapp-web.js engine) → links are sent instead (see README troubleshooting).
- `chat_bot/` is an unrelated nested git clone (empty) sitting in the repo root — untracked; remove or move.

## 6b. V8 (2026-09-28) — what was added and how to extend
- Spec §23 questions are covered offline in `tests/test_v8_features.py` (prompt builder, protection, fuzzy, pivot, images, exports, channel, result check, RAG off/on, hybrid agent, index persistence).
- WhatsApp: `prompt_builder.set_channel("whatsapp")` in `bot.ask`; `whatsapp/format.result_list` lists grouped results whose value column carries the measure name.
- Real sheet offline check (2026-09-28): "sales ka total" = scalar SALES/AMOUNT; "sales amount category wise" = CATEGORY × SUM(AMOUNT) (40 rows); "inventory closing stock" = INVENTORY/CLOSING STOCK; "September ki sales" switches back to SALES and asks TIMESTAMP vs VOUCHER DATE once.
- Not done: RAG UI toggle (env only), stacked charts for pivots (table only), agent-side citations are the passages it retrieved (not per-sentence).

## 7. Testing pattern
- Offline, no LLM: `monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)` (planner/wording raise if called), fixed
  reference date via `monkeypatch.setattr("understanding.date", date)` **and** `monkeypatch.setattr("periods.date", date)`.
- Planner-path tests use a fake planner class returning a fixed plan (`tests/test_multi_metric.py::planner`).
- Run: `.venv/bin/python -m pytest tests -q -p no:cacheprovider -W ignore` (282 pass, 16 live tests skipped).
- Backend tests: `platform` fixture in `tests/test_backend.py` (in-memory DB via `db.configure("sqlite://")`, `DATA_DIR` in tmp, `APP_SECRET_KEY` set). UI: `APP_AUTH_MODE=none` for `AppTest`.
- Real sheet offline check: register the sheet with `MCPServer().register_google_sheet` and run the conversation with NoLLM
  (see `tests/test_periods_context.py::test_acceptance_conversation` for the exact turns).
- Live/golden (`RUN_LIVE=1`, `tests/golden_real_sheet.py`) only when the user asks — they spend OpenRouter credit.
