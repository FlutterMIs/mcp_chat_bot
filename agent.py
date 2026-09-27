"""Bounded agent loop for complex questions: LLM picks allowlisted tools → tools run on real data → the LLM sees
compact observations (result_ids) → validation → replan on errors → a final answer whose numbers are all bound to results.

Limits: MAX_STEPS LLM turns, MAX_TOOL_CALLS, MAX_SECONDS, MAX_CONTEXT_CHARS. When a limit is hit the best validated
partial result is returned with a note; nothing is ever guessed.
"""
import json
import re
import time

import pandas as pd

from analyst import Reply, chart_hint, _fmt
from grounding import _numbers, enrich_result
from openrouter import compact_schema, reply_language
from resultstore import ResultStore
from semantics import validate_plan

MAX_STEPS = 8            # LLM turns that run tools (a final forced "finish" turn is extra)
MAX_TOOL_CALLS = 10
MAX_SECONDS = 45
MAX_CONTEXT_CHARS = 160_000          # ≈ 40k tokens (estimate); transcript is trimmed before this

# Read-only, allowlisted. load_file / register_* / raw DB tools are deliberately absent.
TOOL_SPECS = [
    ("list_sources", "List connected sources with their sheets/tables and columns (kinds: monetary, quantity, dimension, date...).", {}),
    ("sample_data", "A few raw rows of a sheet/table, to see real values before filtering.",
     {"source_id": "string", "sheet_name": "string?", "n": "integer?"}),
    ("distinct_values", "Real values of one column with row counts (to match the user's wording to actual values).",
     {"source_id": "string", "sheet_name": "string?", "column": "string", "limit": "integer?"}),
    ("aggregate_data", "sum/avg/count/count_distinct/min/max of a column (count_distinct = number of unique values, e.g. distinct customers), optionally grouped, filtered, date-bounded (date_grain month/day/year), sorted, top_n. Returns a result_id.",
     {"source_id": "string", "sheet_name": "string?", "metric": "string", "aggregation": "string?", "group_by": "array?", "filters": "array?",
      "date_column": "string?", "date_grain": "string?", "date_from": "string?", "date_to": "string?", "sort": "string?", "top_n": "integer?"}),
    ("query_data", "Raw rows matching filters (columns, sort_by, sort, limit). Returns a result_id.",
     {"source_id": "string", "sheet_name": "string?", "columns": "array?", "filters": "array?", "sort_by": "string?", "sort": "string?", "limit": "integer?"}),
    ("search_text", "Search a website/document source's text.", {"source_id": "string", "query": "string"}),
    ("join_results", "Join two stored results on a key column each (inner/left/right/outer). Reports unmatched rows. Returns a result_id.",
     {"left_result_id": "string", "right_result_id": "string", "left_key": "string", "right_key": "string", "join_type": "string?"}),
    ("calculate", "Deterministic math on a stored result: expression over its columns, e.g. 'value - Target', 'pct_change(value, prev)', 'share(value)', 'rank(value)', 'running_total(value)', 'sum(value)'. Returns a result_id.",
     {"result_id": "string", "expression": "string", "new_column": "string?"}),
    ("sort_limit", "Sort a stored result by a column and keep the top/bottom N. Returns a result_id.",
     {"result_id": "string", "by": "string", "direction": "string?", "limit": "integer?"}),
    ("compare_periods", "Aggregate a metric for two date ranges (optionally grouped) and compute change and change_pct. Returns a result_id.",
     {"source_id": "string", "sheet_name": "string?", "metric": "string", "date_column": "string", "period_a_from": "string", "period_a_to": "string",
      "period_b_from": "string", "period_b_to": "string", "group_by": "array?", "aggregation": "string?"}),
    ("finish", "Give the final answer. Every number must come from the result_ids you cite.",
     {"answer": "string", "result_ids": "array", "primary_result_id": "string?"}),
]
_TYPES = {"string": {"type": "string"}, "integer": {"type": "integer"}, "array": {"type": "array", "items": {}}}


def tool_definitions():
    out = []
    for name, desc, params in TOOL_SPECS:
        props, req = {}, []
        for p, t in params.items():
            opt = t.endswith("?")
            props[p] = dict(_TYPES[t.rstrip("?")])
            if not opt:
                req.append(p)
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


