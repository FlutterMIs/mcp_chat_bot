import base64
import json
import re
import time
import requests
from datetime import date, datetime
from pathlib import Path

USAGE_LOG = Path(__file__).with_name("llm_usage.jsonl")   # one line per call: type, tokens, cost (no content)
MAX_TOKENS = {"plan": 900, "format": 450, "text": 600, "agent": 900, "transcribe": 400}
ROWS_TO_LLM = 20          # grouped rows shown to the wording model
DETAIL_ROWS_TO_LLM = 10   # raw detail rows
DETAIL_COLS_TO_LLM = 8


def compact_result(result):
    """What the wording model sees: a few rows + every aggregate. The channels render the full table themselves."""
    rows = result.get("rows") or []
    out = {k: v for k, v in result.items() if k not in ("rows", "rows_matched", "count")}
    if result.get("rows_matched") is not None:
        out["matching_rows_technical_metadata"] = {"rows": result["rows_matched"], "note": "technical metadata — NOT a business metric (not items, orders, customers or quantity)"}
    if rows and "value" in rows[0]:
        out["rows"] = rows[:ROWS_TO_LLM]
    else:
        keep = list(rows[0].keys())[:DETAIL_COLS_TO_LLM] if rows else []
        out["rows"] = [{k: r.get(k) for k in keep} for r in rows[:DETAIL_ROWS_TO_LLM]]
        if rows and len(rows[0]) > DETAIL_COLS_TO_LLM:
            out["columns_not_shown"] = list(rows[0].keys())[DETAIL_COLS_TO_LLM:]
    out["rows_total"] = len(rows)
    if len(rows) > len(out["rows"]):
        out["rows_omitted"] = len(rows) - len(out["rows"])
    return out


def compact_schema(schema, samples=8):
    """Planner copy of a schema: fewer sample values, no per-column stats it never uses."""
    def col(c):
        d = {k: v for k, v in c.items() if k in ("name", "role", "kind", "distinct_count")}
        vals = c.get("sample_values") or []
        d["sample_values"] = vals[:samples if c.get("role") == "dimension" else 3]
        return d
    out = {k: v for k, v in schema.items() if k not in ("sheets", "columns", "tables")}
    if schema.get("sheets"):
        out["sheets"] = [{"name": t["name"], "row_count": t.get("row_count"), "columns": [col(c) for c in t.get("columns", [])]} for t in schema["sheets"]]
    elif schema.get("columns"):
        out["columns"] = [col(c) for c in schema["columns"]]
    return out

OPENROUTER_URL="https://openrouter.ai/api/v1/chat/completions"

_HINGLISH = re.compile(r"\b(hai|hain|kya|kese|kaise|kaisa|mujhe|chahiye|chhaiye|chaiye|bhai|batao|btao|bta|kitna|kitni|kitne|mein|ka|ki|ke|nahi|aur|kar|karo|do|dikhao|hota|hoti|wala|kaun|kab|kyun|isme|iska|ye|yeh)\b", re.I)


def reply_language(question):
    """What the user wrote in, so the answer matches it (the LLM often drifts to English for Hinglish)."""
    q = question or ""
    if re.search(r"hindi\s+(mein|me)|हिंदी", q, re.I) or re.search(r"[\u0900-\u097F]", q):
        return "Hindi in Devanagari script"
    if re.search(r"english\s+(mein|me)|in english", q, re.I):
        return "English"
    return "Hinglish (Hindi words in Roman script, like the user)" if len(_HINGLISH.findall(q)) >= 2 else "the user's language"


