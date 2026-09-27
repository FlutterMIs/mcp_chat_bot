# Pre-Agentic Audit — MCP + OpenRouter Universal Business Analyst

Audit date: 2026-09-27 · Scope: read-only (no application code, `.env`, data or services changed).
Only new files: `audit/*.md`. Reproduction script lived in a temp scratch folder (not in the project).

Status legend: **CONFIRMED** (seen in code and reproduced or measured) · **SUSPECTED** (code suggests it, not reproduced) ·
**UNVERIFIED** (could not be checked in this audit) · **NOT IMPLEMENTED** · **ALREADY IMPLEMENTED**.

---

## 1. Executive summary

The project works as a **single-step, tool-using AI assistant**. It is not yet an agent. For each message, one LLM "planner" call produces a JSON query plan. Deterministic Python guards adjust the plan, one data tool runs, and one LLM call words the answer, which a number checker then validates. It serves two channels, a Streamlit web app and a WhatsApp (OpenWA) bot, through one shared module (`analyst.py`).

The wrong answers seen on 2026-09-26/27 have been patched one by one, but the audit found **structural causes that are still present**:

1. **Memory learns mistakes and shares them with everyone.** `workspace_memory.json` holds 82 "successful plans". 16 of them are month-split plans, the April-as-total bug. They are fed back to the planner as examples. One learned rule, taken from a chat, is sent with every planner call for every user: *"Only respond with data-related messages if the user query is related to sales or inventory data…"*. This matches the repeated "main sirf sales/inventory…" refusals on website questions. **CONFIRMED** (R7, R8).
2. **Accuracy still depends on the LLM for the core choices** (sheet, metric, aggregation). The code guards only some cases: month-splitting, counts, complaints and contains-matching. If the planner picks `QTY` or `INVENTORY` for a sales question, the plan runs without complaint. **CONFIRMED** (R1, R2).
3. **The number check tests presence, not correctness.** Any number that appears anywhere in the tool result passes, even if it is the wrong row. Numbers ≤ 12 are never checked. **CONFIRMED** (R4, R5).
4. **A correction that contains "galat" is swallowed.** "galat hai, amount chahiye" goes to the complaint path, which re-runs the old QTY plan. **CONFIRMED** (R3).
5. **Token use is unbounded for detail questions.** A 500-row `query_source` result is serialised into the wording call: **328,311 characters** (roughly 80k tokens, estimate). **CONFIRMED** (measured).
6. **Security.** The OpenRouter key is pre-filled into a browser widget of a web app listening on all interfaces. Three backup zips contain `.env`. **CONFIRMED.**

Agentic readiness: the parts exist (MCP tools, error-driven replanning, deterministic calculation helpers, validation). They are wired for one step, not for a bounded multi-step loop. Section 8 and `AGENTIC_IMPLEMENTATION_PLAN.md` describe what to change. **Phase 0 (correctness and memory) must come first.**

---

## 2. Project inventory and backups

| Item | Finding | Status |
|---|---|---|
| Version control | **Not a Git repository** (`git rev-parse` fails). No history, no diff, no rollback. | CONFIRMED |
| Backups | `~/Downloads/mcp_openrouter_chatbot_v6.zip` (09-25 22:40, 52 KB, original), `mcp_openrouter_chatbot 2.zip` (09-25 23:44, 160 MB), `… 3.zip` (09-26 11:02, 192 MB), `mcp_openrouter_chatbot.zip` (09-26 11:07, 660 KB). The two large ones include `.venv`. | CONFIRMED |
| Secrets in backups | `.env` is inside `mcp_openrouter_chatbot 2.zip` and `mcp_openrouter_chatbot.zip`. `whatsapp_state.db` and `workspace_memory.json` (user questions) are inside `3.zip` and `.zip`. | CONFIRMED |
| `.env` keys (names only) | `OPENROUTER_API_KEY, OPENROUTER_MODEL, DATABASE_URL, OPENWA_API_KEY, OPENWA_SESSION, OPENWA_WEBHOOK_SECRET, WHATSAPP_ALLOWED_NUMBERS, WHATSAPP_GOOGLE_SHEET_URL` | CONFIRMED |
| Runtime data | `workspace_memory.json` (1,620 lines, 82 plans, 1 rule), `whatsapp_state.db` (+WAL), `whatsapp_shared/` (4 PNG links), `whatsapp_debug.json` (93 rejected-webhook diagnostics, last 2026-09-26 13:54) | CONFIRMED |
| Code size | `analyst.py` 426, `mcp_server.py` 402, `whatsapp/bot.py` 601, `source_loader.py` 233, `openrouter.py` 125, `app.py` 166, `tts_service.py` 565; tests 1,371 lines | CONFIRMED |

