# PROJECT_STATE.md — MCP Universal Business Analyst (V6)

Last updated: 2026-09-27. Architecture and every module's role are in `README.md` (this file lists what is not obvious there).

## 1. What it is
Streamlit web app + WhatsApp bot sharing one brain (`analyst.answer`). OpenRouter LLM reads language; MCP-style read-only
tools compute; every number is validated in code (`grounding.py`, `semantics.validate_plan`).

## 2. Files that matter most
| File | Role |
|---|---|
| `analyst.py` | routing: pre-routes → understanding/state path → multi-metric → agent → LLM planner; `sanitize_plan`, `seed_state` |
| `periods.py` | **the only** date resolver (`resolve_date_expression`, `enforce_period`) |
| `understanding.py` / `analysis.py` | intent + follow-up merge (collapse, additive, correction, drill) → deterministic executor, `reconcile` |
| `response.py` | `finalize` → one `FinalResponse` per message (shape: scalar/series/ranking/breakdown/detail/text), `display_table` |
| `agent.py`, `resultstore.py` | bounded tool loop for complex questions |
| `app.py`, `whatsapp/bot.py` | the two channels; both render only the `FinalResponse` |

## 3. Rules (do not break)
- Generic: no business column names hardcoded. Never guess a number; ask (chips) when ambiguous.
- Dates: only `periods.py`. "last month" = previous calendar month; "last N months" = N calendar months ending today.
- Follow-ups modify the previous state; "total kar ke batao" collapses, it never restarts.
- One UI render per message; debug material only with the sidebar toggle.
- Secrets never printed/committed (`.env` ignored).

## 4. Data / config
`.env`: OpenRouter key/model, `WHATSAPP_GOOGLE_SHEET_URL` (real sheet: tabs INVENTORY, SALES; SALES has two date
columns TIMESTAMP / VOUCHER DATE → the bot asks once and remembers per source).

## 5. Memory
`workspace_memory.json` (ignored): per-source rules/mappings (saved only after "haan"), validated plans. Tests must
point `memory.MEMORY_FILE` elsewhere — never leave test mappings in it.

## 6. Pending / ideas
- PDF reports, scheduled runs, approval-gated actions (send/email) — not built.
- WhatsApp media sends fail on the OpenWA server (whatsapp-web.js engine) → links are sent instead (see README troubleshooting).
- `chat_bot/` is an unrelated nested git clone (empty) sitting in the repo root — untracked; remove or move.

## 7. Testing pattern
- Offline, no LLM: `monkeypatch.setattr(analyst, "OpenRouterAI", NoLLM)` (planner/wording raise if called), fixed
  reference date via `monkeypatch.setattr("understanding.date", date)` **and** `monkeypatch.setattr("periods.date", date)`.
- Planner-path tests use a fake planner class returning a fixed plan (`tests/test_multi_metric.py::planner`).
- Run: `.venv/bin/python -m pytest tests -q -p no:cacheprovider -W ignore` (243 pass, 16 live tests skipped).
- Real sheet offline check: register the sheet with `MCPServer().register_google_sheet` and run the conversation with NoLLM
  (see `tests/test_periods_context.py::test_acceptance_conversation` for the exact turns).
- Live/golden (`RUN_LIVE=1`, `tests/golden_real_sheet.py`) only when the user asks — they spend OpenRouter credit.
