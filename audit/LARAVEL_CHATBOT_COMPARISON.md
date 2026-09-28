# MIS-LARAVEL-WP-CHATBOT vs humara MCP Business Analyst — code review + comparison

Source: `MIS-LARAVEL-WP-CHATBOT-main.zip` (Laravel 13 / PHP 8.3, MySQL, OpenAI gpt-4o / gpt-4o-mini, WhatsApp + web panel).
Date: 2026-09-28. Trigger: ek hi messy sawal dono bots ko diya —

> "bhai mujhe kuchh esa chaiye ki date mont wise sir mujhe usme usme moka top saler usak sale amount and fir mujhe usak total items sale count"

- **Laravel bot** (screenshot 2): seedha ek table de diya — MONTH · SALES PERSON · TOTAL SALES · TOTAL ITEMS SOLD, Grand Total row, "Trend Chart", Download PDF / Save as Image / Export CSV.
- **Humara bot** (screenshot 1): teen baar wahi clarification ("Top 10 kis hisaab se…"), phir SALES PERSON wise table with Sales Amount / Item Count / Customer Count, cards, bar chart, CSV/Excel/Report.

Neeche: (1) Laravel project ka architecture, (2) us sawal par exact code path, (3) wahan ke bugs, (4) humare bot mein kya galat tha (fix ho gaya), (5) kya lena layak hai, kya nahi.

---

## 1. Laravel project — architecture (file map)

| Layer | File | Kya karta hai |
|---|---|---|
| Entry | `routes/api.php` → `ChatController` (web) / `WebhookController` (WhatsApp) | `POST /api/chat` with phone-bound HMAC token; `toWebResponse()` result ko `{reply(html), type, chartMeta, attachments}` mein badalta hai |
| Brain | `app/Services/Chat/TenantRouter.php` (**2,533 lines, "God object"**) | tenant resolve → intent regex → pending state → orchestrator → PDF knowledge → external API → disambiguation → QueryMemory → MultiStepAgent → **AI SQL + 3-attempt self-heal loop** → chart/pivot/images/CSV/PDF/text formatter |
| Data | `app/Services/Sheets/TenantTablesProvisioner.php` + `SheetSchemaDetector` + `TenantSyncService` | Har Google Sheet tab ko **real MySQL table** banata hai (`tenant_<id>_<tab>`), type detect (NUMERIC/DATE/TEXT), role detect (currency/quantity/entity/date/id), date text ke saath `*_actual` DATE column, `tables_metadata` JSON tenant par |
| SQL prompt | `app/Services/Chat/PromptBuilder.php::buildBaseSqlPrompt()` | 20 numbered "CRITICAL RULES" (date phrases → SQL, `col+0` cast, GST dedup, LIKE fuzzy, "bigger table wins"…) + mode notes (`chart` = 2 cols, `pivot` = 3 cols, `images`) + admin `company_context` overlay + **HIGH/MEDIUM/LOW confidence** prefix |
| Tool choice | `app/Services/AI/AiOrchestrator.php` | OpenAI function-calling: `query_database` / `query_external_api` / `search_knowledge` / `manage_calendar` / `get_ledger`, plus `format_instruction` string |
| Complex | `app/Services/Chat/MultiStepAgent.php` | AI bolta hai SIMPLE ya COMPLEX; COMPLEX → ≤5 SQL steps, har step 50 rows, phir AI synthesize |
| Healing | `app/Services/Chat/SelfHeal.php` | error classify (TRANSIENT / SCHEMA_COLUMN / PERMISSION), Levenshtein column fix, `relaxSQL()` (date predicate hatao → LIKE loosen), `validateResult()` (all-NULL row, 0 aggregates, "wise" par 1 row) |
| Memory | `app/Services/AI/QueryMemory.php` + `query_memory` table | normalized question hash → working SQL, confidence, times_used; negation words par invalidate ("Silent Correction") |
| Format | `app/Services/Chat/SmartFormatter.php` | ≤15 rows numbered list + "Total across N"; >15 rows gpt-4o-mini summary; `buildTableHTML()` = web table + **Grand Total row** + "Total Records"; ₹ Indian format with (₹ X Cr) suffix |
| Pivot | `app/Services/Chat/PivotQueryHandler.php` | 3-col SQL rows → in-memory grid, row/col/grand totals, month-aware axis sort, ≤80 cells text table else CSV, axis cap 30 |
| Charts | `app/Services/Chat/ChartBuilder.php` (WhatsApp: QuickChart PNG) + `public/assets/js/chat.js::tryBuildChart()` (web: Chart.js from the HTML table) | web chart **table ke text se parse hota hai** (header regex `total|amount|sales…`, "Rs." strip) |
| Exports | `chat.js::addDownloadButtons()` | PDF = `html2canvas` screenshot → `jsPDF`; Image = html2canvas PNG; CSV = table text → CSV (client-side). Server side: `TenantCsvBuilder`, `TenantPdfBuilder` (dompdf) for >100 / >50 rows on WhatsApp |
| RAG | `app/Services/Chat/PdfKnowledgeService.php` + `tenant_pdf_chunks` (embedding JSON column) | PDF/DOCX/XLSX/CSV/TXT/web → 1,200-char overlapping chunks → keyword LIKE scoring, fallback OpenAI embeddings cosine ≥0.35, adjacent chunk expand, gpt answer with "NONE" escape; access-level filter (all/team/owner) |
| Prompt builder | `tenant_prompts` table, `SuperAdminController::getPrompts/savePrompts`, admin panel | 4 fields: `response_style`, `data_format`, `bot_personality`, `company_context` — **exactly wahi 4 jo humne V8 mein banaye** |
| Extras | Calendar (Google OAuth booking), Ledger PDF (dompdf), External APIs (delegation/call logs/leads), multi-DB switch, row-level access by phone, OTP, dashboards (`DashboardEngine`, `WidgetCatalog`) | humare scope ke bahar |

