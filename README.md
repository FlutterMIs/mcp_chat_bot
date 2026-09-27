# MCP Universal Business Analyst V6

> **2026-09-27 — agentic upgrade.** The analyst is now generic (no business domain assumed), validates every plan
> deterministically, and runs a bounded agent loop for complex/multi-source questions. See *Agentic architecture* below.

A local Streamlit prototype with OpenRouter as the reasoning model and MCP-style read-only data tools.

## What V6 adds
- Google Sheets: downloads the entire public workbook and reads **all tabs**, not only the active `gid`.
- Strict metric/column validation: if the user asks for `AMOUNT`, the system cannot silently use `QTY`.
- Semantic but controlled column resolution: exact names first, safe aliases second.
- Web URL source: fetches page text and HTML tables.
- File sources: CSV, XLSX, XLS, PDF, DOCX, PPTX, TXT, MD, JSON, HTML.
- Universal planner: data analysis vs document/web Q&A.
- Context memory: follow-up questions inherit prior plan.
- Self-learning memory: explicit user rules/mappings and successful plans are stored locally in `workspace_memory.json`.
- Optional real MCP stdio server in `mcp_stdio_server.py` using FastMCP.
- Deterministic charts for every result table: time series → line, categories → bar (raw rows are summed per label). The **⋮ Chart** menu next to *Download CSV* switches between bar, horizontal bar, line, area, pie, donut, scatter or table only, and lets you pick the X and Y columns. Built with Altair in `chart_ui.py`, no LLM call.

## V7 — accounts, workspaces, persistent sources and chats (2026-09-27)

The web app is now a multi-device product: **login → workspace → Chat · Chats · Data Sources · Settings**. Everything the
user adds or says is stored server-side (`backend/`), so the same account sees the same sources and conversations on a
laptop, a phone, another browser, after logout/login and after a restart. `analyst.py` and the tools are unchanged; the
channels stopped owning state.

```
Browser (Streamlit: ui/*)                      WhatsApp (whatsapp/bot.py)
   st.login / email+password                      phone linked with /link <code> (Settings → Generate link code)
        └──────────────► backend/ ◄──────────────────────┘
   auth.py  workspaces.py  sources.py (registry + cache)  credentials.py (Fernet)  google_oauth.py
   conversations.py (chats, messages, results)  learning.py (per-workspace mappings/rules)  preferences.py  events.py
        └──────────────► analyst.answer(conv, question, tools)  (unchanged contract)
```

| Table | Holds |
|---|---|
| `users`, `workspaces`, `workspace_members`, `channel_identities` | accounts, personal workspace, WhatsApp number ↔ workspace |
| `sources`, `source_credentials`, `source_syncs`, `source_files`, `web_documents` | the permanent source registry: Google Sheet (public or OAuth), website (normalized text/tables stored; refresh re-fetches), files (bytes under `DATA_DIR/files/<ws>/`), databases (URL encrypted) |
| `source_mappings`, `source_rules`, `validated_plans` | what the assistant learned, scoped to (workspace, source) — replaces `workspace_memory.json` |
| `conversations`, `messages`, `results` | chat history; `analytical_context` = the analyst's state, so "August ka detail" works on device 2 |
| `user_preferences`, `events` | settings that follow the user; operator log (request_id, conversation_id, ms, no secrets) |

Setup: copy `.env.example` → `.env`, set `APP_SECRET_KEY` (long random), optionally `APP_DATABASE_URL=postgresql+psycopg://…`
(default SQLite in `app_data/`), pick `APP_AUTH_MODE` (`password` default; `oidc` with Streamlit `[auth]` secrets for
Google sign-in; `none` for a private single-user machine). Migrate a V6 install with
`.venv/bin/python scripts/migrate_v6_to_v7.py --email you@x.com --password …` (seeds the `.env` sheet/files/database as
sources, imports `workspace_memory.json` per source, links the allowlisted WhatsApp numbers).

Private Google Sheets: create a Google Cloud OAuth client (Web application, redirect URI `APP_BASE_URL/sources`), set
`GOOGLE_OAUTH_CLIENT_ID/SECRET` and `APP_BASE_URL`; Data Sources → Google Sheet → *Connect another Google account*. The
refresh token is stored encrypted, read via the Sheets API on the server, never sent to the browser or the LLM.

Source freshness: sheets/databases reload after `SOURCE_TTL_SHEET/DB` seconds (300) on the next question; websites and
files only on **Refresh**. Each card shows status and "last synced"; a failed refresh keeps the last good copy and says so.

