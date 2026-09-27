# Token & Cost Audit

Measured values are **character counts** taken from the code and the real connected sheet on 2026-09-27 (no LLM calls were made).
Token figures are **estimates** at ~4 characters per token for English/JSON. Romanized Hindi and Devanagari tokenise differently, so treat every token number as ±30 %. No provider-side token usage was read. **No measured token counts exist in this project** (there is no usage logging).

## 1. Current LLM call pattern

| Setting | Value | Where |
|---|---|---|
| Model | `OPENROUTER_MODEL` (default and current: `openai/gpt-4.1-mini`), same model for planning, wording and document Q&A | `openrouter.py:24-26`, `.env` |
| STT model | `google/gemini-2.5-flash` (`OPENROUTER_STT_MODEL`) | `openrouter.py:119` |
| temperature | 0 | `openrouter.py:25` |
| max_tokens | not set | `openrouter.py:25` |
| timeout | 120 s; no retry on HTTP errors (`raise_for_status`) | `openrouter.py:25-26` |
| caching / summarisation | none | — |
| usage logging | none | — |

**Calls per user message (from code paths):**

| Path | Planner | Wording | Total LLM calls |
|---|---|---|---|
| Greeting, image list, video list, "share kro", complaint/source re-check, count answer | 0 | 0 | **0** (`analyst.py:265-284`, `count_answer`) |
| Data question, happy path | 1 | 1 | **2** |
| Data question with total check retry | 1 | 2 | 3 |
| Data question with number-check retry | 1 | 2–3 | 3–4 |
| Tool error → replan (≤ 2) | up to 3 | up to 3 | **up to 6** |
| Website/document question | 1 | 1 (`answer_text`) | 2 |
| Refusal → document search fallback | 1 | 1 | 2 |
| WhatsApp voice note | +1 STT | | +1 |

## 2. Measured payload sizes (real sheet: SALES 8,252 × 22, INVENTORY 333 × 3)

| Component | Characters | ≈ Tokens (est.) | Sent on |
|---|---|---|---|
| Planner system prompt (35 rules) | 10,787 | ~2,700 | every planner call |
| `source_schemas` (both tabs, up to 40 sample values per dimension column) | 14,283 | ~3,600 | every planner call |
| Memory (`get_context`: 20 plans + 1 rule) | 11,125 | ~2,800 | every planner call |
| History (8 lines) + previous/earlier plans | ~1,000–4,000 (varies) | ~250–1,000 | every planner call |
| **Planner call input, total** | **~37,000–40,000** | **~9,500–10,000** | |
| `format_result` system prompt | 2,279 | ~570 | every wording call |
| Result: single total | 178 | ~45 | |
| Result: customer-wise, no top_n (317 rows) | 23,150 | ~5,800 | |
| Result: item-wise, no top_n (473 rows) | 37,184 | ~9,300 | |
| **Result: `query_source` detail rows (500 × 22)** | **328,311** | **~82,000** | "data do / details do" |
| `answer_text` system prompt | 1,523 | ~380 | document Q&A |
| Website retrieval (callsaathi.ai, "features") | 1,154 (cap 18,000) | ~300 (≤ 4,500) | document Q&A |

**Estimated input per question (happy path):** a simple total is ~10.5k tokens. A grouped list without top_n is ~16k. A detail-rows question is **~92k tokens**. A retry doubles the wording part.

Observations (CONFIRMED from code):
1. The **whole planner prompt, schema and memory are re-sent on every call**, including replans. About 75 % of the planner input is identical from one question to the next.
2. **Memory is the second-largest block** and adds examples that can be wrong (see A2).
3. **Wording calls receive all rows**, although the channels render the table/list from code (`rows_are_shown_below_your_answer`, `openrouter.py:107-108`).
4. History is capped (20 stored, 8 sent), so conversation growth is bounded. ALREADY IMPLEMENTED.
5. Web retrieval is capped at 18,000 chars. ALREADY IMPLEMENTED.

## 3. Token-saving opportunities (ranked by effect)

| # | Change | Est. saving | Risk |
|---|---|---|---|
| T1 | Truncate the wording payload: ≤ 20 rows + `grand_total`, `groups_total`, `rows_matched`, `derived`; never send detail rows beyond 10 | ~90 % on list/detail questions (~70k tokens on "details do") | low: code already renders the full table |
| T2 | Memory: send ≤ 5 validated examples for the active source; drop unvalidated ones | ~2,000 tokens/call | low (improves accuracy too) |
| T3 | Compact schema: for the planner, send column names + role + ≤ 8 sample values; fetch more via `distinct_values` only when a filter value is unknown | ~2,000–2,500 tokens/call | medium: fewer values for fuzzy mapping; `find_value` covers misses |
| T4 | Prompt caching: keep the system prompt byte-stable and first (OpenAI-family models on OpenRouter cache repeated prefixes automatically where supported; the provider bills cached input at a discount) | cost, not tokens; UNVERIFIED for this model/route | none |
| T5 | Deterministic answers for single-value results (headline number from code, one short LLM sentence or none) | 1 call on ~40 % of questions (share of question types UNVERIFIED) | low |
| T6 | `max_tokens` per call type (planner 600, wording 400, document 500) | caps runaway outputs | low |
| T7 | Retry only on 429/5xx with backoff; on JSON parse failure, one repair attempt with a short prompt instead of a full replan | avoids full-price replans | low |
| T8 | Usage logging (`usage` field from the OpenRouter response) per call type, source and channel | enables real measurement | none |

## 4. Proposed per-query budgets (hybrid design)

| Class | Route | Max LLM calls | Max tool calls | Input budget (est.) | Output budget | Latency target |
|---|---|---|---|---|---|---|
| **Simple** (total, top N, month-wise, count, follow-up edit, greeting, media) | existing direct pipeline + T1–T3 | 2 (0 for deterministic paths) | 1 | ≤ 8k tokens | ≤ 800 | < 6 s |
| **Medium** (comparison of 2 periods, filter + group, document Q&A) | direct pipeline, 1 replan allowed | 3 | 2 | ≤ 15k | ≤ 1,000 | < 10 s |
| **Complex** (cross-source, multi-step "why", reports) | bounded agent loop | ≤ 6 LLM steps + 1 final | ≤ 8 | ≤ 40k total, hard stop | ≤ 1,500 | < 30 s |

Routing rule: the complex path is chosen only by explicit signals. These are multiple sources or tabs named, words like "compare … with stock", "why / kyun", "report banao", or an earlier direct-path plan that failed validation twice. Everything else stays on the direct path.

## 5. Context compression for the agent loop

- Keep a structured **analysis state** (source, sheet, metric, aggregation, filters, dates, grouping, sort) instead of raw history. It serialises to ~300 chars vs 1–4k for history.
- Tool results enter the loop as **summaries**: row count, columns, first ≤ 10 rows, aggregates, and a `result_id` handle. The full data stays in Python, and later steps reference it by `result_id` (e.g. join, compute).
- Step transcript: keep only the last 3 steps verbatim and compress older ones to one line each ("step 2: aggregate SALES.AMOUNT by ITEM, 473 rows → r2").
- Hard limits: `MAX_AGENT_STEPS = 6`, `MAX_TOOL_CALLS = 8`, `MAX_INPUT_TOKENS_PER_QUERY ≈ 40k` (estimated via character count ÷ 4 until real usage is logged), wall-clock timeout 45 s. When a limit is hit, return the best validated partial answer with a note, never an unvalidated guess.