`MANAGER_REPORT.md` khud kehta hai: "40% Laravel-standard, TenantRouter God object, testing almost impossible". `PROMPT_GUIDE.md` har prompt ka map hai.

---

## 2. Screenshot wala sawal — Laravel bot ka exact path

1. `detectIntent()` → DATA_QUERY (greeting/ignore nahi).
2. `detectQueryMode()` → `data` (query mein "pivot"/"chart" word nahi hai, isliye pivot handler **nahi** chala).
3. `AiOrchestrator::decide()` (gpt-4o-mini function call) → `query_database` + `format_instruction`.
4. `PdfKnowledgeService::search()` skip (orchestrator ne DB chuna).
5. `checkDisambiguationWithPending()` — sirf tab poochta hai jab ek term (e.g. "balance") multiple tables mein ho. Yahan nahi.
6. `QueryMemory::lookup()` miss → `MultiStepAgent::handle()` → AI bola SIMPLE.
7. `aiPlanAndSQL()` (gpt-4o) + 20 rules → **ek SQL**: `SELECT DATE_FORMAT(date_actual,'%b-%y') AS month, sales_person, SUM(amount+0) AS total_sales, COUNT(...) AS total_items_sold … GROUP BY 1,2 ORDER BY total_sales DESC LIMIT N`. Confidence `HIGH` → koi clarification nahi.
8. `validateSQL()` (SELECT-only) → `DB::select()` → `SelfHeal::validateResult()` ok.
9. Web: `ChatController::toWebResponse()` → `SmartFormatter::buildTableHTML(rows)` (Grand Total row + Total Records) → `chat.js` DOM se chart banata hai (labels "Sep-26" → time-series → line chart) → download bar.

**Matlab: LLM ne poora SQL likha, koi deterministic validation nahi ki "amount" hi AMOUNT column hai, "items count" ka matlab COUNT hai ya SUM(qty). Jawab tez aur confident aaya, par sahi hai ya nahi ye code nahi jaanta.**

### Wahi screenshot mein dikhne wale bugs (Laravel side)