Tests: `tests/test_backend.py` (accounts, authorization A/B can't cross, sources survive logout/login/restart without
network, conversations continue on another device, learning scope, encrypted credentials, WhatsApp link, OAuth with mocked
HTTP, migration). Hosting note: Streamlit Community Cloud cannot run the WhatsApp/OAuth server or keep SQLite; use one
VPS with Streamlit + `whatsapp_server.py` + PostgreSQL.

## Important
Google Sheet URL must be public/viewable-by-link for the XLSX export method. Private Sheets require Google OAuth/service-account integration later.

The local UI currently uses the same MCP tool contracts through a local adapter for simplicity. `mcp_stdio_server.py` exposes the contracts over actual MCP stdio transport.

## Run on Mac
```bash
cd mcp_openrouter_chatbot_v6
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

## Example
- `sales amount category wise`
- `AMOUNT se total sales batao`
- `September sales month wise`
- `sales ka data details do`
- `website ka summary do`
- upload a PDF and ask `total revenue kya hai?`

## Teach memory
Examples:
- `Remember that “turnover” means AMOUNT in SALES.`
- `Remember this rule: sales questions should use the SALES tab.`
The LLM returns a learn action and the rule is saved locally. This is memory, not automatic model retraining.

## Text-to-Speech
Every assistant answer has **🔊 Speak / ⏸ Pause / ▶ Resume / ⏹ Stop** controls. Sidebar → *Text-to-Speech*: Auto Speak (off by default), speech engine, language (auto / English / Hindi / Hinglish), number style (lakh/crore or million), voice, speed, volume.

- `tts_service.py` — `prepare_text_for_speech()` (strips markdown, emojis, code, JSON, SQL, tool lines, tables; ₹96,273,311.63 → "9 crore 62 lakh 73 thousand 311 rupees and 63 paise"), `build_speech_text()` (table/chart summaries, reads rows only when asked, e.g. "table read karke sunao"), and the provider layer: `speak(text, language, voice, speed)`.
- Providers: `BrowserTTSProvider` (default, Web Speech API, nothing leaves the browser), `LocalTTSProvider` (macOS `say` / espeak-ng, offline), `CloudTTSProvider` base + `OpenAITTSProvider` (only offered when `OPENAI_API_KEY` is set; sends answer text to OpenAI). Add Google/Azure/ElevenLabs by subclassing `CloudTTSProvider` and registering in `PROVIDERS`.
- `tts_ui.py` — Streamlit player (custom component v2) and browser voice list. If TTS fails, the chat keeps working and shows a short notice.

## Answers stay inside your data
- The planner answers only from connected sources. General-knowledge questions, small talk and metrics that don't exist (e.g. profit with no profit/cost column) get a polite "not in your data" reply that lists what is available.
- `grounding.py` checks every number in the LLM's answer against the tool result. If a number isn't there, the answer is rewritten once, then replaced with a plain answer built from the result.
- Filters match real values: case, spacing and small typos (`electronic` → `Electronics`), with `eq/ne/gt/gte/lt/lte/contains/in/not_in`. An unknown value raises an error listing the real values.
- `aggregate_source` supports `sort` + `top_n` ("top 5 city"); `query_source` supports `sort_by` ("sabse bada order"); `distinct_values` lists a column's real values.
- If a plan fails, the planner sees the error and corrects itself (up to 2 retries).
- Memory only uses past plans for sources that are connected now.

## Voice messages
The chat box has a mic button. The recording goes to an audio-capable OpenRouter model (`OPENROUTER_STT_MODEL`, default `google/gemini-2.5-flash`) using the same API key. The transcript becomes the question, and the recording stays playable in the chat.

## MCP stdio server
`python mcp_stdio_server.py` exposes: `load_file`, `register_google_sheet`, `register_web`, `source_schema`, `aggregate_source`, `query_source`, `distinct_values`, `search_source`, `get_database_schema`, `aggregate_data`.

## WhatsApp (OpenWA)
WhatsApp is a second channel for the same analyst. There is no separate WhatsApp brain.

```
WhatsApp → OpenWA → POST /webhook/openwa (whatsapp_server.py) → whatsapp/bot.py (router)
        → analyst.py (planner, validation, grounding; same code as the web app) → MCP stdio server (mcp_stdio_server.py)
        → Google Sheet / files → reply text, chart PNG, CSV, optional voice → OpenWA API → WhatsApp