SYSTEM = """You are a careful data analyst working ONLY with the user's connected data through tools. You never know the data
before looking: inspect sources/schemas/values first when unsure. Plan a few steps, run tools, read the observations
(result_ids with summaries), and finish with a short natural answer in the language noted in reply_in.

Rules:
- All numbers come from tool results; never compute in your head — use calculate/compare_periods/aggregate_data.
- Column names must be exact schema names. If a needed column/value doesn't exist, say so in finish (don't substitute).
- Prefer aggregate_data with group_by/sort/top_n for rankings; use join_results for questions spanning two tables or sources
  (e.g. one table's totals vs another table's values per entity). Check unmatched counts after a join and mention them if large.
- Questions that span two tables/sheets (e.g. sold items AND their stock, revenue AND target): (1) get each side as a result,
  (2) join_results on the shared key column (use the join_hint in the observation), (3) read matched/unmatched counts,
  (4) finish from the joined result. Never say values are missing or "do not match" unless a join_results observation shows it.
- A lookup table used in a join must be complete: aggregate_data grouped by the key, or query_data with a limit ≥ its row count
  (the observation says "truncated" otherwise).
- When two readings are equally plausible, finish with a one-line clarification question instead of guessing.
- Several metrics for the same dimension (e.g. amount + quantity + distinct customers per month): run one aggregate_data per metric with
  the same group_by/date_grain, then join_results on that dimension (period or the group column) until one table has every metric.
- Be efficient: at most {steps} steps and {calls} tool calls. Cite result_ids in finish. Do not repeat a failed call unchanged.
- In the final answer do not mention result_ids, tools or steps — talk like a colleague: what was found, the key figures, and any caveat.
"""


def _tools_schema_for_llm(schemas):
    return [compact_schema(s) | {"source_id": sid} for sid, s in schemas.items()]