- **"TOTAL ITEMS SOLD = Rs. 289"** — `buildTableHTML()` har numeric column ko amount maan leta hai (`$NON_AMT` regex mein "items"/"count"/"qty" nahi hai) → count par "Rs." lag gaya, Grand Total "Rs. 1,180". Yahi Rs. wala text phir chart parse karta hai.
- **Sirf Sep-26** — "date mont wise" bola tha, LLM ne ek hi month (ya `LIMIT 5`) uthaya; month-wise breakdown nahi mila. Rule 12 "top N → LIMIT N" ne 5 rows par kaat diya → 5 salespersons, 1 month. Grand Total isliye poore business ka nahi, sirf in 5 ka hai.
- **Trend chart galat** — X-axis par paanch baar "Sep-26"; "Sales Person" ko numeric series maan ke zero line kheench di (`valCols` regex header "SALES PERSON" ke "SALES" par match ho gaya). Chart table text se banta hai, data se nahi.
- **Items count ka definition unknown** — COUNT(*) rows hai ya COUNT(DISTINCT item) ya SUM(qty)? Table header se pata nahi chalta; user ko lagta hai "items sold".
- **"Grand Total" = shown rows ka sum**, poore data ka nahi (top-N ke baad). Humare yahan `grand_total_all_groups` alag rakha jaata hai isi wajah se.
- **"None" salesperson row** dono bots mein hai (sheet mein blank SALES PERSON) — Laravel ne "-" dikhaya, humne "None". Rule 4 (`col <> 'NA'`) NULL ko filter karta hai par yahan nahi hua.

### Aur general risks jo code mein dikhe

- `relaxSQL()`: 0 rows aaye to **date filter chupchaap hata deta hai** aur "relaxed-filter results" note ke saath jawab deta hai — "August ki sale" ka jawab poore saal ka aa sakta hai (note chhota hai, WhatsApp par miss hota hai).
- `QueryMemory`: same question ka purana SQL reuse (confidence ≥0.7) — data/sheet badle to bhi wahi SQL; sirf negation words par invalidate.
- Rule 18 "bade table ko prefer karo, chhota test data hoga" — guess hai, galat sheet chun sakta hai.
- `MultiStepAgent::synthesize()` mein numbers LLM likhta hai; koi grounding check nahi (humare `grounding.py` jaisa kuch nahi).
- PDF export = screenshot of the bubble (html2canvas); text select/search nahi, bade tables cut. CSV bhi DOM text se ("Rs. 1,71,46,841" string, number nahi).
- `super_admin_phones`, `gmail_user` defaults **code mein hardcoded** (`config/mis.php`) — secrets/PII in repo.
- Charts WhatsApp par QuickChart (external service) ko data bhejte hain.

---

## 3. Humara bot — us sawal par kya hua, aur fix

Path (`analyst.answer` → `understood_reply` → `ambiguity_gate`):

1. "top saler" → `FOLLOW_TOP` = top 10; koi dimension explicitly named nahi (typo "saler") → **ambiguity A**: "Top 10 kis hisaab se — CUSTOMER NAME / SALES PERSON / STATE?" Ye sahi hai — hum guess nahi karte.
2. **Bug**: chip "CUSTOMER NAME wise total" choose karne par rewrite banta tha `"<question> (CUSTOMER NAME wise total)"`. Question ke aakhri shabd "…items sale **count**" ke turant baad "customer" aa gaya → `dimension_matches.counted()` ne CUSTOMER NAME ko "count of customers" samjha, dimension nahi → gate ne dobara poocha (teen baar). "SALES PERSON" isliye chal gaya kyunki "person" count cue ke paas nahi tha.
3. **Fix (commit ke saath)**: `understanding.dimension_matches.counted()` — agar entity ke theek baad grouping word ("wise"/"hisaab"/…) hai to wo grouping hai, count nahi. Saath mein "mont wise" typo ab month grain hai.
4. Ab flow: clarification (1 baar) → "date ke liye TIMESTAMP ya VOUCHER DATE?" (sheet mein do date columns hain; ek baar poochta hai, yaad rakhta hai) → month × sales person table with Sales Amount + Items Count (Laravel jaisa shape, par har number tools se, top-10 by amount); "items count" ka matlab (SUM QTY vs distinct ITEM NAME) settled choice se aata hai.

Real sheet offline (NoLLM) verify: 282 tests pass.

---

## 4. Side-by-side