**Recommended backup procedure before any implementation:**
1. `git init`. Commit the current tree with `.env`, `.venv/`, `*.db*`, `workspace_memory.json`, `whatsapp_*` excluded (they are mostly in `.gitignore` already). Tag it `pre-agentic-baseline`.
2. Snapshot runtime state separately: `workspace_memory.json`, `whatsapp_state.db*` → dated folder outside the project.
3. Delete or re-create the zips without `.env`. If any zip that contains `.env` was ever shared, **rotate** the OpenRouter key, OpenWA key and webhook secret. (Keeping the key in `.env` during development is the user's stated choice. The risk here is the copies inside the zips.)
4. Run `pytest tests` and `tests/golden_real_sheet.py`, and store the output next to the tag as the baseline.

---

## 3. Actual architecture (from source)

```
Web (app.py)                         WhatsApp (whatsapp_server.py → whatsapp/bot.py)
  sidebar → source_loader / MCPServer    OpenWA webhook → verify HMAC → thread pool → normalize → guards/dedup
  answer_question() app.py:134           WhatsAppBot.process() bot.py:256 → ask() bot.py:298
        │  LocalTools (in-process)              │  McpStdioTools (real MCP stdio, mcp_tools.py)
        └──────────────► analyst.answer()  analyst.py:252 ◄──────────┘
                              │
     deterministic pre-routes (no LLM):  complaint/source verify :265 · greeting :271 · video :276 · image :278 · share :281
                              │
     OpenRouterAI.plan()  openrouter.py:40   (1 LLM call; system prompt 10,787 chars, 35 rules)
                              │
     sanitize_plan() :233 (date-grain + count guards) → route_documents() :220
                              │
     loop ≤3 attempts :287 ── out_of_scope → doc search fallback :297 ── clarify ── learn (memory write)
                              │
     data: run_data_plan() :57 → tools.call(aggregate_source | query_source | distinct_values)
           → MCPServer (pandas) mcp_server.py:206/263/281, filters :62, column resolution :14
           → enrich_result() grounding.py:55 → grounded_answer() analyst.py:142 (1–3 LLM calls)
     text: search_source mcp_server.py:298 → answer_text() openrouter.py:114 (1 LLM call)
                              │
     on ValueError: replan with failed_attempt :343-349 (≤2 more planner calls)
                              │
     conv.last_plan / recent_plans / history updated; memory.add_plan() :338 (global file)
                              │
     channel rendering: app.py render_assistant :93 (table, ⋮ chart, images, videos, TTS) ·
                        whatsapp/format.py compose + bot.deliver :337 (text, PNG chart, CSV/link, images)
```

Confirmed facts about the flow:
- **Two transports, same tool contract.** The web app uses `LocalTools` (in-process `MCPServer`, `analyst.py:42`). WhatsApp uses `McpStdioTools` (`mcp_tools.py:23`), a real MCP stdio session to `mcp_stdio_server.py`. The web path is **not** MCP transport. ALREADY IMPLEMENTED (as stated in README).
- **Sources are loaded fully into pandas**: sheets, files, web tables, and databases (`read_database`, `source_loader.py:216`: `SELECT` of up to 200,000 rows × 50 tables). No query is pushed down to SQL. The old SQL path `MCPServer.aggregate_data` (`mcp_server.py:142`) exists but `analyst` never uses it.
- **Unused code:** `mcp_client.MCPClient.call_tool` (async, never awaited), `import asyncio` in `app.py:1`.

---

## 4. Confirmed capabilities

| Capability | Where | Status |
|---|---|---|
| Multi-tab Google Sheet (public), CSV/XLSX/XLS/PDF/DOCX/PPTX/TXT/MD/JSON/HTML, websites (text, tables, images, videos), SQL databases | `source_loader.py`, `mcp_server.py:189-297` | ALREADY IMPLEMENTED |
| Column resolution: exact → normalised → small alias table; never QTY for AMOUNT | `resolve_column` `mcp_server.py:14-39` | ALREADY IMPLEMENTED |
| Filters eq/ne/gt/gte/lt/lte/contains/in/not_in with value resolution; unknown value → error listing real values; full-name contains → exact | `apply_filters` `mcp_server.py:62` | ALREADY IMPLEMENTED |
| sort / top_n / grand total before top_n, distinct, count on text columns | `aggregate_dataframe` `mcp_server.py:206` | ALREADY IMPLEMENTED |
| ISO vs DD/MM date parsing | `parse_dates` `source_loader.py:33` | ALREADY IMPLEMENTED |
| Deterministic derived figures (share %, change, change %) | `enrich_result` `grounding.py:55` | ALREADY IMPLEMENTED |
| Date-grain guard, count guard, document routing, complaint re-check, grand-total check, ₹-on-quantity strip, chart-claim strip | `analyst.py:106-250, 389` | ALREADY IMPLEMENTED |
| Error-driven replanning (≤2) | `analyst.py:343-349` | ALREADY IMPLEMENTED |
| Per-chat WhatsApp context in SQLite; per-browser-session web context | `whatsapp/store.py`, `st.session_state` | ALREADY IMPLEMENTED |
| MCP stdio server with 13 tools | `mcp_stdio_server.py` | ALREADY IMPLEMENTED |
| WhatsApp: HMAC verify, dedup, loop guards, allowlist, voice STT, files, links, charts, images | `whatsapp/` | ALREADY IMPLEMENTED |

## 5. Missing capabilities (relevant to the next step)

| Capability | Status |
|---|---|
| Multi-step plan with several sequential tool calls in one answer | NOT IMPLEMENTED |
| Cross-source join (SALES ↔ INVENTORY by item) | NOT IMPLEMENTED |
| Deterministic arithmetic tool the LLM can call (only fixed `enrich_result` shapes) | NOT IMPLEMENTED |
| Deterministic sheet/metric validation against the question's words | NOT IMPLEMENTED |
| Correctness-gated memory (only store plans marked correct) | NOT IMPLEMENTED |
| Per-source / per-user memory scoping | NOT IMPLEMENTED |
| Token budget, cost accounting, max_tokens, result truncation for the LLM | NOT IMPLEMENTED |
| LLM call retry/backoff on 429/5xx | NOT IMPLEMENTED |
| User approval gate for external actions | NOT IMPLEMENTED (no such actions exist yet) |
| Scheduling | NOT IMPLEMENTED |
| Business rule "SALES PENDING counts as sales?" | NOT IMPLEMENTED; decision pending with user |

---

## 6. Identified bugs (details in `WRONG_ANSWER_ROOT_CAUSES.md`)

| ID | Finding | Evidence | Status |
|---|---|---|---|
| B1 | Planner-chosen `QTY` for a sales-amount question is accepted; the metric guard (`analyst.py:76-79`) fires only when the resolved column differs from the planned one | R1 | CONFIRMED |
| B2 | `INVENTORY` sheet accepted for a sales question; sheet choice is prompt-only (rule 26, `openrouter.py:90`) | R2 | CONFIRMED |
| B3 | Correction containing "galat" is routed to `verify_previous`, which re-runs the old plan (`analyst.py:265`) | R3 | CONFIRMED |
| B4 | Grounding accepts a wrong-row number and any number ≤ 12 (`grounding.py:46-49`) | R4, R5 | CONFIRMED |
| B5 | Prefix value resolution silently maps "ABC" → "ABC Traders" (`mcp_server.py:55`) | R6 | CONFIRMED |
| B6 | Learned rules are global across users and sources (`memory.py:55`, `analyst.py:261`); a learned refusal rule exists in production memory | R7 + file | CONFIRMED |
| B7 | Every executed plan is stored as "successful", including wrong ones, and replayed (`memory.py:38-42`); 16/82 stored plans are month-splits | R8 + file | CONFIRMED |
| B8 | WhatsApp dedup key is OpenWA's per-webhook `idempotencyKey` (`whatsapp/webhook.py:58`); two webhooks → two replies | R9 | CONFIRMED |
| B9 | `format_result` receives the full result (500 rows → 328,311 chars) | measured | CONFIRMED |
| B10 | Planner prompt rules are numbered out of order (…28, 35, 30, 29, 31…); rule 14 claims memory is "user-approved" but no approval exists | `openrouter.py:73, 93-96` | CONFIRMED |
| B11 | Running WhatsApp server started ~24 h before this audit, so it predates the latest fixes (image/video/routing). Python does not hot-reload. | `ps` etime | CONFIRMED |
| B12 | Two webhook deliveries per message (one unsigned, one bad signature) from 01:00–13:54 on 09-26, which points to extra webhooks configured then; only one remains now | `whatsapp_debug.json`, `GET …/webhooks` | CONFIRMED (historical) |
| B13 | Web retrieval for pages > 18,000 chars keeps the first 40 lines + keyword hits; recall for paraphrased questions may be low | `mcp_server.py:298` | SUSPECTED |
| B14 | Concurrent writers (web + WhatsApp) to `workspace_memory.json` with no lock | `memory.py:17` | SUSPECTED |

---

## 7. Security findings

| Area | Finding | Status |
|---|---|---|
| Secrets → browser | `st.text_input("OpenRouter API Key", value=os.getenv(...), type="password")` (`app.py:23`). The value is sent to the browser. Streamlit listens on `*:8501` (lsof) with no auth and no `.streamlit/config.toml`, so anyone on the network can open the app and reveal the key. Same for the Database URL field (`app.py:41`). | CONFIRMED |
| Secrets in backups | `.env` inside two zips | CONFIRMED |
| Logs | JSON logs mask phone numbers and redact `sk-or…`, bearer, X-API-Key (`whatsapp/log.py:14`). Error strings may still contain business values (filter values, names). | CONFIRMED / low |
| SQL | Legacy SQL path validates table and column names against the inspector and parameterises values (`mcp_server.py:142-181`). `read_database` uses a SQLAlchemy `select(Table)`. No injection found. | CONFIRMED safe |
| DB connect from web UI | Any SQLAlchemy URL is accepted, including `sqlite:///any/local/path.db` → reads local SQLite files. Connecting to internal DB hosts is also possible. | CONFIRMED (local-user risk) |
| WhatsApp DB URL | Refused in chat (`DB_URL` regex, `bot.py:50`); only `.env` | CONFIRMED |
| URL fetch (SSRF) | `check_public_url` blocks private/loopback/link-local and re-checks each redirect (`source_loader.py:116-148`). DNS is resolved twice (check, then `requests`) → a DNS-rebinding window. No response size cap. | CONFIRMED guard / SUSPECTED gaps |
| MCP `load_file(path)` | Reads any local path the process can read (`mcp_stdio_server.py:29`). Harmless while only the bot calls it; **dangerous if exposed to an agent loop.** | CONFIRMED |
| Uploads | Streamlit default upload limit (200 MB) applies; WhatsApp body ≤ 20 MB (`whatsapp_server.py:26`); parsers run in-process | CONFIRMED |
| Webhook auth | HMAC-SHA256 over the raw body; `X-Ashveratech-Signature` / `X-OpenWA-Signature`; 401 on mismatch; 503 without a secret unless `WHATSAPP_ALLOW_UNSIGNED` (`whatsapp_server.py:62-80`). No timestamp/replay window (dedup only). | CONFIRMED |
| Shared chart/CSV links | `/r/<128-bit token>.png|csv`, 24 h expiry, no auth (`whatsapp/links.py`) | CONFIRMED (by design) |
| Isolation | WhatsApp context per `whatsapp:{session}:{chat}`; web per browser session. **Memory (rules, plans with user questions) is global.** | CONFIRMED |
| Debug residue | `whatsapp_debug.json` stays on disk (header names, expected HMACs, test bodies) | CONFIRMED / low |

## 8. Performance and resources

| Item | Finding | Status |
|---|---|---|
| Measured RSS | `streamlit run app.py` 36 MB, `whatsapp_server.py` 30 MB, `mcp_stdio_server.py` 31 MB (macOS `ps`). macOS RSS excludes compressed pages and these processes were idle, so this is **not** a reliable working-set figure. | CONFIRMED value / UNVERIFIED meaning |
| Data duplication | The same sheet is held in memory once in the Streamlit session, again in each per-request `MCPServer` (`app.py` answer_question) and again in the MCP stdio process. A 200k-row × 50-table database multiplies this. | CONFIRMED (code) |
| Hosting | MilesWeb shared/managed plan (mPanel). CPU, RAM and process limits of the account are **unknown**. The measurements above say nothing about them. | UNVERIFIED |
| Latency | From WhatsApp logs: planner ~3–5 s, OpenWA send-text 4–5 s, total 8–13 s per message | CONFIRMED (log sample) |
| Sheet refresh | Re-downloaded on demand after 300 s TTL → periodic slow answers | CONFIRMED (code) |

---

## 9. Tests

Executed in this audit (safe, offline, no credentials):
- `pytest tests` → **97 passed, 15 skipped**. The skips are `tests/test_live_analyst.py` (needs `RUN_LIVE=1` + OpenRouter key).
- A deterministic reproduction script (no LLM, no network, temp memory) → R1–R9 reproduced, R10 confirms a previous fix.

Not executed in this audit (they need the production OpenRouter key and the live sheet/website): `tests/test_live_analyst.py`, `tests/golden_real_sheet.py`.
*Prior evidence (earlier session, not re-run now):* golden suite 33/33 on 2026-09-27.

Coverage gaps and proposed tests: see `AGENTIC_IMPLEMENTATION_PLAN.md` §Tests.

---

## 10. Agentic readiness

| Requirement | Today | Status |
|---|---|---|
| Tool selection | LLM chooses one of aggregate/rows/distinct/text via JSON plan | ALREADY IMPLEMENTED (single choice) |
| Multi-step planning | One plan per question | NOT IMPLEMENTED |
| Sequential tool execution | One data tool per answer | NOT IMPLEMENTED |
| Replanning after tool errors | ≤2 replans with the error text | ALREADY IMPLEMENTED |
| Cross-source analysis | None (sources are separate) | NOT IMPLEMENTED |
| Deterministic calculations | Fixed shapes only (`enrich_result`, grand total, count) | PARTIAL |
| Result validation | Presence-only number check + specific guards | PARTIAL (see B4) |
| Bounded retries | Planner ≤3, wording ≤3; no LLM HTTP retry | PARTIAL |
| User approval before external actions | No external actions exist | NOT IMPLEMENTED |
| Scheduled tasks | — | NOT IMPLEMENTED |

**Verdict:** ready to build a *hybrid* agent on the existing tool layer, **after** Phase 0. Adding a multi-step loop now would multiply today's single-step errors (wrong sheet, wrong metric, memory bias) across several steps, and cost more tokens per question.
