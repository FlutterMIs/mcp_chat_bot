# Wrong-Answer Root Causes

Each scenario: code path → evidence → reproduction → root cause → recommended fix → regression test.
"R#" refers to the deterministic reproduction run on 2026-09-27. It used no LLM and no network, with a temp memory file and the fixture workbook from `tests/conftest.py`. "Prior evidence" is from earlier live runs in this project's history and was **not** re-run in this audit.

Common path for all data answers:
`analyst.answer` (`analyst.py:252`) → `OpenRouterAI.plan` (`openrouter.py:40`) → `sanitize_plan` (`analyst.py:233`) → `route_documents` (`:220`) → `run_data_plan` (`:57`) → `MCPServer.aggregate_dataframe` / `query_dataframe` (`mcp_server.py:206/263`) → `grounded_answer` (`analyst.py:142`).

---

## 1. Sales AMOUNT answered with QTY or inventory values

- **Code:** metric choice is made by the LLM (prompt rules 2–4, `openrouter.py:61-63`). `resolve_column` (`mcp_server.py:14-39`) never substitutes QTY for AMOUNT. The only runtime guard is `analyst.py:76-79`, which fires **only if the resolved column differs from the planned column**.
- **Evidence:** R1: a plan `metric: "QTY"` on SALES runs and returns 28 with no error. The question text is never compared with the chosen metric.
- **Reproduce:** `run_data_plan(tools, conv, {"sheet_name":"SALES","operation":"aggregate","metric":"QTY",...})`.
- **Root cause:** there is no deterministic check that "amount / sale / revenue / ₹" in the question maps to an amount-like column, or that "qty / quantity / stock" maps to a quantity-like one.
- **Status:** CONFIRMED (gap). Prior evidence: live test `test_not_quantity_amount` and golden "sales amount batao" passed, so the LLM usually chooses correctly.
- **Fix:** add `validate_metric(question, plan, schema)` in `analyst.py`, called after `sanitize_plan`. It classifies question intent (money vs quantity vs count) and column semantics (name keywords + numeric profile) and rejects mismatches with an error the planner receives in `failed_attempt`. Apply it to the *resolved* column.
- **Test:** plan returns QTY for "2026 ki sales amount" → must raise/replan, final metric AMOUNT. Plan returns AMOUNT for "total qty" → must be rejected.

## 2. Sales question answered from closing stock / INVENTORY

- **Code:** sheet choice is prompt-only (rule 26, `openrouter.py:90`). `run_data_plan` (`analyst.py:63-71`) checks only that the tab exists.
- **Evidence:** R2: a plan on `INVENTORY.AMOUNT` (stock value) returns 1,900,000 as a plain sum. Prior incident (2026-09-26): "closing stock kitna hai" → INVENTORY, then the correction "I am asking about sales amount" moved to SALES (live test passed).
- **Root cause:** there is no deterministic sheet scoring. Both tabs contain a numeric `AMOUNT` column, so name-based column resolution cannot tell them apart.
- **Status:** CONFIRMED (gap); correct in all recorded live/golden runs (prior evidence).
- **Fix:** `validate_sheet(question, plan, schema)`. Score each tab by the question's domain words (sale/bikri/customer/invoice → SALES; stock/closing/inventory → INVENTORY) against tab names and column names. Reject a plan whose tab scores lower than another tab, or ask a clarification when the scores tie.
- **Test:** "sales amount batao" with a plan on INVENTORY → corrected to SALES; "closing stock" with a plan on SALES → corrected to INVENTORY; ambiguous "amount batao" → clarify.

## 3. "Which one has the highest amount?" computed as SUM instead of MAX / ranking

- **Code:** prompt rule 24 (`openrouter.py:84-88`) distinguishes the highest transaction (rows sorted desc, limit 1) from the highest entity total (group + sum + top_n 1). No code guard.
- **Evidence:** prior live test `test_total_then_highest` passed: after a total, "Which one is highest?" returned the ₹45,000 transaction and did not repeat the total. No deterministic check prevents a repeat of the SUM plan.
- **Root cause:** follow-up semantics exist only in the prompt, and the user's wording is often truly ambiguous (transaction vs customer).
- **Status:** SUSPECTED residual risk (LLM-dependent); UNVERIFIED in this audit (no live run).
- **Fix:** a deterministic follow-up guard. If the question contains a superlative (highest / sabse zyada / max / top) and the new plan equals `previous_plan` with an unchanged aggregation and no `sort`/`top_n`, reject it with "superlative requested; use sort desc + top_n or operation rows sort_by metric". If the entity is unspecified, prefer rows + sort (the transaction) and mention how to get per-customer results.
- **Test:** total plan → "highest kaun hai?" → the result must not equal the previous total and must have `sort=desc`. "sabse zyada kis customer ki" → `group_by=[CUSTOMER]`, `top_n=1`.