class Agent:
    def __init__(self, ai, tools, conv, question, log=None, requested=None):
        self.ai, self.tools, self.conv, self.question = ai, tools, conv, question
        self.requested = requested or []          # measures the user asked for; the final table must cover them all
        self.log = log or (lambda *a, **k: None)
        self.store = ResultStore()
        self.trace = []
        self.calls = 0
        self.started = time.time()

    # ---------- tool execution ----------
    def run_tool(self, name, args):
        s = self.conv.schemas
        if name == "list_sources":
            return {"sources": _tools_schema_for_llm(s)}
        if name in ("sample_data", "distinct_values", "aggregate_data", "query_data", "search_text", "compare_periods"):
            sid = args.get("source_id")
            if sid not in s:
                raise ValueError(f"Unknown source_id {sid}. Connected: {list(s)}")
            sheet = args.get("sheet_name")
            tabs = [t["name"] for t in s[sid].get("sheets") or []]
            if tabs and not sheet:
                if len(tabs) == 1:
                    sheet = tabs[0]
                else:
                    raise ValueError(f"sheet_name required for {sid}; tabs: {tabs}")
            if tabs and sheet not in tabs:
                raise ValueError(f"Sheet {sheet} not in {sid}; tabs: {tabs}")
        if name == "sample_data":
            res = self.tools.call("query_source", {"source_id": sid, "sheet_name": sheet, "limit": int(args.get("n") or 5)})
            return {"rows": res["rows"], "rows_total": res.get("rows_matched")}
        if name == "distinct_values":
            return self.tools.call("distinct_values", {"source_id": sid, "sheet_name": sheet, "column": args["column"], "limit": int(args.get("limit") or 30)})
        if name == "search_text":
            return self.tools.call("search_source", {"source_id": sid, "query": args["query"]})
        if name in ("join_results", "calculate", "sort_limit"):
            return self._run_other(name, args)
        if name in ("aggregate_data", "query_data", "compare_periods"):
            obs = self._run_data(name, args, sid, sheet, s)
            obs.update(self._join_hint(obs["result_id"]))
            return obs
        raise ValueError(f"Tool {name} is not available")

    def _join_hint(self, rid):
        """If another stored result comes from a different table and shares a column, suggest the join."""
        me = self.store.items[rid]
        mine = {_normcol(c): c for c in me["df"].columns if c != "value"}
        for other_id in reversed(self.store.order):
            other = self.store.items[other_id]
            if other_id == rid or other["result"].get("source") in ("join", "calculate", "sort") or (other["source_id"], other["sheet_name"]) == (me["source_id"], me["sheet_name"]):
                continue
            shared = [(mine[_normcol(c)], c) for c in other["df"].columns if _normcol(c) in mine and c != "value"]
            if shared:
                # the smaller result drives the join (e.g. top-5 items on the left, the full stock list on the right)
                left, right, lk, rk = (rid, other_id, shared[0][0], shared[0][1]) if len(me["df"]) <= len(other["df"]) else (other_id, rid, shared[0][1], shared[0][0])
                return {"join_hint": f"join_results(left_result_id='{left}', right_result_id='{right}', left_key='{lk}', right_key='{rk}', join_type='left')"}
        return {}

    def _run_data(self, name, args, sid, sheet, s):
        if name == "aggregate_data":
            sort = str(args.get("sort") or "").lower()
            args["sort"] = "asc" if "asc" in sort else ("desc" if "desc" in sort or args.get("top_n") else None)
            plan = {"status": "execute", "mode": "data", "operation": "aggregate", "source_id": sid, "sheet_name": sheet,
                    **{k: args.get(k) for k in ("metric", "aggregation", "group_by", "filters", "date_column", "date_grain", "date_from", "date_to", "sort", "top_n")}}
            validate_plan(self.question, plan, s[sid])   # generic guards; raise → observation
            res = self.tools.call("aggregate_source", {k: v for k, v in plan.items() if v is not None and k not in ("status", "mode", "operation")})
            rid = self.store.put(enrich_result(res, plan), f"{plan.get('aggregation') or 'sum'} {plan['metric']}" + (f" by {plan['group_by']}" if plan.get("group_by") else ""), sid, sheet)
            return {"result_id": rid, **self.store.summary(rid)}
        if name == "query_data":
            res = self.tools.call("query_source", {"source_id": sid, "sheet_name": sheet, **{k: args[k] for k in ("columns", "filters", "sort_by", "sort") if args.get(k) is not None}, "limit": min(int(args.get("limit") or 1000), 5000)})
            rid = self.store.put(res, f"rows of {sheet or sid}", sid, sheet)
            return {"result_id": rid, **self.store.summary(rid)}
        if name == "compare_periods":
            base = {"source_id": sid, "sheet_name": sheet, "metric": args["metric"], "aggregation": args.get("aggregation") or "sum", "date_column": args["date_column"]}
            gb = args.get("group_by") or []
            a = self.tools.call("aggregate_source", {k: v for k, v in (base | {"group_by": gb, "date_from": args["period_a_from"], "date_to": args["period_a_to"]}).items() if v is not None and v != []})
            b = self.tools.call("aggregate_source", {k: v for k, v in (base | {"group_by": gb, "date_from": args["period_b_from"], "date_to": args["period_b_to"]}).items() if v is not None and v != []})
            da, db = pd.DataFrame(a["rows"]), pd.DataFrame(b["rows"])
            if gb:
                m = da.rename(columns={"value": "period_a"}).merge(db.rename(columns={"value": "period_b"}), on=gb, how="outer").fillna(0)
            else:
                m = pd.DataFrame([{"period_a": float(da["value"].iloc[0]) if len(da) else 0.0, "period_b": float(db["value"].iloc[0]) if len(db) else 0.0}])
            m["change"] = (m["period_b"] - m["period_a"]).round(2)
            m["change_pct"] = ((m["period_b"] - m["period_a"]) / m["period_a"].replace(0, float("nan")) * 100).round(2)
            res = {"rows": m.astype(object).where(pd.notna(m), None).to_dict(orient="records"), "count": len(m), "metric": a.get("metric"),
                   "note": f"period_a={args['period_a_from']}→{args['period_a_to']}, period_b={args['period_b_from']}→{args['period_b_to']}", "source": "compare"}
            rid = self.store.put(res, f"compare {args['metric']} {args['period_a_from']}..{args['period_a_to']} vs {args['period_b_from']}..{args['period_b_to']}", sid, sheet)
            return {"result_id": rid, **self.store.summary(rid)}
        raise ValueError(f"Tool {name} is not available")

    def _run_other(self, name, args):
        if name == "join_results":
            rid = self.store.join(args["left_result_id"], args["right_result_id"], args["left_key"], args["right_key"], args.get("join_type") or "inner")
            return {"result_id": rid, **self.store.summary(rid)}
        if name == "calculate":
            rid = self.store.calculate(args["result_id"], args["expression"], args.get("new_column"))
            return {"result_id": rid, **self.store.summary(rid)}
        if name == "sort_limit":
            rid = self.store.sort_limit(args["result_id"], args["by"], args.get("direction") or "desc", args.get("limit"))
            return {"result_id": rid, **self.store.summary(rid)}
        raise ValueError(f"Tool {name} is not available")

    # ---------- the loop ----------
    def run(self):
        sys_prompt = SYSTEM.format(steps=MAX_STEPS, calls=MAX_TOOL_CALLS)
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": json.dumps({"question": self.question, "reply_in": reply_language(self.question), "today": time.strftime("%Y-%m-%d"),
                                                             "sources": _tools_schema_for_llm(self.conv.schemas), "previous_plan": self.conv.last_plan,
                                                             "recent_conversation": [f"{h['role']}: {h['content'][:200]}" for h in self.conv.history[-6:]]}, ensure_ascii=False, default=str)}]
        tools = tool_definitions()
        final_feedback_used = False
        seen_calls = set()
        stop_note = None
        tool_turns = 0
        for step in range(MAX_STEPS + 2):          # +2: turns spent repairing a rejected final answer don't count as steps
            if time.time() - self.started > MAX_SECONDS:
                stop_note = "time limit"
                break
            self._trim(messages)
            if tool_turns >= MAX_STEPS:
                # Out of tool steps: one last turn that can only finish, from what is already in the results.
                messages.append({"role": "user", "content": "Step limit reached. Call finish now with the best answer supported by the results above (say what could not be completed)."})
                finish_only = [t for t in tools if t["function"]["name"] == "finish"]
                for repair in range(2):      # one forced finish + one repair with the exact rejection reason
                    msg = self.ai.chat_with_tools(messages, finish_only, tool_choice={"type": "function", "function": {"name": "finish"}})
                    messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
                    reply = None
                    for call in msg.get("tool_calls") or []:
                        try:
                            reply = self._finish(json.loads(call["function"].get("arguments") or "{}"))
                        except ValueError as e:
                            self.log("agent_finish_parse_error", error=str(e)[:120])
                        if reply is not None:
                            return reply
                        messages.append({"role": "tool", "tool_call_id": call.get("id", "finish"), "name": "finish",
                                         "content": json.dumps({"error": getattr(self, "_pending_feedback", None) or "Numbers not found in the results. Use only figures shown in the result summaries above."})})
                        self._pending_feedback = None
                stop_note = "step limit"
                break
            msg = self.ai.chat_with_tools(messages, tools)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # A plain text reply: treat as a finish attempt.
                reply = self._finish({"answer": msg.get("content") or "", "result_ids": self.store.order[-2:]})
                if reply is not None:
                    return reply
                messages.append({"role": "user", "content": "Numbers in that answer are not in any result. Call finish with an answer that only uses figures from the result_ids, or run a tool to compute them."})
                if final_feedback_used:
                    break
                final_feedback_used = True
                continue
            if any(c["function"]["name"] != "finish" for c in tool_calls):
                tool_turns += 1
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                if name == "finish":
                    reply = self._finish(args)
                    if reply is not None:
                        return reply
                    obs = {"error": getattr(self, "_pending_feedback", None) or "The answer contains numbers that are not in the cited results. Use only figures from result summaries, or compute them with calculate."}
                    self._pending_feedback = None
                    if final_feedback_used:
                        stop_note = "answer could not be grounded"
                        break
                    final_feedback_used = True
                else:
                    self.calls += 1
                    if self.calls > MAX_TOOL_CALLS:
                        stop_note = "tool-call limit"
                        break
                    key = name + json.dumps(args, sort_keys=True, default=str)
                    if key in seen_calls:
                        obs = {"error": "This exact call already ran; its result is above. Use it or change the arguments."}
                    else:
                        seen_calls.add(key)
                        t0 = time.time()
                        try:
                            obs = self.run_tool(name, args)
                            self.trace.append(f"{name}({_brief(args)}) → {obs.get('result_id', 'ok')}")
                        except Exception as e:      # any tool failure becomes an observation; the loop never dies on one tool
                            obs = {"error": f"{type(e).__name__}: {str(e)[:500]}"}
                            self.trace.append(f"{name}({_brief(args)}) ✗ {str(e)[:80]}")
                        self.log("agent_tool", step=step, tool=name, ms=int((time.time() - t0) * 1000), error=obs.get("error", "")[:120])
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "name": name, "content": json.dumps(obs, ensure_ascii=False, default=str)[:6000]})
            if stop_note:
                break
        return self._partial(stop_note or "step limit")

    def _trim(self, messages):
        """Keep the transcript under the character budget: compress old tool observations to one line."""
        size = lambda: sum(len(json.dumps(m, default=str)) for m in messages)
        i = 2
        while size() > MAX_CONTEXT_CHARS and i < len(messages) - 2:
            m = messages[i]
            if m.get("role") == "tool" and len(m.get("content", "")) > 300:
                try:
                    d = json.loads(m["content"])
                    m["content"] = json.dumps({k: d[k] for k in ("result_id", "label", "row_count", "error") if k in d})
                except ValueError:
                    m["content"] = m["content"][:300]
            i += 1

    # ---------- finishing ----------
    def _allowed_numbers(self):
        vals = self.store.all_numbers()
        vals.update(round(n, 2) for n in _numbers(self.question))
        return vals

    def _finish(self, args):
        answer = (args.get("answer") or "").strip()
        if not answer:
            return None
        allowed = self._allowed_numbers()
        bad = [n for n in _numbers(answer) if round(n, 2) not in allowed and not any(a and abs(a - n) / abs(a) < 0.006 for a in allowed)
               and not (n == int(n) and 0 <= n <= 10)]
        if bad:
            self.log("agent_unsupported_numbers", bad=bad[:5], answer=answer[:300])
            return None
        tables = {(it["source_id"], it["sheet_name"]) for it in self.store.items.values() if it["result"].get("source") not in ("join", "calculate", "sort")}
        joined = any(it["result"].get("source") == "join" for it in self.store.items.values())
        if len(tables) >= 2 and not joined and re.search(r"(nahi mil|not (?:found|match|present)|mismatch|match nahi|do not match|don't match|missing)", answer, re.I):
            self.log("agent_unjoined_claim")
            self._pending_feedback = "You claim values are missing/unmatched but never ran join_results. Join the two results on their shared key and answer from the join (matched/unmatched counts)."
            return None
        rid = args.get("primary_result_id") or next((r for r in reversed(self.store.order) if self.store.items[r]["result"].get("source") in ("join", "calculate", "sort")), None) or (self.store.order[-1:] or [None])[-1]
        if self.requested and rid in self.store.items:
            missing = self._uncovered(rid)
            if missing:
                self.log("agent_missing_measures", missing=missing)
                self._pending_feedback = (f"The user asked for these measures but the final table {rid} does not have them: {missing}. "
                                          "Compute each with aggregate_data (same group_by/date_grain), join_results on the dimension, then finish with that result.")
                return None
        df = self.store.df(rid) if rid in self.store.items else None
        plan = {"source_id": self.store.items[rid]["source_id"], "sheet_name": self.store.items[rid]["sheet_name"], "title": self.store.items[rid]["label"]} if rid in self.store.items else None
        chart = chart_hint({"group_by": [c for c in df.columns if c != "value"][:1], "date_grain": "month" if df is not None and "period" in df.columns else None}, df) if df is not None and "value" in (df.columns if df is not None else []) else None
        return Reply(answer, kind="agent", df=df if df is not None and len(df) > 1 else None, chart=chart, plan=plan,
                     metric=self.store.items[rid]["result"].get("metric") if rid in self.store.items else None, trace=list(self.trace))

    def _uncovered(self, rid):
        """Requested measures that have no column in the result (by kind or by entity name)."""
        from semantics import MONEY_WORDS, QUANTITY_WORDS, stem, tokens
        df = self.store.df(rid)
        it = self.store.items[rid]
        cols = {str(c): {stem(t) for t in tokens(c)} for c in df.columns}
        metric = it["result"].get("metric")
        if "value" in cols and metric:
            cols["value"] = {stem(t) for t in tokens(metric)}
        money_s, qty_s = {stem(w) for w in MONEY_WORDS}, {stem(w) for w in QUANTITY_WORDS}
        missing = []
        for w in self.requested:
            if w["kind"] == "monetary":
                ok = any(tk & money_s for tk in cols.values())
            elif w["kind"] == "quantity":
                ok = any(tk & qty_s for tk in cols.values())
            else:
                ok = any(w["entity"] in tk for tk in cols.values()) or any(w["entity"] == stem(t) for c in df.columns for t in tokens(c))
            if not ok:
                missing.append(w["kind"] + (f" of {w['entity']}" if w["entity"] else ""))
        return missing

    def _partial(self, why):
        """Best validated partial: the last stored result rendered by code, plus an honest note."""
        self.log("agent_stopped", reason=why, steps=len(self.trace))
        if not self.store.order:
            return Reply(f"Main is sawal ko poora nahi kar paaya ({why}). Sawal ko chhote hisson mein pucho, jaise pehle ek table ka total.", kind="agent_failed", trace=list(self.trace))
        rid = next((r for r in reversed(self.store.order) if self.store.items[r]["result"].get("source") in ("sort", "calculate", "join")), self.store.order[-1])
        it = self.store.items[rid]
        df = it["df"]
        head = f"⚠️ Poora analysis nahi ho paaya ({why}). Jo verified data mila, wo ye hai — *{it['label']}*:"
        lines = []
        if len(df):
            cols = list(df.columns)[:6]
            for r in df.head(10).to_dict(orient="records"):
                parts = [f"{c}: {_fmt(r[c], c) if isinstance(r[c], (int, float)) and not isinstance(r[c], bool) else r[c]}" if c != "value" else _fmt(r[c], it["result"].get("metric")) for c in cols if r.get(c) is not None]
                lines.append("• " + " · ".join(str(p) for p in parts))
        return Reply(head + ("\n" + "\n".join(lines) if lines else ""), kind="agent_partial", df=df if len(df) > 1 else None, trace=list(self.trace))