```

| File | Role |
|---|---|
| `analyst.py` | The shared brain: plan → data tools → validation → grounded answer. Used by `app.py` and the WhatsApp bot. |
| `mcp_tools.py` | Real MCP client (stdio) with the same `call(name, args)` interface as the in-process tools. |
| `whatsapp_server.py` | Starlette webhook: verifies `X-OpenWA-Signature`, acks right away, processes on a worker thread. |
| `whatsapp/webhook.py` | Signature check and `message.received` → normalized `InboundMessage`. |
| `whatsapp/bot.py` | Loop guards, dedup, allowlist, per-chat context, voice notes, files, charts, commands. |
| `whatsapp/openwa.py` | OpenWA API client. Endpoints are taken from `/api/docs-json` v0.14.2. |
| `whatsapp/store.py` | SQLite: processed message keys and per-chat context (`whatsapp:{session}:{chat}`). |
| `whatsapp/format.py` | WhatsApp text: `*bold*`, Indian number format, numbered lists, `/help` text. |

Transport note: WhatsApp calls the data tools over a real MCP stdio session (`WHATSAPP_TOOL_TRANSPORT=mcp`). The Streamlit app still calls the same tools in-process, because its sources live in the browser session.

### OpenWA endpoints used
| Purpose | Endpoint |
|---|---|
| Auth | header `X-API-Key` |
| Resolve session name → id | `GET /api/sessions` |
| Text | `POST /api/sessions/{sessionId}/messages/send-text` `{chatId, text}` |
| Chart | `POST …/messages/send-image` `{chatId, base64, mimetype, caption}` |
| CSV | `POST …/messages/send-document` `{chatId, base64, mimetype, filename}` |
| Voice reply | `POST …/media/convert/voice` `{base64}`, then `POST …/messages/send-audio` `{…, ptt: true}` |
| Incoming media | inline `data.media.data` (≤ 1 MiB by default), else `GET …/messages/{chatId}/{messageId}/media` |
| Typing indicator | `POST /api/sessions/{id}/chats/typing` `{chatId, state}` |

The webhook payload shape and signature are not in the OpenAPI document. They were read from the OpenWA v0.14.2 source (`webhook.service.ts`, `whatsapp-engine.interface.ts`): body `{event, timestamp, sessionId, idempotencyKey, deliveryId, data: IncomingMessage}`, and `sha256=<HMAC-SHA256(raw body, secret)>`. Upstream sends it as `X-OpenWA-Signature`; the Ashveratech server sends `X-Ashveratech-Signature` (with `X-Ashveratech-Event/-Delivery-Id/-Idempotency-Key/-Retry-Count`). Both names are accepted.

### Setup
1. OpenWA dashboard → **API Keys** → create a key (operator role). Put it in `.env` as `OPENWA_API_KEY`.
2. Fill the WhatsApp block of `.env` (see `.env.example`): `OPENWA_SESSION=new-testing`, a random `OPENWA_WEBHOOK_SECRET` (16+ chars), `WHATSAPP_ALLOWED_NUMBERS`, and `WHATSAPP_GOOGLE_SHEET_URL` or `WHATSAPP_DATA_FILES`.
3. Start it: `./run_whatsapp_mac.sh` (listens on `:8600`).
4. Make it reachable over HTTPS:
   - Local testing: `brew install cloudflared`, then `cloudflared tunnel --url http://localhost:8600`, and use the `https://….trycloudflare.com` URL it prints.
   - Production: run it on your server behind nginx/Caddy with TLS, e.g. `https://<your-domain>/webhook/openwa`.
5. OpenWA → **Webhooks** → **Add Webhook**:
   - Session: `new-testing`
   - URL: `https://<public-host>/webhook/openwa`
   - Events: **message.received** only
   - Secret: the same value as `OPENWA_WEBHOOK_SECRET`. If the dialog has no secret field, create the webhook with `POST /api/sessions/{sessionId}/webhooks` `{url, events:["message.received"], secret}`, or set it with `PUT …/webhooks/{id}`.
6. Click **Test** on the webhook. The server log should show `webhook_ignored … "event": "test"` with HTTP 200.

