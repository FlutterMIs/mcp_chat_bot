# Agentic Implementation Plan (hybrid)

Principle: **reuse the existing tool layer and guards; add a bounded loop only for complex questions; fix correctness first.**
Nothing in this plan has been implemented. Every phase needs explicit approval.

Target architecture:

```
question ─► analyst.answer()
            ├─ deterministic pre-routes (existing: greeting, media, share, complaint)
            ├─ classify(question, state) ──► SIMPLE/MEDIUM ─► existing direct pipeline
            │                                   plan → sanitize → validate_sheet/metric (new) → 1 tool → code-rendered figures → LLM prose → validate
            └──────────────────────────────► COMPLEX ─► agent.run() (new, bounded)
                                                  loop ≤6: LLM picks tool + args (tool-calling) → same MCP tools + new calc/join tools
                                                  → results stored in Python by result_id, summaries to LLM
                                                  → final: validator checks every number ← result_ids → answer
```

Reused as-is: `mcp_server.py` tools (aggregate/query/distinct/search/find_*), `mcp_tools.py` MCP transport, `grounding.enrich_result`, `sanitize_plan`, `route_documents`, `verify_previous`, channel rendering (`app.py`, `whatsapp/format.py`, `chart_ui.render_png`), WhatsApp stack, tests and golden suite.

---

## Phase 0 — Fix wrong answers and validation (prerequisite)

Goals: the direct pipeline is correct and consistent on the real sheet; memory stops teaching mistakes.