class OpenRouterAI:
    def __init__(self,api_key,model): self.api_key=api_key; self.model=model
    def _request(self,body,kind="plan",model=None):
        """One chat completion with retries on 429/5xx/network, usage logging, and a max_tokens cap per call type."""
        body={"model":model or self.model,"temperature":0,"max_tokens":MAX_TOKENS.get(kind,600),**body}
        headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json","HTTP-Referer":"http://localhost:8501","X-Title":"MCP Universal Business Analyst"}
        last=None
        for attempt in range(3):
            try:
                r=requests.post(OPENROUTER_URL,headers=headers,json=body,timeout=120)
            except requests.RequestException as e:
                last=e; time.sleep(1.5*(attempt+1)); continue
            if r.status_code in (429,500,502,503,504) and attempt<2:
                time.sleep(float(r.headers.get("Retry-After",2*(attempt+1)))); last=requests.HTTPError(f"{r.status_code}"); continue
            r.raise_for_status()
            data=r.json()
            self._log_usage(kind,body["model"],data.get("usage") or {},len(json.dumps(body["messages"],default=str)))
            return data["choices"][0]["message"]
        raise last or requests.HTTPError("OpenRouter request failed")

    @staticmethod
    def _log_usage(kind,model,usage,chars):
        try:
            rec={"ts":datetime.now().isoformat(timespec="seconds"),"kind":kind,"model":model,"input_chars":chars,
                 "prompt_tokens":usage.get("prompt_tokens"),"completion_tokens":usage.get("completion_tokens"),"cost":usage.get("cost")}
            with USAGE_LOG.open("a") as f: f.write(json.dumps(rec)+"\n")
        except OSError:
            pass

    def _call(self,messages,model=None,kind="plan"):
        return self._request({"messages":messages},kind=kind,model=model)["content"]

    def chat_with_tools(self,messages,tools,model=None,tool_choice="auto"):
        """Agent step: returns the assistant message (may carry tool_calls). tool_choice may force one function."""
        return self._request({"messages":messages,"tools":tools,"tool_choice":tool_choice},kind="agent",model=model)
    @staticmethod
    def parse_json(v):
        """First JSON object in the reply; tolerates ``` fences, leading prose and trailing extra objects."""
        v=v.strip()
        if v.startswith("```"):
            v=v.split("\n",1)[1]; v=v.rsplit("```",1)[0]
        start=v.find("{")
        if start<0: raise ValueError("Model did not return JSON")
        try:
            obj,_=json.JSONDecoder().raw_decode(v[start:])
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON from model: {e.msg}") from e
        return obj
    @staticmethod
    def norm(s): return re.sub(r"[^a-z0-9]+"," ",str(s).lower()).strip()

    def plan(self,question,context):
        system=r'''You are the reasoning/planning brain of a universal business data assistant. Return ONLY JSON.

The assistant can use DATABASE, GOOGLE SHEETS (all tabs), uploaded FILES (CSV/XLSX/XLS/PDF/DOCX/PPTX/TXT/MD/JSON/HTML), and WEB URLs. It must be reliable, not merely conversational.

EXECUTE DATA JSON:
{"status":"execute","mode":"data","source_id":"...","source_kind":"workbook|table","sheet_name":null,"operation":"aggregate|rows|distinct","metric":"EXACT REAL COLUMN","aggregation":"sum|avg|count|min|max","group_by":[],"date_column":null,"date_grain":"day|month|year|null","date_from":null,"date_to":null,"filters":[{"column":"EXACT REAL COLUMN","op":"eq|ne|gt|gte|lt|lte|contains|in|not_in","value":"..."}],"sort":"desc|asc|null","top_n":null,"columns":null,"sort_by":null,"limit":500,"chart":"auto|bar|barh|line|area|pie|donut|scatter|none","want_chart":false,"title":"..."}

EXECUTE DOCUMENT/WEB JSON:
{"status":"execute","mode":"text","source_id":"...","operation":"search","answer_needed":true}

OUT OF SCOPE JSON when the question cannot be answered from the connected sources (general knowledge, news, advice, jokes, coding, other companies, anything not in the data):
{"status":"out_of_scope","message":"(in the reply_in language; for a bare greeting use the language of recent_conversation, default Hinglish) ONE short sentence (plus at most one example question) — if the previous assistant message was already such a refusal, just one line; polite reply in the language of the CURRENT question (Hinglish question → Hinglish reply, even if an earlier answer was in Hindi) saying you only answer from their connected data, and 1-2 example questions using REAL column names from the schema"}
Greetings/small talk ("hi", "kya haal hai", "aur bhai kya hai", "aur batao", "thanks", "ok") also use out_of_scope with a friendly one-line message and example questions — even right after a website/document answer; never send small talk to text search.
If the user asks for a metric/column/value that does not exist in the data (e.g. profit when there is no profit or cost column), the message must say plainly that it is not in their data and list what IS available. Never estimate it.

CLARIFY JSON only when a required choice is genuinely ambiguous:
{"status":"clarify","message":"...","options":[{"id":"...","label":"..."}],"reason":"..."}

RULES:
1. Prefer the user's explicitly connected/selected source. If they say sales/inventory and a workbook has SALES/INVENTORY tabs, choose the semantically correct tab.
2. NEVER invent a column. Use EXACT schema names. Do not substitute QTY for AMOUNT when AMOUNT is requested.
3. For phrases like sales amount/revenue/sale value, inspect real columns and choose AMOUNT/SALES AMOUNT/NET SALES/REVENUE only if that real column exists.
4. For quantity, use QTY/QUANTITY only. For rate, use RATE only.
5. If the exact requested business metric is missing, clarify or report that it is unavailable. Never silently use a different metric.
6. If one obvious numeric metric exists, use it. If multiple numeric metrics exist, semantic match; if still ambiguous, clarify.
7. Date-wise/month-wise/year-wise reports use the real date column. Prefer Voucher Date, Sale Date, Invoice Date, Date, Timestamp.
8. “data do/details do” means rows. Do not chart raw rows.
9. A total has no group_by. Category/city/customer/salesperson-wise uses the exact real dimension column.
10. Follow-up questions inherit previous context. Do not re-ask answered information.
11. “this year” means Jan 1 of current year through today; do not invent missing periods.
12. Web/document questions use text mode. Table extraction from a webpage can use data mode if a table schema is available.
13. Never make up values. All numeric answers must come from tool results.
14. "memory" holds rules and column mappings the user confirmed for the connected sources, plus a few validated example plans. Treat it as preference/context, not as data.
15. If user explicitly teaches a mapping/rule ("remember that…", "yaad rakho…", "X ka matlab Y column"), return status learn (the app asks the user to confirm before saving):
{"status":"learn","rule":"..."} or {"status":"learn","term":"...","column":"...","source_id":"...","sheet_name":"..."}
16. Language or voice follow-ups ("isko Hindi mein batao", "English mein bolo", "table read karke sunao", "phir se sunao") are NOT new data questions: repeat previous_plan exactly (same source, metric, group_by, filters, dates). Never learn or clarify for these.
17. GROUNDING: you answer ONLY from the user's connected sources. Never use outside knowledge to fill gaps. If the data cannot answer it, return out_of_scope (or clarify if a source/column choice is the problem).
18. Understand the user like a colleague would: Hindi/Hinglish/English, typos, short forms ("amt", "qty", "cat"), local names ("dilli" = Delhi, "bambai" = Mumbai). Map their words onto REAL column names and REAL sample_values from the schema. Filter values must be spelled as in sample_values when you can see them.
19. "top 5 / sabse zyada / highest" → operation aggregate with sort "desc" and top_n; "sabse kam / lowest" → sort "asc". "sabse bada order / highest sale row" → operation rows with sort_by the metric, sort "desc", limit N.
20. Comparisons ("10000 se zyada", "above", "before March") use filters with op gt/gte/lt/lte. "Delhi ke alawa" → op ne. Several values → op in with a list.
21. For operation rows, "columns" may list the real columns to show (null = all).
36. If "correction_of" is present, the user says the previous answer (that plan) was wrong and tells you what to change: return that plan with the corrected field(s) changed — metric, sheet_name, group_by, filters, dates or aggregation — never the identical plan.
22. If "failed_attempt" is present, your previous plan raised that error. Read the error (it lists real columns/values) and return a corrected plan; do not repeat the same mistake. If the requested thing truly does not exist in the data, return out_of_scope explaining what is available.
23. FOLLOW-UPS: short messages ("2026 ka batao", "month wise dikha", "only top 5", "sirf Delhi", "amount ke basis par") MODIFY previous_plan: keep its source, sheet, metric, filters and dates, change only what the user changed. Never ask the user to repeat dataset/sheet/metric/year.
24. "which one is highest / highest kaun hai / sabse zyada kaun" after previous_plan:
    - previous_plan had group_by → same plan with sort "desc", top_n 1.
    - previous_plan was a total without group_by → the single highest transaction: operation rows, same source/sheet/filters/dates, sort_by the metric, sort "desc", limit 1. Do NOT repeat the total.
    - the user names an entity ("kis customer ki", "which city") → group_by that real column, sort "desc", top_n 1.
    Highest transaction (one row) is different from highest customer total (group + sum); pick from the wording.
25. CORRECTIONS update previous_plan, they are not new topics: "this is closing stock, I am asking sales amount" / "inventory nahi, sales sheet dekho" → switch sheet to the SALES-like tab and metric to its AMOUNT-like column; "not quantity, amount" → metric AMOUNT; "customer wise nahi, salesman wise" → replace that group_by column; "previous answer galat tha" → re-check sheet/metric/filters against the schema and fix the wrong part; "sirf 2026 ka" → date_from 2026-01-01, date_to 2026-12-31 (or today if current year).
26. SHEET CHOICE: sales/revenue/amount questions use the tab whose name or columns are about sales (e.g. SALES), never INVENTORY/STOCK tabs just because they have numbers. If two tabs fit equally, clarify with the tab names.
27. CHARTS: "graph bana do / chart dikhao / graph bhejo" → repeat (or modify) previous_plan with want_chart true. The chart type the user names wins: pie / gol chart / circle → "pie"; donut → "donut"; line / trend / graph over time → "line"; bar / column → "bar"; horizontal bar → "barh"; area → "area"; scatter → "scatter". "isko pie chart mein dikhao" = previous_plan with chart "pie", want_chart true. Without a named type: month/date-wise → "line", categories/rankings → "bar", shares/percentages → "pie". A chart request with no previous_plan and no data question → clarify what to chart. A chart of a ranking needs several bars: when want_chart is true for "sabse zyada / highest / top" without a number, use top_n 10 (the answer still highlights the #1).
28. COMPARISON / GROWTH / CHANGE ("2025 aur 2026 compare karo", "kitna badha") → operation aggregate with date_grain "year" (or "month"), date_from/date_to covering both periods; the tool result carries derived change and change_pct. PERCENTAGE / SHARE → group_by the dimension; derived share_pct is added. DISTINCT ("kitne customers hain", "kaun kaun se city") → operation "distinct" with columns [the real column]; the result has distinct_count and each value with its row count.
35. "just_connected_source" is the source the user added a moment ago. Vague questions ("ye kya hai", "isme kya hai", "iske baare mein batao", "summary do", "what is this") are about THAT source: mode "text" for a website/document, a short overview plan for a sheet. They are not small talk.
30. EARLIER ANSWERS: "earlier_plans" holds the data questions answered before previous_plan (oldest first). When the user points back ("pehle wala", "upar wali customer list", "jo sabse pehle poocha tha", "wo category wala") reuse THAT plan (with want_chart etc. if asked), not previous_plan.
29. DATE RANGE vs DATE GRAIN: a period ("last 12 months", "2026 mein", "is saal", "March se June") sets date_from/date_to only. Set date_grain ONLY when the user asks for a breakdown over time ("month wise", "mahine ke hisaab se", "trend", "har mahine"). "last 12 months" = date_from today minus 12 months, date_to today. "Top items / sabse zyada bikne wala item in last 12 months" → group_by the item column, sort desc, top_n (default 10), NO date_grain — otherwise the same item is split per month.
31. COMPLAINTS ("ye galat hai", "data not correct", "thoda issue hai", "wrong hai", "match nahi kar raha") right after an answer are about THAT answer: re-check previous_plan (date_grain splitting a total, sheet, metric, date column, filters, top_n) and return a corrected execute plan. Never answer them with out_of_scope.
32. "total / kul / kitni sale" over a period is ONE number: aggregate with no group_by and no date_grain.
33. COUNTING: "kitne items / kitne customers / how many" → count, not sum. Distinct names → operation "distinct" with columns [that column]; count of rows matching a word ("telescopic items kitne hain") → aggregate with aggregation "count", metric = the name column, filters [contains word]. Never sum a stock/amount column to answer "kitne".
34. WEBSITES / DOCUMENTS: you only see their name and size, not their text, so you cannot know what they contain. Any question about a connected web page or document ("isme pricing kya hai", "features batao", "ye company kya karti hai", "iska contact number") → mode "text" with that source_id (the one in previous_plan or named in the message). Never answer such questions with out_of_scope — the text search decides whether the answer is there.
'''
        from prompt_builder import planner_section
        system+=planner_section()          # workspace terminology only (empty unless the workspace set one)
        payload={"today":str(date.today()),**context,"question":question,"reply_in":reply_language(question)}
        msgs=[{"role":"system","content":system},{"role":"user","content":json.dumps(payload,ensure_ascii=False,default=str)}]
        raw=self._call(msgs,kind="plan")
        try:
            return self.parse_json(raw)
        except ValueError:
            # Malformed/truncated JSON: one repair attempt, then a clean error the channels can show.
            msgs+= [{"role":"assistant","content":raw[:2000]},{"role":"user","content":"That was not valid JSON. Return ONLY the JSON object, nothing else."}]
            raw=self._call(msgs,kind="plan")
            try:
                return self.parse_json(raw)
            except ValueError as e:
                raise ValueError("Planner ne valid JSON nahi diya; sawal thoda chhota karke dobara pucho.") from e

    def format_result(self,question,plan,result,feedback=None):
        system='''Answer the user's question using ONLY the supplied tool result. Write the answer in the language given in "reply_in". "definition" is the authoritative description of the result: describe only the columns, filters, date range, grouping and calculations listed there. Never say a column/metric was added, included or calculated unless it is in definition.columns. Matching-row counts are technical metadata: never present them as items, orders, customers, products or quantity. Tone: polite business Hinglish — address the user as "aap" (never "tu", "tujhe", "tera"), and do not start with "Bhai"/"Bro". Users type fast with typos and filler ("bro", "bhai", "bri"): never repeat those words in your answer. Never invent, alter, or recalculate numbers: every number you write must appear in the result exactly (you may add ₹ and commas). No percentages, differences or averages unless they are in the result. When plan.aggregation is "count", the value is a number of items/rows/entries (say "33 items hain"), never a quantity, stock or amount. Never say that a chart, graph, file or table was made, attached or sent — the app decides that separately. Do not add outside facts, advice or guesses. If "resolved_filters" is non-empty, briefly say how you read the user's words (e.g. electronic → Electronics). "grand_total_all_groups" is the real overall total across every group (use it for "total sales"); "total_of_shown_rows_only" is just the listed top rows — never call it the total sales. "derived" holds figures computed by the tool (total_of_rows, share_pct, change, change_pct, highest, lowest); use them for comparisons, growth and percentages instead of calculating. If the result has no rows or rows_matched is 0, say no matching data was found. Be concise and natural. Language: reply in the language the user asks for; otherwise match the user's language. "Hindi mein" means Hindi in Devanagari script; "English mein" means English; romanized Hindi questions get a Hinglish reply. Write money (amount, sales, revenue, value, price, rate) with the ₹ symbol and digits (e.g. ₹96,273,311.63) so it can be read aloud correctly. Quantities, stock, counts and units (QTY, CLOSING STOCK, pieces, rows) are plain numbers — never put ₹ on them. If "rows_are_shown_below_your_answer" is true, do NOT list or enumerate the rows, not even for "top N" questions (the app shows them as a table/list right below your text): give a 1-2 sentence summary with the key highlight (e.g. the top item and its value, or the total). If the result is tabular, summarize what it proves and mention the metric/filters. Return ONLY JSON: {"answer":"..."}.'''
        from grounding import result_definition
        from prompt_builder import presentation_section
        system+=presentation_section()     # workspace style/format/personality: presentation only, numbers are re-checked by code
        payload={"question":question,"reply_in":reply_language(question),"definition":result_definition(result,plan,question),"result":compact_result(result)}
        if len(result.get("rows") or [])>=3:
            payload["rows_are_shown_below_your_answer"]=True   # the channel renders the list/table itself
        if feedback: payload["rejected_previous_answer"]=feedback
        raw=self._call([{"role":"system","content":system},{"role":"user","content":json.dumps(payload,ensure_ascii=False,default=str)}],kind="format")
        return self.parse_json(raw)

    def answer_text(self,question,retrieved):
        system='''Write the answer in the language given in "reply_in" (Hinglish question → Hinglish answer, even if the page is English). Tone: polite business Hinglish — address the user as "aap" (never "tu", "tujhe", "tera"), and do not start with "Bhai"/"Bro". Answer from the retrieved source text only. Never use outside knowledge, even for well-known facts. Explain clearly in 2-5 short sentences (or a few bullets for steps/features), like a helpful colleague. Users type fast with typos and filler ("bro", "bhai", "bri", "kese", "recoin" = recording): understand the intent and never repeat those typos or filler words in your answer. Hinglish: "kon kon / kaun kaun (se) / kya kya" = "which ones — list them" (not "who"), "kaise" = how, "kitne" = how many. If the page lists items (reports, features, plans), list them by name. If the source does not contain the answer, set "found" to false. Never mention "retrieved", "source text" or "document provided" — talk about "website" / "file" like a person would. Language: reply in the language the user asks for; otherwise match the user's language. "Hindi mein" means Hindi in Devanagari script; "English mein" means English; romanized Hindi questions get a Hinglish reply. Write money (amount, sales, revenue, value, price, rate) with the ₹ symbol and digits (e.g. ₹96,273,311.63) so it can be read aloud correctly. Quantities, stock, counts and units (QTY, CLOSING STOCK, pieces, rows) are plain numbers — never put ₹ on them. Return ONLY JSON: {"answer":"...","found":true|false}.'''
        from prompt_builder import presentation_section
        system+=presentation_section()
        if retrieved.get("chunks"):
            system+=('\n\nRETRIEVAL: "retrieved.chunks" are the only passages you may use; each carries "source", "page"/"section". '
                     'If the passages do not answer the question, set "found" to false — never fill the gap. Do not write citations yourself: the app adds them.')
        raw=self._call([{"role":"system","content":system},{"role":"user","content":json.dumps({"question":question,"reply_in":reply_language(question),"retrieved":retrieved},ensure_ascii=False,default=str)}],kind="text")
        return self.parse_json(raw)

    def transcribe(self,audio_bytes,fmt="wav",model="google/gemini-2.5-flash"):
        """Voice message -> text via an audio-capable OpenRouter model. Returns the words as spoken."""
        prompt=("Transcribe this voice message exactly as spoken. The speaker may use English, Hindi or Hinglish. "
                "Write Hindi words in the script the speaker would type in chat: romanized Hinglish for mixed speech, Devanagari only for fully Hindi speech. "
                "Keep numbers as digits and business words (sales, amount, category, city) in English. Return only the transcript, nothing else.")
        content=[{"type":"text","text":prompt},{"type":"input_audio","input_audio":{"data":base64.b64encode(audio_bytes).decode(),"format":fmt}}]
        return self._call([{"role":"user","content":content}],model=model,kind="transcribe").strip().strip('"')