### Try it
- Text: send "2026 mein total sales kitni hui?", then "highest kaun hai?", then "customer wise top 5 ka graph bana".
- Voice: record a voice note with the same question.
- Graph: "month wise graph bhejo". A PNG built from the result rows is sent.
- Document: send an Excel/CSV, then ask "is file mein total kitna hai?". The file stays in that chat's context (max 5 files).
- Commands: `/help`, `/status`, `/reset` (clears this chat's context), `/reset all` (also removes this chat's files).

### Data sources
| Source | Web app | WhatsApp |
|---|---|---|
| Google Sheet (public link) | sidebar, several at once | `WHATSAPP_GOOGLE_SHEET_URL`, or send the link in the chat |
| Website (text + HTML tables) | sidebar | send the link in the chat |
| Files (Excel, CSV, PDF, Word, PPT, TXT, JSON) | sidebar upload | send the file in the chat |
| SQL database (Postgres, MySQL, SQLite, …) | sidebar → Database | `WHATSAPP_DATABASE_URL` in `.env` only |

A database is read with `SELECT` only (up to 200,000 rows per table, 50 tables), and each table works like a sheet. For WhatsApp, use a read-only DB user. Connection strings sent in a chat are refused and never stored. Web links to localhost/private IPs are blocked (SSRF guard, redirects re-checked).

### Charts
Grouped and month-wise results get a chart image automatically (`WHATSAPP_AUTO_CHART`). Name a type to switch it: "pie chart mein dikhao", "isko line graph mein badlo", "horizontal bar". Supported types: bar, horizontal bar, line, area, pie, donut, scatter. The PNG has the values (or % for pie) printed on it and is drawn from the same result rows as the text.

### Safety
- The bot never answers messages with `fromMe`, from its own number, status broadcasts, other sessions, or groups (unless `WHATSAPP_ALLOW_GROUPS=true`).
- It only answers numbers listed in `WHATSAPP_ALLOWED_NUMBERS`.
- Each OpenWA `idempotencyKey` is processed at most once, so retries never cause a second reply.
- Sends are not retried after the request reached OpenWA; only connection failures are retried.
- Logs are JSON lines with masked phone numbers and no message text or keys.

### Tests
```bash
.venv/bin/python -m pytest tests/test_whatsapp.py            # offline: webhook, dedup, loops, media, OpenWA shapes, failures
RUN_LIVE=1 .venv/bin/python -m pytest tests/test_live_analyst.py   # real OpenRouter: context, corrections, SALES vs INVENTORY, MCP e2e
```

### Troubleshooting
| Symptom | Check |
|---|---|
| `/health` returns 503 | `OPENWA_API_KEY` / `OPENWA_SESSION` missing; see the `config_error` log line |
| OpenWA shows 401 deliveries | `OPENWA_WEBHOOK_SECRET` differs from the webhook's secret |
| No reply, log shows `skip: not_allowlisted` | add the sender to `WHATSAPP_ALLOWED_NUMBERS` (digits with country code) |
| No reply, log shows `skip: group` | expected; set `WHATSAPP_ALLOW_GROUPS=true` to allow groups |
| Voice/file: "download nahi ho paayi" | media over 1 MiB is not inline; enable media archiving in OpenWA or raise `WEBHOOK_MEDIA_INLINE_MAX_BYTES` |
| "Graph bhej nahi paaya", or the chart/CSV arrives as a link | OpenWA returns 500 for every media send (checked on w.ashveratech.com: base64, URL, tiny image, @lid and @c.us all fail; text works). That is the whatsapp-web.js engine on the OpenWA server. The bot then sends a 24-hour link to the chart PNG / CSV, which WhatsApp shows as a preview. Fix it on the server by updating whatsapp-web.js, or by switching the session to the Baileys engine (it is installed but disabled) |
| Voice reply arrives as a file, not a mic bubble | OpenWA media conversion (ffmpeg) is off; `GET /api/sessions/{id}/media/convert` shows it |
| "Data source reach nahi ho paaya" | the Google Sheet must be public (viewable by link) |

## Agentic architecture (2026-09-27)