## 4. Context lost between related questions

- **Code:** context sent to the planner = `previous_plan`, `earlier_plans` (last 4), last 8 history lines, `just_connected_source`, global memory (`analyst.py:260-262`). WhatsApp persists `last_plan / history(20) / recent_plans(5) / files / focus` per chat (`whatsapp/store.py:31-36`). Web keeps them in `st.session_state` (lost on app restart or browser refresh).
- **Evidence:** CONFIRMED by code: a web restart drops context and all connected sources. CONFIRMED (R8 + production file): 16 of 82 stored "successful plans" are month-split plans, and they are sent as examples with every `google_sheet` question (`memory.get_context`, `memory.py:45-59`). This biases follow-ups toward the old wrong shape. Prior evidence: live chains (customer wise → top 5 → graph; referring back to an earlier list) passed.
- **Root cause:** (a) memory stores unvalidated plans; (b) web context is not persisted; (c) the chosen context depends on the LLM interpreting short follow-ups.
- **Status:** CONFIRMED (a, b); follow-up interpretation SUSPECTED/LLM-dependent.
- **Fix:** see #5 and Phase 0 memory changes. Persist web context per browser session id. Keep an explicit **analysis state** (source, sheet, metric, aggregation, filters, dates, group_by, sort/top_n) and let the planner return a *diff* against it, not a whole new plan.
- **Test:** three-turn chains on the fixture: "2026 total" → "month wise" → "sirf top 5" keep source/sheet/metric/dates. The memory file must not influence the plan (run with an empty vs a populated memory → same plan).

## 5. Correction "Not quantity, amount" keeps the old metric

- **Code:** corrections are prompt rule 25 (`openrouter.py:89`). **Pre-route at `analyst.py:265`:** if `COMPLAINT` matches (`galat|wrong|incorrect|not correct|…`, `analyst.py:196`) and the message has ≤ 12 words, `verify_previous` (`analyst.py:389`) re-runs the **previous** plan and never asks the planner.
- **Evidence:** R3: `last_plan.metric = QTY`, question "galat hai, amount chahiye" → reply "• Column: QTY (sum) … 28". The requested change is ignored. The pure form "not quantity, amount" does not match `COMPLAINT` and reaches the planner, where the prior live test passed.
- **Root cause:** the complaint shortcut treats every short message containing "galat/wrong" as "re-check", even when it also carries a new instruction.
- **Status:** CONFIRMED.
- **Fix:** take the shortcut only when the message is a *pure* complaint (no metric, sheet, grouping, date or entity words). Otherwise send it to the planner with `correction_of: previous_plan`. After planning, verify that the corrected field changed (e.g. the metric is no longer QTY).
- **Test:** "galat hai, amount chahiye" → metric AMOUNT. "ye galat hai" → verification path. "customer nahi salesman wise, galat tha" → group_by SALESMAN.

## 6. Customer-wise and salesman-wise confused

- **Code:** grouping column chosen by the LLM (rules 9, 25). `resolve_column` requires an exact or normalised name, and the alias table has only `category`.
- **Evidence:** prior live test `test_correction_customer_to_salesman` passed. No guard ensures the grouping word in the question ("salesman", "sales person") matches the chosen column.
- **Root cause:** no deterministic question-word → dimension mapping. The real sheet uses `SALES PERSON` and `CUSTOMER NAME`, not the words users type.
- **Status:** SUSPECTED residual risk; UNVERIFIED in this audit.
- **Fix:** a synonym map per dimension (salesman/sales person/SP/executive → SALES PERSON; party/customer/client → CUSTOMER NAME) used to check `group_by`, plus the learned mappings (`add_mapping`) scoped per source.
- **Test:** "salesman wise" and "SP wise" → `group_by=[SALES PERSON]`; "party wise" → `[CUSTOMER NAME]`.

## 7. Missing columns / ambiguous data silently replaced