| | Laravel bot | Humara bot (V8) |
|---|---|---|
| Sawal → data | LLM poora SQL likhta hai (gpt-4o), 20 rules | Deterministic understanding pehle (metrics/period/grain/dimension), LLM sirf plan JSON jab zaroorat; tools code mein compute |
| Galat column se bachav | Prompt rule ("never QTY for AMOUNT") + Levenshtein on error | `semantics.validate_plan` + `SAFE_METRIC_WORDS` + `result_check` — code roke, prompt nahi |
| Ambiguity | Poochta nahi (confidence LOW par hi), warna guess | Chips: sheet / metric / date column / top-N dimension / entity typo |
| 0 rows | filters relax karke jawab (silent-ish) | "koi data nahi" + fuzzy value suggest; period kabhi nahi badalta |
| Numbers in prose | LLM likhta hai (summary) | `grounding.py`: har number result mein hona chahiye, warna code-written answer |
| Totals | Grand Total = shown rows | `grand_total_all_groups` + reconcile across turns |
| Pivot | "pivot" word par 3-col SQL + grid | "month wise sales by city" / "X vs Y" auto; Total row/col; long table when >24 cols |
| Charts | Web: DOM-parse Chart.js; WA: QuickChart | Altair from the DataFrame, type menu |
| Exports | Screenshot PDF, DOM CSV | `exports.ResultExport` → CSV/XLSX/PDF from result |
| RAG | LIKE keyword → embedding fallback → adjacent chunks, "NONE" escape, access levels | BM25 + hashed/OpenRouter embeddings RRF, page/section citations, flag-gated, hybrid → agent |
| Prompt builder | 4 fields per tenant, additive overlays | Same 4 fields per workspace + validation + preview + forbidden overrides |
| Memory | question-hash → SQL cache | validated plans + user-confirmed rules/mappings per source |
| Multi-user | tenants, roles, row-level access by phone, OTP, admin panel | workspaces, members, Google OAuth; row-level access nahi |
| Extras | Calendar booking, ledger PDFs, external APIs, dashboards | Nahi (out of scope) |
| Code health | 2.5k-line router, PHPUnit mostly feature-level | 282 offline tests, modules chhote |

---

## 5. Kya lene layak hai (recommendation)

**Le lo (chhota, safe):**
1. **Grand Total row har grouped table mein** (humare paas `grand_total_all_groups` data hai; UI `display_table` mein bold Total row + "Total Records: N" caption). Laravel wala look yahi hai. *Count/qty columns par ₹ nahi.*
2. **"HIGH/MEDIUM/LOW" jaisa confidence** — hum pehle se ambiguity par poochte hain; planner ke plan ke saath `confidence` field maang sakte hain aur LOW par clarify (planner path ke liye).
3. **Month label "Sep-26"** display option (abhi "2026-09"); `PivotQueryHandler::sortAxis` jaisi month-aware sort humare paas period ISO hone se already hai.
4. **Typo-tolerance** jaisi unke LIKE rule 7 — humne `entities.py` se kar diya; "saler"/"mont" jaise typos `fuzzy_token` vocab mein aur add karo (ho gaya "mont").
5. **Rule 19** — invoice list mein pdf/link columns hamesha include: humare `rows` mode mein link columns ko clickable dikhana (UI).
6. **Row-level access (team/client phone filter)** — agar WhatsApp par multiple users honge to ye feature humare `backend.workspaces` mein sochne layak hai.

**Mat lo:**
- LLM-written SQL as the source of truth; `relaxSQL` (date drop); query-hash SQL cache; chart/CSV DOM parse se; screenshot PDF; hardcoded phones/emails in config.

---

## 6. Repro / verify

```bash
# humara bot, real sheet, NoLLM (planner call hota to AssertionError aata):
.venv/bin/python -m pytest tests -q -p no:cacheprovider -W ignore      # 282 passed
```
Screenshot conversation (real sheet): sawal → "Top 10 kis hisaab se…" → "CUSTOMER NAME wise total" → ab "date column TIMESTAMP ya VOUCHER DATE?" → "VOUCHER DATE" → month × SALES PERSON table (period, SALES PERSON, Sales Amount, Items Count — 10 rows, top-10 by amount). Pehle 2nd step par wahi sawal dobara aata tha.