```
user (web / WhatsApp)
  └─ analyst.answer()                      shared brain for both channels
       ├─ deterministic pre-routes         greeting · complaint re-check · media/video lists · "share kro" · rule confirmation
       ├─ agent.classify()                 SIMPLE/MEDIUM → direct path · COMPLEX → agent loop
       ├─ DIRECT PATH (one tool call)      LLM plan → sanitize_plan → semantics.validate_plan → MCP tool → enrich_result
       │                                   → code-written key figure + LLM prose → number grounding (units-aware) → answer
       └─ AGENT LOOP (agent.py)            LLM tool-calling over an allowlist: list_sources · sample_data · distinct_values ·
                                           aggregate_data · query_data · search_text · join_results · calculate · sort_limit ·
                                           compare_periods · finish
                                           results stay in Python (resultstore.py, result_ids); the LLM sees summaries
                                           limits: 8 tool steps (+ forced finish), 10 tool calls, 45 s, ~40k-token transcript
                                           every step validated (validate_plan); tool errors are observations, never crashes
                                           final answer: every number must exist in a stored result (lakh/crore aware)
                                           on limits: best verified partial result with an honest note — never a guess
```

| Module | Role |
|---|---|
| `semantics.py` | data-agnostic: question intent (sum/avg/max/count, ranking, grouping, money vs quantity), column kinds (monetary/quantity/rate/identifier from name + profile), `validate_plan` (wrong sheet / wrong metric / identifier as metric / average vs sum / superlative without ranking / named dimension not grouped), pure-complaint detection |
| `resultstore.py` | result_ids, compact summaries, `join` (normalised keys, unmatched counts, refuses truncated lookups), `calculate` (AST-based safe expressions: + - * / comparisons, pct_change, share, rank, running_total, sum/avg/min/max/count), `sort_limit` |
| `agent.py` | the bounded loop, tool allowlist, join hints, unjoined-claim guard, classification |
| `memory.py` | rules and mappings **per source**, saved only after the user confirms ("haan"); example plans only when validated; legacy global rules ignored |
| `openrouter.py` | `max_tokens` per call type, retry/backoff on 429/5xx, `llm_usage.jsonl` (tokens + cost per call), `compact_result` (≤20 rows to the wording model), `compact_schema` |

**Understanding layer (`understanding.py` + `analysis.py`):** each message becomes a structured intent (metrics, period, grain, grouping, top-N, additive/correction/drill/report) merged with the conversation's analysis *state*. Relative periods are computed in code from today's date ("last 12 months" = 12 calendar months, missing months shown as 0; "last month", "August", "2025"). For unambiguous requests the whole analysis runs without the planner: one aggregate per metric through the tools, joined by period/dimension, chronological, columns named from the user's words (Customer Count = distinct customer field, Items Count = SUM of the quantity field, Sales Amount = SUM of the monetary field), summary cards, clickable drill-down rows, and Excel reports ("report bana do"). Follow-ups update the state ("customer count bhi add karo", "August", "party wise", "top 10", "nahi total items chahiye").

**Answer validation (final layer):** every draft is checked against the *result definition* (`grounding.result_definition`): numbers must be business values of the result (matching-row counts are technical metadata, never "items sold"), a number next to a row label must be that row's value, and "X was added/included/calculated" is rejected unless X is a column of the result. Multi-metric questions (amount + quantity + count …) go to the agent, whose final table must contain every requested measure. Time-series results are always chronological (table and chart share one order). Single-value answers carry no table.

**Corrections:** "ye galat hai" → the previous plan is re-run and explained; "galat hai, amount chahiye" → the planner must change the previous plan (an identical plan is rejected and re-planned).

**Security:** the OpenRouter key and DB URL are no longer pre-filled into the browser; Streamlit binds to 127.0.0.1 (`.streamlit/config.toml`); local files and SQLite paths are restricted to allowlisted folders; the agent never gets `load_file`/`register_*`.

**Not implemented (yet):** PDF reports, scheduled runs, approval-gated external actions (send/email). Excel/CSV download and chart PNG exist.

### Tests
```bash
.venv/bin/python -m pytest tests                 # 255 offline tests: 7 unrelated schemas, agent loop with a scripted LLM, WhatsApp, security
RUN_LIVE=1 .venv/bin/python -m pytest tests/test_live_analyst.py    # real LLM incl. multi-source agent case
.venv/bin/python tests/golden_real_sheet.py      # 34 golden questions on the real sheet + website, incl. a cross-sheet agent case
```

## Canonical periods & conversational follow-ups (2026-09-27)

One resolver, `periods.resolve_date_expression(text, reference_date)`, produces every date range in the system. The
understanding layer, the LLM planner (`sanitize_plan` overrides the planner's `date_from/date_to`), the agent loop (steps
are clamped into the asked period), the state executor, charts, tables and answers all use the same result, so
"last 2 months" cannot mean two different things on two paths. LLM-written dates never bypass it.