- **Code / evidence:**
  - Missing metric → `resolve_column` returns None → `aggregate_dataframe` raises "Metric column … not found" → replan → an honest out_of_scope message (golden "profit" case). **ALREADY IMPLEMENTED.**
  - **Value resolution is silent** for prefixes and close matches: `resolve_value` (`mcp_server.py:45-60`) maps "ABC" → "ABC Traders" (R6) and fuzzy matches with difflib cutoff 0.8. The change is recorded only in `resolved_filters`, and whether it is shown depends on the LLM wording. **CONFIRMED.**
  - Ambiguous business definition: SALES `VOUCHER NAME` = "Sales" (8,012 rows, ₹9,41,33,141.98) vs "SALES PENDING" (240 rows, ₹21,40,169.65). No rule exists, and the planner sometimes filtered to "Sales" and sometimes not (observed 2026-09-26: ₹9.41 cr vs ₹9.63 cr for the same question). **CONFIRMED.**
  - `_count_guard` (`analyst.py:204`) rewrites the metric to a name column for "kitne items" questions. This is deterministic and intended.
- **Root cause:** fuzzy matching without a mandatory user-visible note; business definitions left to the LLM.
- **Fix:** always put resolved-value notes into the reply from code (not the LLM). Require an exact or unique normalised match for eq filters, and return clarify for prefix matches with more than one candidate or for any prefix-only match. Encode the Sales-vs-Pending decision as a per-source rule once the user decides.
- **Test:** "ABC ki sale" with customers "ABC Traders" + "ABC Steel" → clarify. With a single match → the answer contains "ABC → ABC Traders". A sales total follows the configured Pending rule in every run (run 3×).

## 8. LLM numbers do not match tool results

- **Code:** `unsupported_numbers` (`grounding.py:41-52`) allows any number present anywhere in the result, plan or question, anything within 0.5 %, and **any |n| ≤ 12**. `_missing_total` (`analyst.py:109`) adds a total check only when total words appear. Text mode applies the same check against retrieved text (`analyst.py:327`). `count_answer` writes counts from code.
- **Evidence:** R4: "total ₹1,84,08,445.35" (April's row) passes as the 12-month total, which is exactly the 2026-09-26 WhatsApp incident. R5: numbers ≤ 12 are unchecked.
- **Root cause:** the check verifies *presence*, not the *binding* of a number to its meaning (which row, which aggregate).
- **Status:** CONFIRMED.
- **Fix:** move key figures to code. For single-value results, the answer's headline number is inserted by code from `rows[0].value`. For grouped results, the LLM may only reference rows by label, and code renders `label → value`. Keep the LLM for connective prose, and reject answers that contain a number not tied to a rendered row. Remove the ≤ 12 exemption except for ranks tied to the list.
- **Test:** monthly result + total question → the answer must contain the sum of rows; a synthetic LLM answer quoting one row as the total → rejected. "7 customers" with a result of 5 rows → rejected.

---

## Additional causes found during the audit

| ID | Cause | Evidence | Status | Fix |
|---|---|---|---|---|
| A1 | **Global learned refusal rule** in production memory: "Only respond … if related to sales or inventory data; otherwise out_of_scope". It is sent with every planner call (`memory.py:55`, `analyst.py:261`), for every user and source, and is the likely cause of the repeated website refusals on 09-26/27 | `workspace_memory.json` rules[0]; R7 | CONFIRMED | Remove after user approval. Scope rules per source and owner, and require confirmation before saving (`status: learn` currently saves immediately, `analyst.py:290-296`) |
| A2 | **Memory stores wrong plans as "successful"** (`add_plan` after any executed plan, `analyst.py:338`) | 16/82 month-split plans; R8 | CONFIRMED | Store only after validation passes; never replay plans that `sanitize_plan` changed; cap examples per source (e.g. 5) |
| A3 | April-as-total (month split) | fixed by `sanitize_plan` (`analyst.py:233`) + `_missing_total` | ALREADY IMPLEMENTED (but A2 keeps feeding the old shape) | Purge month-split plans from memory |
| A4 | WhatsApp duplicate replies if two webhooks exist (dedup on per-webhook key) | R9; `whatsapp/webhook.py:58` | CONFIRMED | Dedup on `msg_{sessionId}_{data.id}`, ignoring OpenWA's key suffix |
| A5 | Wording call receives the full result (up to 500 rows) | 328,311 chars measured | CONFIRMED | Send ≤ 20 rows + aggregates to the LLM; render the full table from code |