| Work item | Files likely to change |
|---|---|
| Baseline safety: `git init` + tag, runtime-state snapshot, zips without `.env` (+ key rotation if the zips were shared) | none (ops) |
| `validate_metric` / `validate_sheet` / dimension synonym check after `sanitize_plan`; mismatch → `failed_attempt` replan (B1, B2, #6) | `analyst.py` |
| Complaint shortcut only for pure complaints; corrections go to the planner with `correction_of` (B3) | `analyst.py` |
| Code-rendered key figures; tighten `unsupported_numbers` (remove the ≤ 12 exemption, bind numbers to rows) (B4) | `analyst.py`, `grounding.py`, `whatsapp/format.py` |
| Value resolution: prefix/fuzzy → clarify unless unique; a code-written note in the reply (B5) | `mcp_server.py`, `analyst.py` |
| Memory: store only validated plans; per-source scoping; ≤ 5 examples; learned rules need confirmation and are scoped per source; purge the month-split plans and (with user approval) the global refusal rule (A1, A2, B6, B7) | `memory.py`, `analyst.py`, `workspace_memory.json` (data) |
| Business rule for `VOUCHER NAME = SALES PENDING` (user decision) stored as a per-source rule | `memory.py` / config |
| Token hygiene T1, T2, T6, T7, T8 (truncate the wording payload, compact memory, max_tokens, retry/backoff, usage logging) | `openrouter.py`, `analyst.py` |
| WhatsApp dedup on `sessionId + data.id` (A4) | `whatsapp/webhook.py` |
| Web security: don't pre-fill the API key into the browser; bind Streamlit to 127.0.0.1 via `.streamlit/config.toml`; restrict the DB URL field (no `sqlite:///` outside the project) | `app.py`, new `.streamlit/config.toml`, `source_loader.py` |
| Renumber and deduplicate the planner rules; remove the "user-approved memory" claim | `openrouter.py` |

- **Dependencies:** none new.
- **Risks:** stricter validation can turn some previously answered questions into clarifications. Watch the golden suite for over-rejection.
- **Tests (new):** see §Tests (P0-1 … P0-12).
- **Acceptance:** `pytest tests` green. Golden suite **run 3× in a row** with 100 % pass and identical numbers across runs. The results match pandas truth for both counting rules of Sales/Pending. Planner input for a simple question ≤ 8k tokens (estimated from chars). No global rule is sent for a website-only chat.

## Phase 1 — Bounded agent orchestration

Goals: complex questions run as a short, validated multi-step loop. Simple questions stay unchanged.

| Work item | Files |
|---|---|
| `classify(question, state)` → SIMPLE / MEDIUM / COMPLEX (rules first; LLM only as tie-breaker) | `analyst.py` |
| `agent.py`: OpenRouter tool-calling loop over an **allowlisted** tool set (read-only: schema, distinct, aggregate, query, search, find_*); `MAX_AGENT_STEPS=6`, `MAX_TOOL_CALLS=8`, token budget, 45 s timeout | new `agent.py`, `openrouter.py` (tool-calling request) |
| Result store: each tool result kept in Python by `result_id`; the LLM sees summaries (≤ 10 rows + aggregates) | `agent.py` |
| Step guards: every aggregate step passes through `sanitize_plan` + `validate_metric/sheet`; tool errors return to the loop as observations | `agent.py`, `analyst.py` |
| Final validator: every number in the answer must map to a `result_id` value or a deterministic calc output | `grounding.py` |
| Step trace in the reply footer ("SALES → INVENTORY join → top 10") | `whatsapp/format.py`, `app.py` |
| **Excluded tools:** `load_file`, `register_*`, `get_database_schema`/`aggregate_data` (raw DB) are not exposed to the loop | `agent.py` |

- **Dependencies:** OpenRouter tool/function calling for the chosen model (**UNVERIFIED** for `openai/gpt-4.1-mini` via OpenRouter; confirm with a one-call probe before building).
- **Risks:** latency and cost growth; loops that repeat the same tool; the LLM inventing arithmetic. Mitigations: hard limits, a repeated-call detector, and no-arithmetic-in-LLM enforced by the validator.
- **Tests:** P1-1 … P1-6.
- **Acceptance:** complex golden questions answered within limits in ≥ 90 % of runs, all numbers validated. Simple questions show no latency or cost change (±10 %).

## Phase 2 — Cross-source analysis and deterministic calculation tools

| Tool | Behaviour |
|---|---|
| `join_results(left_id, right_id, on, how)` | pandas merge on normalised keys (e.g. ITEM NAME), reporting unmatched keys |
| `compute(result_id, expr)` | a whitelisted expression set: ratio, difference, % change, share, running total, rank; no `eval` |
| `compare_periods(source, sheet, metric, period_a, period_b, by)` | two aggregates + deterministic change table |
| `stock_cover(sales_id, stock_id)` | days-of-stock / fast-moving-low-stock on join results |

- **Files:** `mcp_server.py` (+ `mcp_stdio_server.py` exposure), `agent.py`.
- **Risks:** key mismatches between SALES.ITEM NAME and INVENTORY.ITEM NAME (formatting differences). Mitigation: normalised join key + an unmatched-key report in the answer.
- **Tests:** P2-1 … P2-4.
- **Acceptance:** "fast-moving items with low stock" on the real sheet matches a pandas reference implementation exactly.

## Phase 3 — Reports and user-approved external actions

- Report builder (PDF/XLSX with charts from `render_png`) in a new `reports.py`.
- Actions (`send_whatsapp`, `send_email`) exist only as **proposals**: the agent returns a draft and a confirm button (web) or a "reply HAAN to send" prompt (WhatsApp). Execution happens only on explicit confirmation from the same user, with an audit log.
- **Files:** new `reports.py`, `agent.py`, `app.py`, `whatsapp/bot.py`.
- **Risks:** sending to the wrong recipient; data leakage. Mitigations: an allowlist of recipients, a preview, confirmation, rate limits.
- **Acceptance:** no action executes without confirmation (test), and every action is logged.

## Phase 4 — Optional scheduling and WhatsApp integration of agent features

- A scheduler (`schedules` table in SQLite + a worker thread, or OS cron calling a CLI) for "every Monday 9:00 send the weekly sales report to X". Each schedule stores a validated plan/agent recipe and its recipients. Missed runs are logged, not replayed blindly.
- WhatsApp: expose complex questions and report drafts. Approvals via reply.
- **Deployment:** requires a host that allows long-running processes. The MilesWeb account's limits are UNVERIFIED (see audit §8). A VPS or the OpenWA host is recommended.
- **Acceptance:** schedules survive a restart; no duplicate sends (idempotency per schedule and run date).

---

## Tests to add

| ID | Test | Type |
|---|---|---|
| P0-1 | "sales amount" + planner returns QTY → corrected to AMOUNT | unit (fake planner) |
| P0-2 | "total qty" + planner returns AMOUNT → rejected | unit |
| P0-3 | sales question + plan on INVENTORY → SALES; stock question + SALES → INVENTORY; tie → clarify | unit |
| P0-4 | total → "highest kaun hai" never repeats the total; entity-named → group + top_n 1 | unit + golden |
| P0-5 | "galat hai, amount chahiye" changes the metric; "ye galat hai" re-verifies | unit (no LLM for the second) |
| P0-6 | "customer nahi salesman wise" / "SP wise" → SALES PERSON | unit + golden |
| P0-7 | prefix value with 2 candidates → clarify; unique → a code-written note | unit |
| P0-8 | wrong-row-as-total answer rejected; "7 customers" vs 5 rows rejected | unit |
| P0-9 | memory: an unvalidated plan is not stored; a rule saved in chat A is absent in chat B / another source | unit |
| P0-10 | wording payload ≤ 20 rows regardless of result size | unit |
| P0-11 | same message via two webhook keys → one reply | unit |
| P0-12 | golden suite 3× → identical numbers; both Sales/Pending rule variants | live |
| P1-1 | "January vs February mein sabse zyada gire 5 items aur unka stock" → ≤ 6 steps, all numbers bound to result_ids | live golden |
| P1-2 | the agent stops at MAX_AGENT_STEPS with a partial validated answer | unit (fake LLM looping) |
| P1-3 | a tool error mid-loop → recovery or an honest failure, never a fabricated figure | unit |
| P1-4 | the token budget is exceeded → stop + message | unit |
| P1-5 | simple questions never enter the agent loop | unit |
| P1-6 | the agent cannot call `load_file`/`register_*` | unit |
| P2-1 | join SALES↔INVENTORY on normalised item names, with an unmatched-key report | unit |
| P2-2 | `compute` rejects non-whitelisted expressions | unit |
| P2-3 | compare_periods equals the pandas reference | unit |
| P2-4 | fast-moving-low-stock on the real sheet equals the pandas reference | live golden |

## Estimated effort (rough, for planning only)

| Phase | Size | Notes |
|---|---|---|
| 0 | medium (largest share of value) | mostly `analyst.py`, `memory.py`, `grounding.py` + tests |
| 1 | medium | new `agent.py`; depends on the tool-calling probe |
| 2 | small–medium | new tools on top of pandas |
| 3 | medium | reports + approval UX in two channels |
| 4 | medium | scheduler + hosting |