| Expression (typed or voice, Hinglish or English) | reference 2026-09-27 → | period_type |
|---|---|---|
| last month · pichle mahine · previous month · "last moment" (STT slip) | 2026-08-01 → 2026-08-31 | previous_month |
| this month · is mahine · MTD | 2026-09-01 → 2026-09-27 | current_month_to_date |
| last 2 months · last two months · pichle do mahine · do mahine ka total | 2026-08-01 → 2026-09-27 (Aug + Sep to date) | last_n_calendar_months |
| August · Aug 2026 · 2026-08 | 2026-08-01 → 2026-08-31 | calendar_month |
| August September ka total aur August ka total | asks: Aug+Sep, Aug — every part is answered | explicit_months |

Never `today - N*30 days`. Every result's plan carries `period` metadata (`date_expression`, `reference_date`,
`period_type`, `periods[{label,start,end}]`), shown as `FinalResponse.period` / `date_range`.

**Follow-ups modify the previous result** (`understanding.understand` + `analyst.understood_reply`): "total kar ke
batao" / "sab mila ke" / "grand total" collapse a series into one scalar (SERIES → TOTAL, same source, period, filters,
metric); "only total" / "bas total bata" also drop the calculation line; "month wise" / "customer wise" / "August ka?"
/ "detail dikhao" / "top 10" keep the context and change one thing. Planner-made answers seed the same context
(`seed_state`), so a follow-up after an LLM-planned question does not start from zero.

**Simple question = simple answer:** "last 2 months ki sale kitni hui?" is one number (no table, no chart);
"last 12 months sales dikhao" is a series. A scalar for a period and the sum of that period's series are reconciled
(`analysis.reconcile`, `Conversation.totals`): a conflicting earlier number is recomputed and flagged, never shown silently.
Tables render with the same Indian currency format as the cards (`response.display_table`); raw values stay for CSV/Excel/charts.
Tests: `tests/test_periods_context.py` (the deployed-app conversation, offline).

## Ambiguity gate (never guess)

Before anything executes — and before the planner model is even called — a deterministic gate in
`understanding.ambiguity()` / `analyst.understood_reply()` decides whether the request is **CLEAR**, **AMBIGUOUS** or
needs a column that does not exist. Ambiguous requests come back as a short question with chips (web) or a numbered
list (WhatsApp); the reply ("sales person", "2", "voucher date") is matched to an option and the original request is
re-run. Settled choices are remembered per source/sheet in `workspace_memory.json`, so nothing is asked twice.

| Situation | What is asked |
|---|---|
| "top 5 sales wale" — ranking with no dimension actually named | `SALES PERSON wise total` / `Individual transactions (rows)` |
| "items count batao" — table has both a quantity column and an item-like column | `Total quantity — SUM of QTY` / `Different item types — distinct ITEM NAME` |
| Two sheets fit the question about equally ("items" lives in INVENTORY and SALES) | `INVENTORY sheet` / `SALES sheet` |
| Two date columns (TIMESTAMP, VOUCHER DATE) for a period question | one of them, remembered for that sheet |
| A measure resolves to several columns (QTY vs BOX QTY) | the columns; `ALT_`/`OLD_`-style secondaries are skipped automatically |

Rules that stay fixed: "highest sale" = MAX, "top N by total" = GROUP BY + SUM + sort + limit, "make it top 10" changes only
the limit, corrections replace the reading, `rows_matched` is never a business number. Regression tests: `tests/test_ambiguity.py`.

## Response policy (one answer, rendered once)

`response.finalize()` turns the analyst's Reply into a single `FinalResponse` — the only thing the web UI and the
WhatsApp bot render. It looks at the *shape* of the result, not at the words:

| Shape | Example | Rendered |
|---|---|---|
| scalar | "total sales kitni hai?", "ITEM CATEGORY kitna hai?" | answer + one metric card — no table, no chart |
| series | "last 12 months …" | chart (chronological) + table + one card per measure |
| ranking | "top 5 customers" | bar chart + table |
| breakdown | "city wise sales" (many groups) | table; chart only if asked ("graph") |
| detail | "August details" | table capped at 100 rows (full data via CSV/Excel), no chart |
| text | chat, clarification chips, reports | answer only |

Tool traces, plans and tool calls go to `FinalResponse.debug` and appear only with the sidebar **Debug mode** toggle.
The ⋮ chart menu is still there on every table — it just starts at "none" when the policy decided a chart adds nothing.
Tests: `tests/test_response.py`.