def _normcol(c):
    return re.sub(r"[^a-z0-9]+", "", str(c).lower())


def _brief(args):
    keep = {k: v for k, v in args.items() if k in ("source_id", "sheet_name", "metric", "group_by", "top_n", "expression", "left_key", "right_key", "column", "result_id", "by")}
    return ", ".join(f"{k}={str(v)[:24]}" for k, v in keep.items())


# ---------- routing ----------
MULTI_PART = re.compile(r"\b(lekin|but|magar|par|jinka|jinki|jiska|jiski|unka|unki|uska|uski|saath|along with|and their|with their|ke saath|"
                        r"aur unk\w*|aur us\w*|aur unh\w*|and its|and the corresponding|respective)\b", re.I)
COMPLEX_WORDS = re.compile(r"\b(why|kyun|kyu|kyon|reason|wajah|karan|compare\s+\w+\s+(?:with|to|vs|aur|se)|exceed\w*|achiev\w*|vs\.?|versus|"
                           r"target|report\s+(?:bana|banao|generate|create)|milake|mila\s*ke|combine|join|correlat\w*|"
                           r"impact|effect|asar|dono|both\s+(?:sheets|tables|files)|across)\b", re.I)


def classify(question, schemas, direct_failed=False):
    """SIMPLE/MEDIUM → the direct pipeline; COMPLEX → the agent. Schema-driven: words from ≥2 tables/sources, or
    explicit multi-step language, or the direct path already failed twice."""
    if direct_failed:
        return "complex"
    from semantics import content_tokens, stem, stems
    qt = {stem(t) for t in content_tokens(question)}
    # Two tables each matched by a word the other lacks → the question spans both (e.g. revenue from one, target from another).
    matched = {}
    for sid, s in schemas.items():
        tables = s.get("sheets") or ([{"name": s.get("name"), "columns": s.get("columns", [])}] if s.get("columns") else [])
        for t in tables:
            words = stems(t.get("name") or "") | {w for c in t.get("columns", []) for w in stems(c["name"])}
            if qt & words:
                matched[(sid, t.get("name"))] = qt & words
    keys = list(matched)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = matched[keys[i]], matched[keys[j]]
            if (a - b) and (b - a):
                return "complex"
    if len(matched) >= 2 and MULTI_PART.search(question):
        return "complex"
    if COMPLEX_WORDS.search(question) and len(schemas) >= 1 and any((s.get("sheets") and len(s["sheets"]) > 1) or len(schemas) > 1 for s in schemas.values()):
        return "complex"
    return "simple"


def run_agent(conv, question, api_key, model, tools, log=None, requested=None):
    from openrouter import OpenRouterAI
    ai = OpenRouterAI(api_key, model)
    agent = Agent(ai, tools, conv, question, log=log, requested=requested)
    reply = agent.run()
    if reply.kind == "agent" and reply.plan:
        conv.last_plan = {"status": "execute", "mode": "data", "operation": "agent", **reply.plan}
    return reply
